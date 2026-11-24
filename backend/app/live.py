"""Live status + time estimates for running episodes and batches."""

import time
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import func, select

from . import progress
from .config import get_settings
from .db import get_engine, run_messages, runs
from .evaluator import load_tasks

DEFAULT_EPISODE_S = 75.0
DEFAULT_TURNS = 3.0


def _history() -> dict:
    """Average duration / customer turns of finished episodes, by (model, task), model and overall."""
    with get_engine().connect() as c:
        rows = c.execute(
            select(runs.c.agent_model, runs.c.task_id, runs.c.duration_s, func.count(run_messages.c.id))
            .select_from(runs.outerjoin(run_messages, (run_messages.c.run_id == runs.c.id) & (run_messages.c.role == "user")))
            .where(runs.c.kind == "eval", runs.c.status == "done", runs.c.duration_s.is_not(None), runs.c.duration_s > 0)
            .group_by(runs.c.id).order_by(runs.c.created_at.desc()).limit(2000)
        ).all()
    by_mt, by_m, all_ = defaultdict(list), defaultdict(list), []
    for model, task, dur, turns in rows:
        by_mt[(model, task)].append((dur, turns))
        by_m[model].append((dur, turns))
        all_.append((dur, turns))
    return {"mt": by_mt, "m": by_m, "all": all_}


def _avg(xs):
    return sum(xs) / len(xs) if xs else None


def estimate(model: str, task_id: str | None, hist: dict) -> dict:
    for key, src, label in (((model, task_id), hist["mt"], "this model on this task"),
                            (model, hist["m"], "this model"), (None, None, "all runs")):
        samples = (src.get(key) if src is not None else hist["all"]) or []
        if samples:
            samples = samples[:20]
            return {"est_total_s": max(5.0, _avg([d for d, _ in samples])), "expected_turns": max(1.0, _avg([t for _, t in samples])),
                    "based_on": f"{len(samples)} past episode{'s' if len(samples) > 1 else ''} ({label})"}
    return {"est_total_s": DEFAULT_EPISODE_S, "expected_turns": DEFAULT_TURNS, "based_on": "default guess (no history yet)"}


def _ts(dt) -> float | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _parallelism(model: str) -> int:
    """Episodes that run at once (provider request caps slow them down but don't serialize them)."""
    return max(1, get_settings().max_concurrency)


def live_for_rows(rows: list[dict], hist: dict | None = None) -> list[dict]:
    """Live info for the given run rows (queued/running ones get phase + ETA)."""
    hist = hist or _history()
    titles = {t["id"]: t["title"] for t in load_tasks()}
    now = time.time()
    with get_engine().connect() as c:
        queued_all = c.execute(select(runs.c.id).where(runs.c.kind == "eval", runs.c.status == "queued")
                               .order_by(runs.c.created_at)).scalars().all()
    qpos = {rid: i + 1 for i, rid in enumerate(queued_all)}
    out = []
    for r in rows:
        if r["status"] not in ("queued", "running"):
            continue
        st = progress.get(r["id"]) or {}
        est = estimate(r["agent_model"], r["task_id"], hist)
        started = st.get("started_at") or (_ts(r["created_at"]) if r["status"] == "running" else None)
        elapsed = (now - started) if started else 0.0
        turn = st.get("turn") or 0
        phase = st.get("phase") or r["status"]
        if r["status"] == "queued":
            phase = "queued"
        est_total = est["est_total_s"]
        frac = 0.0
        slow = False
        if r["status"] == "running":
            # turn-based progress: halfway through turn t of ~T expected turns
            frac_turn = min(0.9, max(0.05, (max(turn, 1) - 0.5) / max(est["expected_turns"], 1))) if turn else 0.05
            if elapsed > est_total * 0.8:
                # running longer than usual (e.g. rate limits): extrapolate from turn progress
                est_total = max(est_total, elapsed / frac_turn, elapsed + 5)
                slow = True
            frac = min(0.97, max(elapsed / est_total, min(frac_turn, 0.9)))
            if phase == "scoring":
                frac = max(frac, 0.92)
            if phase == "judging":
                frac = max(frac, 0.96)
        remaining = max(2.0, est_total * (1 - frac)) if r["status"] == "running" else est_total
        until = st.get("until")
        wait_left = max(0.0, until - now) if until else 0.0
        out.append({
            "id": r["id"], "task_id": r["task_id"], "title": titles.get(r["task_id"]), "batch_id": r["batch_id"],
            "trial": r["trial"], "agent_model": r["agent_model"], "variant_label": r["variant_label"], "status": r["status"],
            "phase": phase, "phase_label": progress.PHASES.get(phase, phase), "detail": st.get("detail"),
            "turn": turn, "expected_turns": round(est["expected_turns"], 1), "agent_steps": st.get("agent_steps", 0),
            "tool_calls": st.get("tool_calls", 0), "last_tool": st.get("last_tool"), "elapsed_s": round(elapsed, 1),
            "phase_elapsed_s": round(now - st["phase_since"], 1) if st.get("phase_since") else None,
            "est_total_s": round(est_total, 1), "typical_s": round(est["est_total_s"], 1), "slower_than_usual": slow,
            "eta_s": round(remaining + wait_left, 1),
            "progress": round(frac, 3), "wait_left_s": round(wait_left, 1), "based_on": est["based_on"],
            "queue_position": qpos.get(r["id"]),
            "starts_in_s": None,  # filled in by Schedule
        })
    return out


class Schedule:
    """Simulates the FIFO worker pool: when will each active episode finish?

    Running episodes free their slot after their own ETA; queued ones (global FIFO order,
    across every batch and experiment) take the earliest free slot for their estimated
    duration. That gives a finish time for every active run, so any group of runs gets
    an ETA that accounts for whatever is ahead of it in line.
    """

    def __init__(self, hist: dict | None = None):
        self.hist = hist or _history()
        with get_engine().connect() as c:
            rows = [dict(r) for r in c.execute(select(runs).where(runs.c.kind == "eval",
                                                                   runs.c.status.in_(["queued", "running"]))
                                               .order_by(runs.c.created_at)).mappings()]
        self.live = {l["id"]: l for l in live_for_rows(rows, self.hist)}
        self.par = _parallelism("")
        running = [l for l in self.live.values() if l["status"] == "running"]
        queued = sorted((l for l in self.live.values() if l["status"] == "queued"),
                        key=lambda l: l["queue_position"] or 0)
        slots = sorted(l["eta_s"] for l in running)[: self.par]
        slots += [0.0] * max(0, self.par - len(slots))
        self.finish: dict[str, float] = {l["id"]: l["eta_s"] for l in running}
        for q in queued:
            i = min(range(len(slots)), key=lambda k: slots[k])
            q["starts_in_s"] = round(slots[i], 1)
            slots[i] += q["est_total_s"]
            self.finish[q["id"]] = slots[i]
        self.free_at = sorted(slots)  # when each slot frees up after everything currently queued

    def what_if(self, durations: list[float]) -> float:
        """ETA for a hypothetical set of new episodes queued behind everything else."""
        slots = list(self.free_at)
        end = 0.0
        for d in durations:
            i = min(range(len(slots)), key=lambda k: slots[k])
            slots[i] += d
            end = max(end, slots[i])
        return end


def group_progress(rows: list[dict], sched: "Schedule | None" = None) -> dict:
    """Progress + ETA for any set of runs (a batch, an experiment, one variant)."""
    if not rows:
        return {"done": 0, "total": 0, "running": 0, "queued": 0, "parallelism": 1, "eta_s": 0.0, "elapsed_s": None,
                "progress": 1.0, "ends_at": None, "live": []}
    unfinished = [r for r in rows if r["status"] in ("queued", "running")]
    sched = sched or (Schedule() if unfinished else None)
    live = [sched.live[r["id"]] for r in unfinished if sched and r["id"] in sched.live]
    done = sum(1 for r in rows if r["status"] in ("done", "error"))
    running = [l for l in live if l["status"] == "running"]
    queued = [l for l in live if l["status"] == "queued"]
    eta = max((sched.finish.get(r["id"], 0.0) for r in unfinished), default=0.0) if sched else 0.0
    started = min((_ts(r["created_at"]) for r in rows), default=None)
    now = time.time()
    return {
        "done": done, "total": len(rows), "running": len(running), "queued": len(queued),
        "parallelism": sched.par if sched else _parallelism(""),
        "eta_s": round(eta, 1), "elapsed_s": round(now - started, 1) if started and unfinished else None,
        "progress": round((done + sum(l["progress"] for l in running)) / len(rows), 3),
        "ends_at": datetime.fromtimestamp(now + eta, timezone.utc).isoformat() if unfinished else None,
        "live": sorted(live, key=lambda l: (l["status"] != "running", l["queue_position"] or 0)),
    }


def batch_progress(rows: list[dict]) -> dict:
    return group_progress(rows)


def all_active() -> list[dict]:
    sched = Schedule()
    return sorted(sched.live.values(), key=lambda l: (l["status"] != "running", l["queue_position"] or 0))


def estimate_new(model: str, task_ids: list[str], trials: int, n_variants: int = 1) -> dict:
    """How long would a new batch/experiment take, given what's already queued?"""
    sched = Schedule()
    durs = [estimate(model, t, sched.hist)["est_total_s"] for _ in range(trials * n_variants) for t in task_ids]
    based = estimate(model, task_ids[0] if task_ids else None, sched.hist)["based_on"] if task_ids else ""
    ahead = sum(1 for l in sched.live.values())
    return {"episodes": len(durs), "eta_s": round(sched.what_if(durs), 1), "parallelism": sched.par,
            "avg_episode_s": round(sum(durs) / len(durs), 1) if durs else 0, "ahead_in_queue": ahead, "based_on": based}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

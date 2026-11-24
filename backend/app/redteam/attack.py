"""Attack the target agents with gate-passing scenarios, then triage: which scenarios break them."""

import time
from collections import defaultdict

from sqlalchemy import select

from ..agent import VARIANT_PRESETS
from ..db import get_engine, runs

KEEP_FAIL_RATE = 0.5  # a scenario "breaks" the agents if they fail at least this often


def run_attack(task_ids: list[str], models: list[str], trials: int, user_model: str | None) -> dict[str, str]:
    """Queue one batch per target model and wait for all of them. Returns {model: batch_id}."""
    from ..runner import submit_batch

    batches = {}
    for m in models:
        bid, _ = submit_batch(task_ids, trials, m, user_model, VARIANT_PRESETS["baseline"])
        batches[m] = bid
    while True:
        with get_engine().connect() as c:
            left = c.execute(select(runs.c.id).where(runs.c.batch_id.in_(list(batches.values())),
                                                     runs.c.status.in_(("queued", "running")))).all()
        if not left:
            return batches
        time.sleep(3)


def triage(task_ids: list[str], batch_ids: list[str]) -> dict[str, dict]:
    """Per scenario: pass/fail counts per model (infra errors excluded) and a verdict.

    - ``breaks``:   at least one target fails >= KEEP_FAIL_RATE of its trials, AND some agent passed at least once,
                    so it's empirically solvable. These are kept (``breaks_models`` says which targets).
    - ``unsolved``: no agent ever passed. Could be very hard or broken; needs a human look before it's trusted.
    - ``robust``:   agents mostly pass. Discarded as a red-team find (still a valid task).
    """
    with get_engine().connect() as c:
        rows = [dict(r) for r in c.execute(select(runs.c.task_id, runs.c.agent_model, runs.c.status, runs.c.reward,
                                                  runs.c.analysis, runs.c.end_reason, runs.c.process_ok,
                                                  runs.c.db_match).where(runs.c.batch_id.in_(batch_ids))).mappings()]
    by_task: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_task[r["task_id"]].append(r)
    out = {}
    for tid in task_ids:
        rs = [r for r in by_task.get(tid, []) if r["status"] == "done"]
        per_model: dict[str, dict] = defaultdict(lambda: {"passed": 0, "episodes": 0})
        causes: dict[str, int] = defaultdict(int)
        for r in rs:
            pm = per_model[r["agent_model"]]
            pm["episodes"] += 1
            if (r["reward"] or 0) >= 1:
                pm["passed"] += 1
            else:
                causes[(r["analysis"] or {}).get("category", "unanalyzed")] += 1
        n = len(rs)
        passed = sum(1 for r in rs if (r["reward"] or 0) >= 1)
        fail_rate = (n - passed) / n if n else None
        # a scenario that reliably breaks ONE target is a find, even if another target handles it
        broken = sorted(m for m, pm in per_model.items()
                        if pm["episodes"] and (pm["episodes"] - pm["passed"]) / pm["episodes"] >= KEEP_FAIL_RATE)
        if not n:
            verdict = "not_run"
        elif passed == 0:
            verdict = "unsolved"
        elif broken:
            verdict = "breaks"
        else:
            verdict = "robust"
        out[tid] = {"verdict": verdict, "episodes": n, "passed": passed, "fail_rate": fail_rate,
                    "breaks_models": broken, "per_model": dict(per_model), "failure_causes": dict(causes),
                    "infra_errors": sum(1 for r in by_task.get(tid, []) if r["status"] == "error")}
    return out


def feedback_text(cands: list[dict], limit: int = 12) -> str:
    """What the attacker should learn from a finished round: what broke the agents, and what the gate rejected."""
    lines = []
    worked = [c for c in cands if (c.get("attack") or {}).get("verdict") == "breaks"]
    if worked:
        lines.append("Scenarios that BROKE the agents (make harder or different variants of these traps):")
        for c in worked[:limit]:
            a = c["attack"]
            lines.append(f"- [{c.get('attack_type')}] {c['title']}: {c.get('why_hard')} "
                         f"(failed {a['episodes'] - a['passed']}/{a['episodes']}; causes {a['failure_causes']})")
    robust = [c for c in cands if (c.get("attack") or {}).get("verdict") == "robust"]
    if robust:
        lines.append("Scenarios the agents HANDLED (these traps don't work; avoid repeating them):")
        lines += [f"- [{c.get('attack_type')}] {c['title']}" for c in robust[:limit]]
    rejected = [c for c in cands if not (c.get("gate") or {}).get("passed")]
    if rejected:
        lines.append("Scenarios REJECTED by the validity gate (avoid these mistakes):")
        lines += [f"- {c['title']}: {'; '.join((c.get('gate') or {}).get('reasons', []))[:200]}" for c in rejected[:limit]]
    return "\n".join(lines)

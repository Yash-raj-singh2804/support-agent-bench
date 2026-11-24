import logging
import uuid
from collections import Counter, defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import delete, desc, select, text, update

from . import live as live_mod
from . import progress, runner, store
from .agent import VARIANT_PRESETS, AgentConfig, agent_tool_specs, policy_text
from .analysis import TAXONOMY, analyze_run
from .config import get_settings
from .db import get_engine, init_app_tables, llm_calls, run_messages, runs, wait_for_db
from .evaluator import gold_snapshot, load_tasks, pass_hat_k
from .llm import MODEL_PRESETS, PROVIDERS, parse_spec, provider_configured
from .tracing import price_for, pricing, run_trace

logging.basicConfig(level=logging.INFO)


def _cleanup_stale() -> None:
    with get_engine().begin() as c:
        c.execute(update(runs).where(runs.c.kind == "eval", runs.c.status.in_(["queued", "running"]))
                  .values(status="error", error="interrupted by backend restart", reward=0.0))
        schemas = c.execute(text(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'ep\\_%' OR schema_name LIKE 'gold\\_%'"
        )).scalars().all()
    for s in schemas:
        store.drop_schema(s)


@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_db()
    init_app_tables()
    store.rebuild_seed()
    _cleanup_stale()
    for t in load_tasks():  # fail fast if a gold trajectory is broken
        gold_snapshot(t["id"])
    yield


app = FastAPI(title="Support Agent Bench", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_origins.split(","), allow_methods=["*"], allow_headers=["*"])

TASK_CATEGORY = lambda: {t["id"]: t["category"] for t in load_tasks()}  # noqa: E731
LIGHT_COLS = [c for c in runs.c if c.name not in ("diff", "signals")]


# --------------------------------------------------------------------------- meta
@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/config")
def config():
    s = get_settings()
    return {
        "agent_model": s.agent_model, "user_model": s.user_model, "judge_model": s.judge_model,
        "max_concurrency": s.max_concurrency, "auto_analyze": s.auto_analyze,
        "providers": {p: provider_configured(p) for p in PROVIDERS},
        "api_key_configured": provider_configured(parse_spec(s.agent_model)[0]),
        "models": [{**m, "configured": provider_configured(parse_spec(m["spec"])[0]), "price": price_for(m["spec"])}
                   for m in MODEL_PRESETS],
        "variants": {k: v.__dict__ for k, v in VARIANT_PRESETS.items()},
        "taxonomy": TAXONOMY,
    }


@app.get("/api/policy")
def policy(short: bool = False):
    return {"markdown": policy_text(short)}


@app.get("/api/tools")
def tools(variant: str = "baseline"):
    return agent_tool_specs(VARIANT_PRESETS.get(variant, AgentConfig()))


@app.get("/api/tasks")
def tasks():
    with get_engine().connect() as c:
        rows = c.execute(select(runs.c.task_id, runs.c.reward).where(runs.c.kind == "eval", runs.c.status == "done")).all()
    stats: dict[str, list[float]] = {}
    for tid, r in rows:
        stats.setdefault(tid, []).append(r or 0.0)
    return [{**t, "runs": len(stats.get(t["id"], [])), "successes": sum(1 for r in stats.get(t["id"], []) if r >= 1)}
            for t in load_tasks()]


@app.get("/api/store/seed")
def seed_state():
    return store.snapshot(store.SEED_SCHEMA)


# --------------------------------------------------------------------------- summaries
def summarize(rows: list[dict]) -> dict:
    """Aggregate metrics over a set of eval runs (one batch, one variant, ...)."""
    cats = TASK_CATEGORY()
    finished = [r for r in rows if r["status"] in ("done", "error")]
    by_task: dict[str, list[float]] = defaultdict(list)
    for r in finished:
        by_task[r["task_id"]].append(r["reward"] or 0.0)
    n_trials = min((len(v) for v in by_task.values()), default=0)
    rewards = [r["reward"] or 0.0 for r in finished]
    failed = [r for r in finished if (r["reward"] or 0) < 1]
    analyzed = [r for r in failed if r.get("analysis")]
    sim_fault = [r for r in analyzed if r["analysis"].get("responsible") == "user_simulator"]
    adj_n = len(finished) - len(sim_fault)
    by_cat: dict[str, list[float]] = defaultdict(list)
    for r in finished:
        by_cat[cats.get(r["task_id"], "?")].append(r["reward"] or 0.0)
    passed = [r for r in finished if (r["reward"] or 0) >= 1]
    violating_passes = [r for r in passed if (r.get("signals") or {}).get("violations")]
    # Right outcome but failed on process (SCORE_PROCESS): these would have passed under outcome-only scoring.
    process_failures = [r for r in failed if r.get("db_match") and r.get("outputs_ok") and r.get("process_ok") is False]
    grounding = _grounding_summary([r.get("grounding") for r in finished if r.get("grounding")])
    scored_grounding = [r for r in finished if r.get("grounded_ok") is not None]  # bait tasks
    toks = [(r["input_tokens"] or 0) + (r["output_tokens"] or 0) for r in finished if r.get("input_tokens") is not None]
    all_toks = [(r["input_tokens"] or 0) + (r["output_tokens"] or 0) + (r.get("customer_tokens") or 0) + (r.get("judge_tokens") or 0)
                for r in finished if r.get("input_tokens") is not None]
    costs = [r["cost_usd"] for r in finished if r.get("cost_usd") is not None]
    agent_costs = [r["agent_cost_usd"] for r in finished if r.get("agent_cost_usd") is not None]
    durs = [r["duration_s"] for r in finished if r.get("duration_s") is not None]
    return {
        "total": len(rows), "finished": len(finished), "tasks": len(by_task),
        "avg_reward": sum(rewards) / len(rewards) if rewards else None,
        "pass_hat_k": {str(k): pass_hat_k(by_task, k) for k in range(1, n_trials + 1)},
        "adjusted_pass_1": (sum(1 for r in passed) / adj_n) if adj_n else None,
        "by_category": {c: sum(v) / len(v) for c, v in sorted(by_cat.items())},
        "failure_categories": dict(Counter(r["analysis"]["category"] for r in analyzed).most_common()),
        "failure_responsible": dict(Counter(r["analysis"].get("responsible", "agent") for r in analyzed)),
        "unanalyzed_failures": len(failed) - len(analyzed),
        "violating_passes": len(violating_passes),
        "process_failures": len(process_failures),
        "grounding": grounding,
        "grounding_scored_episodes": len(scored_grounding),
        "grounding_failures": sum(1 for r in scored_grounding if r["grounded_ok"] is False),
        "grounding_revisions": sum(r.get("grounding_revisions") or 0 for r in finished),
        "outcome_pass_1": ((len(passed) + len(process_failures)) / len(finished)) if finished else None,
        "passes": len(passed),
        "avg_tokens": sum(toks) / len(toks) if toks else None,
        "avg_total_tokens": sum(all_toks) / len(all_toks) if all_toks else None,
        "total_tokens": sum(all_toks),
        "avg_cost_usd": sum(costs) / len(costs) if costs else None,
        "avg_agent_cost_usd": sum(agent_costs) / len(agent_costs) if agent_costs else None,
        "total_cost_usd": sum(costs) if costs else None,
        "avg_duration_s": sum(durs) / len(durs) if durs else None,
        "errors": sum(1 for r in finished if r["status"] == "error"),
        "rate_limited": sum(1 for r in finished if r["status"] == "error" and _is_rate_limit(r.get("error"))),
    }


def _grounding_summary(gs: list[dict]) -> dict | None:
    """Hallucination metrics over episodes that have a grounding result (see app/grounding)."""
    if not gs:
        return None
    det = [(g.get("deterministic") or {}).get("summary") or {} for g in gs]
    llm = [(g.get("llm") or {}) for g in gs if g.get("llm")]
    llm_ok = [x.get("summary") or {} for x in llm if not x.get("error")]

    def lab(s, k):
        return (s.get("by_label") or {}).get(k, 0)

    llm_claims = sum(s.get("claims", 0) for s in llm_ok)
    llm_bad = sum(lab(s, "contradicted") + lab(s, "unsupported") for s in llm_ok)
    flagged = sum(1 for g in gs if g.get("hallucinated"))
    return {
        "episodes": len(gs),
        "episodes_with_hallucination": flagged,
        "hallucination_episode_rate": flagged / len(gs),
        "structured_claims": sum(s.get("claims", 0) for s in det),
        "structured_unsupported": sum(lab(s, "unsupported") for s in det),
        "llm_episodes": len(llm_ok), "llm_errors": len(llm) - len(llm_ok),
        "llm_claims": llm_claims,
        "contradicted": sum(lab(s, "contradicted") for s in llm_ok),
        "unsupported": sum(lab(s, "unsupported") for s in llm_ok),
        "major_contradicted": sum(s.get("major_contradicted", 0) for s in llm_ok),
        "major_unsupported": sum(s.get("major_unsupported", 0) for s in llm_ok),
        "llm_hallucination_rate": (llm_bad / llm_claims) if llm_claims else None,
    }


def _is_rate_limit(err: str | None) -> bool:
    e = (err or "").lower()
    return any(k in e for k in ("429", "rate limit", "ratelimit", "quota", "rate-limited"))


def _eval_rows(**filters) -> list[dict]:
    q = select(runs).where(runs.c.kind == "eval")
    for k, v in filters.items():
        if v is not None:
            q = q.where(runs.c[k] == v)
    with get_engine().connect() as c:
        return [dict(r) for r in c.execute(q.order_by(runs.c.created_at)).mappings()]


# --------------------------------------------------------------------------- eval runs
class BatchIn(BaseModel):
    task_ids: list[str] | None = None
    trials: int = Field(1, ge=1, le=10)
    agent_model: str | None = None
    user_model: str | None = None
    variant: str | dict | None = None  # preset name or AgentConfig dict


def _variant(v) -> AgentConfig:
    if v is None:
        return AgentConfig()
    if isinstance(v, str):
        if v not in VARIANT_PRESETS:
            raise HTTPException(400, f"unknown variant {v!r}; choose from {list(VARIANT_PRESETS)}")
        return VARIANT_PRESETS[v]
    return AgentConfig.from_dict(v)


@app.post("/api/batches")
def create_batch(body: BatchIn):
    ids = body.task_ids or [t["id"] for t in load_tasks()]
    try:
        batch_id, run_ids = runner.submit_batch(ids, body.trials, body.agent_model, body.user_model, _variant(body.variant))
    except KeyError as e:
        raise HTTPException(404, f"unknown task {e}")
    return {"batch_id": batch_id, "run_ids": run_ids}


@app.get("/api/runs")
def list_runs(kind: str = "eval", batch_id: str | None = None, task_id: str | None = None, limit: int = 100):
    q = select(*LIGHT_COLS).where(runs.c.kind == kind).order_by(desc(runs.c.created_at)).limit(min(limit, 1000))
    if batch_id:
        q = q.where(runs.c.batch_id == batch_id)
    if task_id:
        q = q.where(runs.c.task_id == task_id)
    with get_engine().connect() as c:
        return [dict(r) for r in c.execute(q).mappings()]


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    with get_engine().connect() as c:
        r = c.execute(select(runs).where(runs.c.id == run_id)).mappings().first()
        if r is None:
            raise HTTPException(404)
        msgs = c.execute(select(run_messages).where(run_messages.c.run_id == run_id).order_by(run_messages.c.idx)).mappings().all()
    out = dict(r)
    out["messages"] = [dict(m) for m in msgs]
    if r["task_id"]:
        out["task"] = next((t for t in load_tasks() if t["id"] == r["task_id"]), None)
    return out


@app.get("/api/runs/{run_id}/live")
def get_live(run_id: str):
    with get_engine().connect() as c:
        r = c.execute(select(*LIGHT_COLS).where(runs.c.id == run_id)).mappings().first()
    if r is None:
        raise HTTPException(404)
    if r["kind"] == "playground":
        st = progress.get(run_id)
        if not st:
            return {"id": run_id, "status": "idle", "phase": None}
        return {"id": run_id, "status": "running", "phase": st.get("phase"),
                "phase_label": progress.PHASES.get(st.get("phase"), st.get("phase")), "detail": st.get("detail"),
                "agent_steps": st.get("agent_steps", 0), "tool_calls": st.get("tool_calls", 0),
                "last_tool": st.get("last_tool"), "wait_left_s": max(0.0, (st.get("until") or 0) - __import__("time").time())}
    if r["status"] in ("queued", "running"):
        sched = live_mod.Schedule()
        if run_id in sched.live:
            return {**sched.live[run_id], "finishes_in_s": round(sched.finish.get(run_id, 0.0), 1)}
    return {"id": run_id, "status": r["status"], "phase": "done", "phase_label": progress.PHASES["done"],
            "elapsed_s": r["duration_s"], "progress": 1.0, "eta_s": 0}


@app.get("/api/live")
def get_all_live():
    return {"active": live_mod.all_active(), "phases": progress.PHASES}


@app.get("/api/runs/{run_id}/trace")
def get_trace(run_id: str):
    return run_trace(run_id)


@app.post("/api/runs/{run_id}/analyze")
async def analyze_one(run_id: str):
    try:
        return await run_in_threadpool(analyze_run, run_id, True)
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str):
    with get_engine().begin() as c:
        c.execute(delete(runs).where(runs.c.id == run_id))
    return {"ok": True}


@app.get("/api/batches")
def list_batches():
    rows = [r for r in _eval_rows() if r["batch_id"]]
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["batch_id"]].append(r)
    out = []
    for bid, rs in groups.items():
        unfinished = any(r["status"] in ("queued", "running") for r in rs)
        prog = live_mod.batch_progress(rs) if unfinished else None
        out.append({"eta_s": prog["eta_s"] if prog else None, "batch_id": bid, "experiment_id": rs[0]["experiment_id"], "created_at": rs[0]["created_at"],
                    "agent_model": rs[0]["agent_model"], "user_model": rs[0]["user_model"],
                    "variant_label": rs[0]["variant_label"] or "baseline", **summarize(rs)})
    out.sort(key=lambda b: b["created_at"], reverse=True)
    return out


def _token_split(run_ids: list[str]) -> dict[str, dict]:
    """Per run: input/output tokens, cost and wait time for each role, from llm_calls."""
    from sqlalchemy import func

    if not run_ids:
        return {}
    c_ = llm_calls.c
    with get_engine().connect() as c:
        rows = c.execute(select(c_.run_id, c_.role, func.count(), func.sum(c_.input_tokens), func.sum(c_.output_tokens),
                                func.sum(c_.cached_tokens), func.sum(c_.reasoning_tokens), func.sum(c_.cost_usd),
                                func.sum(c_.wait_ms))
                         .where(c_.run_id.in_(run_ids)).group_by(c_.run_id, c_.role)).all()
    out: dict[str, dict] = defaultdict(dict)
    for rid, role, n, i, o, cached, reasoning, cost, wait in rows:
        out[rid][role] = {"calls": n, "input": int(i or 0), "output": int(o or 0), "cached": int(cached or 0),
                          "reasoning": int(reasoning or 0), "cost_usd": cost, "wait_ms": float(wait or 0)}
    return out


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str):
    rows = _eval_rows(batch_id=batch_id)
    if not rows:
        raise HTTPException(404)
    split = _token_split([r["id"] for r in rows])
    titles = {t["id"]: t["title"] for t in load_tasks()}
    episodes = []
    for r in sorted(rows, key=lambda r: (r["task_id"], r["trial"])):
        sp = split.get(r["id"], {})
        tot_in = sum(v["input"] for v in sp.values())
        tot_out = sum(v["output"] for v in sp.values())
        costs = [v["cost_usd"] for v in sp.values() if v["cost_usd"] is not None]
        episodes.append({
            "id": r["id"], "task_id": r["task_id"], "title": titles.get(r["task_id"]), "trial": r["trial"],
            "status": r["status"], "reward": r["reward"], "error": (r["error"] or "")[:300] or None,
            "rate_limited": r["status"] == "error" and _is_rate_limit(r["error"]),
            "category": (r["analysis"] or {}).get("category"), "duration_s": r["duration_s"],
            "tokens": sp, "input": tot_in, "output": tot_out, "cost_usd": sum(costs) if costs else None,
            "wait_ms": sum(v["wait_ms"] for v in sp.values()),
        })
    done = [e for e in episodes if e["status"] in ("done", "error")]
    n = len(done) or 1
    first = rows[0]
    return {
        "batch_id": batch_id, "created_at": first["created_at"], "agent_model": first["agent_model"],
        "user_model": first["user_model"], "variant_label": first["variant_label"] or "baseline",
        "experiment_id": first["experiment_id"], **summarize(rows),
        "avg_input": sum(e["input"] for e in done) / n, "avg_output": sum(e["output"] for e in done) / n,
        "total_input": sum(e["input"] for e in episodes), "total_output": sum(e["output"] for e in episodes),
        "role_totals": {role: {k: sum(e["tokens"].get(role, {}).get(k, 0) for e in episodes) for k in ("input", "output", "calls")}
                        for role in ("agent", "customer", "judge")},
        "total_wait_ms": sum(e["wait_ms"] for e in episodes),
        "episodes": episodes,
        "progress": live_mod.batch_progress(rows),
    }


@app.post("/api/batches/{batch_id}/retry-errors")
def retry_batch_errors(batch_id: str):
    ids = [r["id"] for r in _eval_rows(batch_id=batch_id) if r["status"] == "error"]
    return {"queued": runner.retry_runs(ids)}


@app.post("/api/batches/{batch_id}/analyze")
def analyze_batch(batch_id: str, only_missing: bool = True):
    rows = _eval_rows(batch_id=batch_id)
    todo = [r["id"] for r in rows if r["status"] in ("done", "error") and (r["reward"] or 0) < 1
            and not (only_missing and r.get("analysis"))]
    for rid in todo:
        runner.pool().submit(analyze_run, rid, True)
    return {"queued": len(todo)}


# --------------------------------------------------------------------------- experiments (ablations)
class ExperimentIn(BaseModel):
    task_ids: list[str] | None = None
    trials: int = Field(5, ge=1, le=10)
    agent_model: str | None = None
    user_model: str | None = None
    variants: list[str | dict] = Field(default_factory=lambda: ["baseline", "think", "checklist", "guards", "all"])


@app.post("/api/experiments/estimate")
def estimate_experiment(body: ExperimentIn):
    ids = body.task_ids or [t["id"] for t in load_tasks()]
    return live_mod.estimate_new(body.agent_model or get_settings().agent_model, ids, body.trials, len(body.variants))


@app.post("/api/batches/estimate")
def estimate_batch(body: BatchIn):
    ids = body.task_ids or [t["id"] for t in load_tasks()]
    return live_mod.estimate_new(body.agent_model or get_settings().agent_model, ids, body.trials)


@app.post("/api/experiments")
def create_experiment(body: ExperimentIn):
    ids = body.task_ids or [t["id"] for t in load_tasks()]
    cfgs = [_variant(v) for v in body.variants]
    if len({c.label for c in cfgs}) != len(cfgs):
        raise HTTPException(400, "variants must be distinct")
    exp_id = "x_" + uuid.uuid4().hex[:10]
    batches = []
    for cfg in cfgs:
        try:
            bid, _ = runner.submit_batch(ids, body.trials, body.agent_model, body.user_model, cfg, experiment_id=exp_id)
        except KeyError as e:
            raise HTTPException(404, f"unknown task {e}")
        batches.append({"variant": cfg.label, "batch_id": bid})
    return {"experiment_id": exp_id, "batches": batches}


def _experiment_summary(exp_id: str, rows: list[dict], sched=None) -> dict:
    unfinished = any(r["status"] in ("queued", "running") for r in rows)
    if unfinished and sched is None:
        sched = live_mod.Schedule()
    by_variant: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_variant[r["variant_label"] or "baseline"].append(r)
    variants = []
    for label, rs in by_variant.items():
        name = next((k for k, c in VARIANT_PRESETS.items() if c.label == label), label)
        vp = live_mod.group_progress(rs, sched) if unfinished else None
        variants.append({"variant": label, "name": name, "config": rs[0]["variant"], "batch_id": rs[0]["batch_id"],
                         **summarize(rs),
                         "progress": {k: v for k, v in vp.items() if k != "live"} if vp else None})
    base = next((v for v in variants if v["variant"] == "baseline"), None)
    for v in variants:
        p1, b1 = v["pass_hat_k"].get("1"), (base or {}).get("pass_hat_k", {}).get("1")
        v["delta_pass_1"] = (p1 - b1) if (p1 is not None and b1 is not None and v is not base) else None
    order = [c.label for c in VARIANT_PRESETS.values()]
    variants.sort(key=lambda v: (order.index(v["variant"]) if v["variant"] in order else 99, v["variant"]))
    return {"experiment_id": exp_id, "created_at": rows[0]["created_at"], "agent_model": rows[0]["agent_model"],
            "user_model": rows[0]["user_model"], "tasks": len({r["task_id"] for r in rows}),
            "finished": sum(1 for r in rows if r["status"] in ("done", "error")), "total": len(rows),
            "variants": variants, "progress": live_mod.group_progress(rows, sched) if unfinished else None}


@app.get("/api/experiments")
def list_experiments():
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in _eval_rows():
        if r["experiment_id"]:
            groups[r["experiment_id"]].append(r)
    sched = live_mod.Schedule() if any(r["status"] in ("queued", "running") for v in groups.values() for r in v) else None
    out = [_experiment_summary(k, v, sched) for k, v in groups.items()]
    for e in out:  # keep the list light
        if e["progress"]:
            e["progress"]["live"] = []
    out.sort(key=lambda e: e["created_at"], reverse=True)
    return out


@app.get("/api/experiments/{exp_id}")
def get_experiment(exp_id: str):
    rows = _eval_rows(experiment_id=exp_id)
    if not rows:
        raise HTTPException(404)
    out = _experiment_summary(exp_id, rows)
    # per-task success matrix: task -> variant -> [rewards]
    matrix: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["status"] in ("done", "error"):
            matrix[r["task_id"]][r["variant_label"] or "baseline"].append(r["reward"] or 0.0)
    out["matrix"] = {t: dict(v) for t, v in matrix.items()}
    return out


# --------------------------------------------------------------------------- failure analysis
@app.get("/api/analysis")
def analysis_overview(batch_id: str | None = None, experiment_id: str | None = None, agent_model: str | None = None,
                      variant_label: str | None = None):
    rows = _eval_rows(batch_id=batch_id, experiment_id=experiment_id, agent_model=agent_model, variant_label=variant_label)
    s = summarize(rows)
    failures = [
        {"id": r["id"], "task_id": r["task_id"], "batch_id": r["batch_id"], "variant_label": r["variant_label"],
         "agent_model": r["agent_model"], "created_at": r["created_at"], **(r["analysis"] or {})}
        for r in reversed(rows) if r["status"] in ("done", "error") and (r["reward"] or 0) < 1
    ]
    violations = [
        {"id": r["id"], "task_id": r["task_id"], "violations": r["signals"]["violations"]}
        for r in reversed(rows) if (r["reward"] or 0) >= 1 and (r.get("signals") or {}).get("violations")
    ]
    by_task_cat: dict[str, Counter] = defaultdict(Counter)
    for f in failures:
        by_task_cat[f["task_id"]][f.get("category") or "unanalyzed"] += 1
    return {"summary": s, "failures": failures[:300], "violating_passes": violations[:300],
            "by_task": {t: dict(c) for t, c in by_task_cat.items()}, "taxonomy": TAXONOMY}


# --------------------------------------------------------------------------- token usage
@app.get("/api/usage")
def usage(days: int = 30):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import cast, Date, func

    since = datetime.now(timezone.utc) - timedelta(days=days)
    c_ = llm_calls.c
    tok = func.coalesce(func.sum(c_.input_tokens), 0)
    out = func.coalesce(func.sum(c_.output_tokens), 0)
    with get_engine().connect() as c:
        base = select().select_from(llm_calls.join(runs, runs.c.id == c_.run_id)).where(c_.created_at >= since)
        by_model = c.execute(base.add_columns(c_.model, c_.role, func.count(), tok, out, func.sum(c_.cached_tokens),
                                              func.sum(c_.reasoning_tokens), func.sum(c_.cost_usd), func.avg(c_.latency_ms))
                             .group_by(c_.model, c_.role)).all()
        by_day = c.execute(base.add_columns(cast(c_.created_at, Date).label("d"), c_.role, tok + out, func.sum(c_.cost_usd))
                           .group_by("d", c_.role).order_by("d")).all()
        by_kind = c.execute(base.add_columns(runs.c.kind, func.count(func.distinct(c_.run_id)), tok + out, func.sum(c_.cost_usd))
                            .group_by(runs.c.kind)).all()
        top = c.execute(select(runs.c.id, runs.c.kind, runs.c.task_id, runs.c.agent_model, runs.c.variant_label, runs.c.reward,
                               runs.c.input_tokens, runs.c.output_tokens, runs.c.customer_tokens, runs.c.judge_tokens,
                               runs.c.cost_usd, runs.c.created_at)
                        .where(runs.c.created_at >= since, runs.c.input_tokens.is_not(None))
                        .order_by(desc(func.coalesce(runs.c.input_tokens, 0) + func.coalesce(runs.c.output_tokens, 0)))
                        .limit(15)).mappings().all()
    models: dict[str, dict] = {}
    for model, role, calls, i, o, cached, reasoning, cost, lat in by_model:
        m = models.setdefault(model, {"model": model, "calls": 0, "input": 0, "output": 0, "cached": 0, "reasoning": 0,
                                      "cost_usd": None, "by_role": {}, "price": price_for(model)})
        m["calls"] += calls
        m["input"] += int(i)
        m["output"] += int(o)
        m["cached"] += int(cached or 0)
        m["reasoning"] += int(reasoning or 0)
        if cost is not None:
            m["cost_usd"] = (m["cost_usd"] or 0) + cost
        m["by_role"][role] = {"calls": calls, "tokens": int(i) + int(o), "cost_usd": cost, "avg_latency_ms": lat}
    roles: dict[str, dict] = {}
    for m in models.values():
        for role, v in m["by_role"].items():
            r = roles.setdefault(role, {"calls": 0, "tokens": 0, "cost_usd": None})
            r["calls"] += v["calls"]
            r["tokens"] += v["tokens"]
            if v["cost_usd"] is not None:
                r["cost_usd"] = (r["cost_usd"] or 0) + v["cost_usd"]
    days_out: dict[str, dict] = {}
    for d, role, t, cost in by_day:
        e = days_out.setdefault(str(d), {"day": str(d), "agent": 0, "customer": 0, "judge": 0, "cost_usd": 0.0})
        e[role] = int(t)
        e["cost_usd"] += cost or 0
    costs = [m["cost_usd"] for m in models.values() if m["cost_usd"] is not None]
    return {
        "days": days,
        "total": {"calls": sum(m["calls"] for m in models.values()), "input": sum(m["input"] for m in models.values()),
                  "output": sum(m["output"] for m in models.values()), "cached": sum(m["cached"] for m in models.values()),
                  "reasoning": sum(m["reasoning"] for m in models.values()), "cost_usd": sum(costs) if costs else None,
                  "unpriced_models": [m["model"] for m in models.values() if m["cost_usd"] is None]},
        "by_model": sorted(models.values(), key=lambda m: -(m["input"] + m["output"])),
        "by_role": roles,
        "by_kind": {k: {"runs": n, "tokens": int(t), "cost_usd": cost} for k, n, t, cost in by_kind},
        "by_day": list(days_out.values()),
        "top_runs": [dict(r) for r in top],
        "pricing": pricing(),
    }


# --------------------------------------------------------------------------- playground
class SayIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class PlaygroundIn(BaseModel):
    agent_model: str | None = None
    variant: str | dict | None = None


@app.post("/api/playground")
def playground_start(body: PlaygroundIn | None = None):
    body = body or PlaygroundIn()
    return {"run_id": runner.start_playground(body.agent_model, _variant(body.variant))}


@app.post("/api/playground/{run_id}/messages")
async def playground_say(run_id: str, body: SayIn):
    try:
        reply = await run_in_threadpool(runner.playground_say, run_id, body.text)
    except KeyError:
        raise HTTPException(404, "session not found or ended")
    except Exception as e:  # LLM/provider errors -> surface to the UI
        logging.exception("playground turn failed")
        raise HTTPException(502, f"{type(e).__name__}: {e}")
    return {"reply": reply}


@app.get("/api/playground/{run_id}/diff")
def playground_diff(run_id: str):
    with get_engine().connect() as c:
        schema = c.execute(select(runs.c.schema_name).where(runs.c.id == run_id)).scalar_one_or_none()
    if not schema:
        raise HTTPException(404, "session not found or ended")
    return store.diff(store.snapshot(store.SEED_SCHEMA), store.snapshot(schema))


@app.delete("/api/playground/{run_id}")
def playground_end(run_id: str):
    runner.end_playground(run_id)
    return {"ok": True}

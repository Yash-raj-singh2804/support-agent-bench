"""Episode orchestration and persistence."""

import logging
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock

from sqlalchemy import delete, func, insert, select, update

from . import progress, store
from .agent import GREETING, AgentConfig, SupportAgent
from .config import get_settings
from .db import get_engine, llm_calls, run_messages, runs
from .evaluator import evaluate, get_task
from .llm import LLM, make_llm
from .tracing import RunTrace, refresh_run_totals
from .user_sim import SimulatedUser

log = logging.getLogger(__name__)
_pool: ThreadPoolExecutor | None = None
_pool_lock = Lock()


def pool() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=get_settings().max_concurrency, thread_name_prefix="episode")
        return _pool


def _now():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- persistence helpers
class MessageLog:
    def __init__(self, run_id: str):
        self.run_id = run_id
        with get_engine().connect() as c:
            self.idx = c.execute(select(func.coalesce(func.max(run_messages.c.idx), -1))
                                 .where(run_messages.c.run_id == run_id)).scalar_one() + 1

    def __call__(self, m: dict) -> None:
        with get_engine().begin() as c:
            c.execute(insert(run_messages).values(
                run_id=self.run_id, idx=self.idx, role=m["role"], content=m.get("content"),
                tool_calls=m.get("tool_calls"), tool_call_id=m.get("tool_call_id"), tool_name=m.get("name")))
        self.idx += 1


def _update_run(run_id: str, **vals) -> None:
    with get_engine().begin() as c:
        c.execute(update(runs).where(runs.c.id == run_id).values(**vals))


def create_run(kind: str, task_id: str | None = None, batch_id: str | None = None, trial: int = 0,
               schema_name: str | None = None, agent_model: str | None = None, user_model: str | None = None,
               variant: AgentConfig | None = None, experiment_id: str | None = None) -> str:
    s = get_settings()
    variant = variant or AgentConfig()
    rid = uuid.uuid4().hex[:16]
    with get_engine().begin() as c:
        c.execute(insert(runs).values(
            id=rid, kind=kind, task_id=task_id, batch_id=batch_id, experiment_id=experiment_id, trial=trial,
            status="queued", schema_name=schema_name, agent_model=agent_model or s.agent_model,
            user_model=(user_model or s.user_model) if kind == "eval" else "human",
            variant=variant.__dict__, variant_label=variant.label))
    return rid


def _row(run_id: str) -> dict:
    with get_engine().connect() as c:
        return dict(c.execute(select(runs).where(runs.c.id == run_id)).mappings().one())


# --------------------------------------------------------------------------- eval episode
def run_episode(run_id: str, agent_llm: LLM | None = None, user_llm: LLM | None = None, keep_schema: bool = False,
                judge_llm: LLM | None = None) -> None:
    with progress.bind(run_id):
        try:
            _run_episode(run_id, agent_llm, user_llm, keep_schema, judge_llm)
        finally:
            progress.finish(run_id)


def _run_episode(run_id: str, agent_llm: LLM | None, user_llm: LLM | None, keep_schema: bool,
                 judge_llm: LLM | None) -> None:
    from .analysis import analyze_run, compute_signals, load_messages

    s = get_settings()
    row = _row(run_id)
    task = get_task(row["task_id"])
    cfg = AgentConfig.from_dict(row["variant"])
    progress.set_phase(run_id, "starting", "Cloning the seed store into a private schema",
                       task_id=row["task_id"], trial=row["trial"], batch_id=row["batch_id"])
    schema = store.new_sandbox("ep")
    _update_run(run_id, status="running", schema_name=schema)
    log_msg = MessageLog(run_id)
    t0 = time.monotonic()
    agent: SupportAgent | None = None
    trace = RunTrace(run_id)
    try:
        agent_llm = trace.wrap(agent_llm or make_llm(row["agent_model"]), "agent", row["agent_model"])
        user_llm = trace.wrap(user_llm or make_llm(row["user_model"]), "customer", row["user_model"])
        agent = SupportAgent(agent_llm, schema, max_steps=s.max_agent_steps_per_turn, temperature=s.agent_temperature,
                             config=cfg)
        agent.progress_id = run_id
        if cfg.grounded_llm:  # runtime verifier calls are traced (and costed) like the judge's
            from .grounding.verify_llm import verifier_llm

            agent.verifier_llm = trace.wrap(verifier_llm(s.grounded_verifier_model), "judge", s.grounded_verifier_model)
        user = SimulatedUser(user_llm, task["user_scenario"], temperature=s.user_temperature)

        log_msg({"role": "assistant", "content": GREETING})
        agent_text = GREETING
        end_reason = "max_turns"
        for turn in range(1, s.max_user_turns + 1):
            trace.turn = turn
            u = user.reply(agent_text)
            if user.is_stop(u):
                end_reason = "user_stop"
                break
            log_msg({"role": "user", "content": u})
            agent_text = agent.respond(u, on_message=log_msg)
            if agent.transferred:
                end_reason = "transfer"
                break

        agent_texts = [m["content"] for m in agent.messages if m["role"] == "assistant" and m.get("content")]
        progress.set_phase(run_id, "scoring", "Hashing the final database and comparing it with the gold state")
        from .agent import policy_text
        from .grounding.check import find_hallucinations

        logged = load_messages(run_id)
        violations = compute_signals(logged, task)["violations"] if s.score_process else None
        hallucinations = find_hallucinations(task, logged, policy_text(cfg.short_policy))
        res = evaluate(task, schema, agent_texts, violations=violations, hallucinations=hallucinations)
        _update_run(run_id, status="done", reward=res.reward, db_match=res.db_match, outputs_ok=res.outputs_ok,
                    process_ok=res.process_ok, grounded_ok=res.grounded_ok,
                    hallucinations=res.hallucinations if hallucinations is not None else None,
                    grounding_revisions=agent.grounding_revisions, grounding_log=agent.grounding_log or None,
                    missing_outputs=res.missing_outputs,
                    agent_hash=res.agent_hash, gold_hash=res.gold_hash, diff=res.diff, end_reason=end_reason,
                    finished_at=_now())
    except Exception as e:
        log.exception("episode %s failed", run_id)
        _update_run(run_id, status="error", error=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-2000:]}",
                    reward=0.0, finished_at=_now())
    finally:
        _update_run(run_id, duration_s=round(time.monotonic() - t0, 2))
        refresh_run_totals(run_id)
        if not keep_schema:
            store.drop_schema(schema)
            _update_run(run_id, schema_name=None)
    try:
        analyze_run(run_id, use_judge=s.auto_analyze or judge_llm is not None, judge_llm=judge_llm, trace=trace)
    except Exception:
        log.exception("analysis for %s failed", run_id)


def submit_batch(task_ids: list[str], trials: int, agent_model: str | None = None, user_model: str | None = None,
                 variant: AgentConfig | None = None, experiment_id: str | None = None) -> tuple[str, list[str]]:
    batch_id = "b_" + uuid.uuid4().hex[:10]
    for tid in task_ids:
        get_task(tid)  # validate before queueing anything
    ids = []
    for trial in range(trials):
        for tid in task_ids:
            rid = create_run("eval", task_id=tid, batch_id=batch_id, trial=trial, agent_model=agent_model,
                             user_model=user_model, variant=variant, experiment_id=experiment_id)
            ids.append(rid)
            progress.start(rid, task_id=tid, trial=trial, batch_id=batch_id)
            pool().submit(run_episode, rid)
    return batch_id, ids


def retry_runs(run_ids: list[str]) -> int:
    """Reset errored episodes (e.g. rate-limited) and queue them again in place."""
    n = 0
    for rid in run_ids:
        with get_engine().begin() as c:
            c.execute(delete(run_messages).where(run_messages.c.run_id == rid))
            c.execute(delete(llm_calls).where(llm_calls.c.run_id == rid))
            c.execute(update(runs).where(runs.c.id == rid).values(
                status="queued", reward=None, db_match=None, outputs_ok=None, process_ok=None, missing_outputs=None,
                grounded_ok=None, hallucinations=None, grounding_revisions=None, grounding_log=None, agent_hash=None,
                gold_hash=None, diff=None, end_reason=None, error=None, finished_at=None, signals=None, analysis=None,
                input_tokens=None, output_tokens=None, llm_calls=None, customer_tokens=None, judge_tokens=None,
                cost_usd=None, agent_cost_usd=None, duration_s=None))
        progress.start(rid)
        pool().submit(run_episode, rid)
        n += 1
    return n


# --------------------------------------------------------------------------- playground (human customer)
_sessions: dict[str, SupportAgent] = {}
_sess_lock = Lock()


def start_playground(agent_model: str | None = None, variant: AgentConfig | None = None) -> str:
    schema = store.new_sandbox("play")
    rid = create_run("playground", schema_name=schema, agent_model=agent_model, variant=variant)
    _update_run(rid, status="running")
    MessageLog(rid)({"role": "assistant", "content": GREETING})
    return rid


def _agent_for(run_id: str) -> SupportAgent:
    with _sess_lock:
        if run_id in _sessions:
            return _sessions[run_id]
    s = get_settings()
    with get_engine().connect() as c:
        row = c.execute(select(runs).where(runs.c.id == run_id, runs.c.kind == "playground")).mappings().first()
        if row is None or not row["schema_name"]:
            raise KeyError(run_id)
        msgs = c.execute(select(run_messages).where(run_messages.c.run_id == run_id)
                         .order_by(run_messages.c.idx)).mappings().all()
    trace = RunTrace(run_id)
    agent = SupportAgent(trace.wrap(make_llm(row["agent_model"]), "agent", row["agent_model"]), row["schema_name"],
                         max_steps=s.max_agent_steps_per_turn, temperature=s.agent_temperature,
                         config=AgentConfig.from_dict(row["variant"]))
    agent.trace = trace  # type: ignore[attr-defined]
    # rebuild history (e.g. after a backend restart)
    agent.messages = [
        {k: v for k, v in {"role": m["role"], "content": m["content"], "tool_calls": m["tool_calls"],
                           "tool_call_id": m["tool_call_id"], "name": m["tool_name"]}.items() if v is not None}
        for m in msgs
    ]
    for m in agent.messages:  # restore authenticated user for guards
        if m["role"] == "tool" and m.get("name") in ("find_user_id_by_email", "find_user_id_by_name_zip") \
                and not m["content"].startswith("Error"):
            agent.authenticated_user = m["content"].strip()
    with _sess_lock:
        _sessions[run_id] = agent
    return agent


def playground_say(run_id: str, text: str) -> str:
    agent = _agent_for(run_id)
    log_msg = MessageLog(run_id)
    log_msg({"role": "user", "content": text})
    trace = getattr(agent, "trace", None)
    if trace:
        trace.turn = sum(1 for m in agent.messages if m["role"] == "user") + 1
    agent.progress_id = run_id
    progress.start(run_id, kind="playground")
    try:
        with progress.bind(run_id):
            reply = agent.respond(text, on_message=log_msg)
    finally:
        progress.finish(run_id)
        refresh_run_totals(run_id)
    if agent.transferred:
        _update_run(run_id, end_reason="transfer")
    return reply


def end_playground(run_id: str) -> None:
    with get_engine().connect() as c:
        schema = c.execute(select(runs.c.schema_name).where(runs.c.id == run_id)).scalar_one_or_none()
    if schema:
        store.drop_schema(schema)
    with _sess_lock:
        _sessions.pop(run_id, None)
    _update_run(run_id, status="done", schema_name=None, finished_at=_now())

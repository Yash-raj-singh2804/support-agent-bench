"""Token tracing: every LLM call (agent, simulated customer, judge) is recorded with
token counts, latency and estimated cost, so you can see where tokens go in each
episode, turn by turn.
"""

import json
import logging
import time
from functools import lru_cache
from pathlib import Path
from threading import Lock

from sqlalchemy import func, insert, select, update

from . import progress
from .db import get_engine, llm_calls, runs
from .llm import LLM, LLMResponse, parse_spec

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- pricing
@lru_cache
def pricing() -> dict:
    data = json.loads((Path(__file__).parent / "data" / "pricing.json").read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


def price_for(spec: str) -> dict | None:
    provider, model = parse_spec(spec)
    return pricing().get(f"{provider}:{model}")


def cost_usd(spec: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float | None:
    p = price_for(spec)
    if not p:
        return None
    lc = p.get("long_context")
    if lc and input_tokens > lc["threshold"]:
        p = {**p, **{k: v for k, v in lc.items() if k != "threshold"}}
    cached_rate = p.get("cached_input", p["input"])
    uncached = max(0, input_tokens - cached_tokens)
    cost = (uncached * p["input"] + cached_tokens * cached_rate + output_tokens * p["output"]) / 1_000_000
    if p.get("currency", "USD") == "INR":
        from .config import get_settings

        cost /= get_settings().inr_per_usd
    return cost


# --------------------------------------------------------------------------- tracer
class RunTrace:
    """Shared per-run state: sequence numbers and the current customer turn."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.turn = 0
        self._lock = Lock()
        with get_engine().connect() as c:
            self._seq = c.execute(select(func.coalesce(func.max(llm_calls.c.seq), 0))
                                  .where(llm_calls.c.run_id == run_id)).scalar_one()
            self.turn = c.execute(select(func.coalesce(func.max(llm_calls.c.turn), 0))
                                  .where(llm_calls.c.run_id == run_id)).scalar_one()

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def wrap(self, llm: LLM, role: str, spec: str) -> "TracedLLM":
        return TracedLLM(llm, self, role, spec)


class TracedLLM:
    """Wraps any LLM and records one llm_calls row per chat() call."""

    def __init__(self, inner: LLM, trace: RunTrace, role: str, spec: str):
        self.inner = inner
        self.trace = trace
        self.role = role
        self.spec = spec
        self.model = getattr(inner, "model", spec)

    def chat(self, system, messages, tools=None, temperature=0.0) -> LLMResponse:
        rid, turn = self.trace.run_id, self.trace.turn
        if self.role == "agent":
            progress.set_phase(rid, "agent", f"Turn {turn}: deciding what to do next", turn=turn)
        elif self.role == "customer":
            progress.set_phase(rid, "customer", f"Turn {turn}: writing the next message", turn=turn)
        else:
            progress.set_phase(rid, "judging", "Classifying why the episode failed")
        t0 = time.perf_counter()
        err = None
        r: LLMResponse | None = None
        try:
            r = self.inner.chat(system, messages, tools=tools, temperature=temperature)
            return r
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:500]
            raise
        finally:
            self._record(r, (time.perf_counter() - t0) * 1000, err, len(messages), bool(tools))

    def _record(self, r: LLMResponse | None, total_ms: float, err: str | None, n_messages: int, with_tools: bool):
        r = r or LLMResponse()
        latency_ms = max(0.0, total_ms - r.wait_ms)
        try:
            with get_engine().begin() as c:
                c.execute(insert(llm_calls).values(
                    run_id=self.trace.run_id, seq=self.trace.next_seq(), turn=self.trace.turn, role=self.role,
                    model=self.spec, input_tokens=r.input_tokens, output_tokens=r.output_tokens,
                    cached_tokens=r.cached_tokens, reasoning_tokens=r.reasoning_tokens,
                    latency_ms=round(latency_ms, 1), wait_ms=round(r.wait_ms, 1), tool_calls=len(r.tool_calls),
                    n_messages=n_messages,
                    with_tools=with_tools, cost_usd=cost_usd(self.spec, r.input_tokens, r.output_tokens, r.cached_tokens),
                    error=err))
        except Exception:  # tracing must never break an episode
            log.exception("failed to record llm call")


# --------------------------------------------------------------------------- aggregation
ROLES = ("agent", "customer", "judge")


def refresh_run_totals(run_id: str) -> dict:
    """Recompute token/cost totals for a run from its llm_calls and store them on the run."""
    with get_engine().connect() as c:
        rows = c.execute(select(llm_calls.c.role, func.count(), func.sum(llm_calls.c.input_tokens),
                                func.sum(llm_calls.c.output_tokens), func.sum(llm_calls.c.cost_usd),
                                func.count(llm_calls.c.cost_usd))
                         .where(llm_calls.c.run_id == run_id).group_by(llm_calls.c.role)).all()
    by = {r[0]: {"calls": r[1], "input": int(r[2] or 0), "output": int(r[3] or 0), "cost": r[4], "priced": r[5]} for r in rows}
    total_cost = sum((v["cost"] or 0) for v in by.values()) if any(v["priced"] for v in by.values()) else None
    agent = by.get("agent", {})
    vals = dict(
        input_tokens=agent.get("input", 0), output_tokens=agent.get("output", 0), llm_calls=agent.get("calls", 0),
        customer_tokens=by.get("customer", {}).get("input", 0) + by.get("customer", {}).get("output", 0),
        judge_tokens=by.get("judge", {}).get("input", 0) + by.get("judge", {}).get("output", 0),
        agent_cost_usd=agent.get("cost"), cost_usd=total_cost,
    )
    with get_engine().begin() as c:
        c.execute(update(runs).where(runs.c.id == run_id).values(**vals))
    return vals


def run_trace(run_id: str) -> dict:
    with get_engine().connect() as c:
        rows = [dict(r) for r in c.execute(select(llm_calls).where(llm_calls.c.run_id == run_id)
                                           .order_by(llm_calls.c.seq)).mappings()]
    totals: dict[str, dict] = {}
    by_turn: dict[int, dict] = {}
    for r in rows:
        t = totals.setdefault(r["role"], {"calls": 0, "input": 0, "output": 0, "cached": 0, "reasoning": 0,
                                          "latency_ms": 0.0, "wait_ms": 0.0, "cost_usd": None})
        t["wait_ms"] += r["wait_ms"] or 0
        t["calls"] += 1
        t["input"] += r["input_tokens"] or 0
        t["output"] += r["output_tokens"] or 0
        t["cached"] += r["cached_tokens"] or 0
        t["reasoning"] += r["reasoning_tokens"] or 0
        t["latency_ms"] += r["latency_ms"] or 0
        if r["cost_usd"] is not None:
            t["cost_usd"] = (t["cost_usd"] or 0) + r["cost_usd"]
        if r["role"] == "judge":
            continue  # graded after the conversation; reported in by_role only
        b = by_turn.setdefault(r["turn"], {"turn": r["turn"], "agent_tokens": 0, "customer_tokens": 0, "calls": 0,
                                           "agent_calls": 0, "cost_usd": 0.0})
        b[f"{r['role']}_tokens"] += (r["input_tokens"] or 0) + (r["output_tokens"] or 0)
        b["calls"] += 1
        b["agent_calls"] += r["role"] == "agent"
        b["cost_usd"] += r["cost_usd"] or 0
    costs = [t["cost_usd"] for t in totals.values() if t["cost_usd"] is not None]
    return {
        "calls": rows, "by_role": totals, "by_turn": [by_turn[k] for k in sorted(by_turn)],
        "total": {
            "calls": len(rows),
            "input": sum(t["input"] for t in totals.values()),
            "output": sum(t["output"] for t in totals.values()),
            "cost_usd": sum(costs) if costs else None,
            "latency_ms": sum(t["latency_ms"] for t in totals.values()),
            "wait_ms": sum(t["wait_ms"] for t in totals.values()),
        },
    }

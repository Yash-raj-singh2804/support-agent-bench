"""Failure analysis.

Two layers:

1. ``compute_signals`` - deterministic facts pulled from the transcript, computed for
   every run (passing ones too): which writes happened, whether they were authenticated
   and confirmed, whether they touched another customer's order, how they line up with
   the gold actions. Passing runs can still contain policy violations (e.g. a write made
   without explicit confirmation that happened to land on the right state); those are
   surfaced as ``violations``.
2. ``judge`` - for failed runs, an LLM judge reads the scenario, gold actions,
   transcript, DB diff and the signals, and assigns one category from a fixed taxonomy,
   plus who is responsible (agent / user simulator / task / infra). If no judge model is
   available, a rule-based classifier over the signals is used instead.
"""

import json
import logging
import re
from collections import Counter

from sqlalchemy import select

from . import seed_data
from .agent import AUTH_TOOLS, CONFIRM_TOOLS, WRITE_TOOLS, is_affirmative
from .config import get_settings
from .db import get_engine, run_messages, runs
from .evaluator import get_task
from .llm import make_llm, parse_spec, provider_configured

log = logging.getLogger(__name__)

TAXONOMY = {
    "wrong_arguments": "Called the right kind of action but with wrong items, payment method, address or reason.",
    "missed_action": "Did not perform a required change (gave up, wrongly refused, or ended early).",
    "unauthorized_action": "Made a change the policy forbids (outside the window, invalid reason, another user's order, compensation).",
    "extra_action": "Made an additional change the customer did not ask for, on top of the right ones.",
    "no_confirmation": "Changed the database without the customer's explicit confirmation.",
    "authentication": "Acted without authenticating the customer, or authenticated the wrong person.",
    "wrong_info": "The database is right, but the agent left out or misstated information it had to give.",
    "hallucination": "Stated something no tool result or policy supports (an invented date, price, tracking number, address, phone number, restock or ship date).",
    "unnecessary_transfer": "Handed off to a human when the request could be handled (or with the wrong reason).",
    "missed_escalation": "Should have transferred to a human (fraud, legal, high-value claim) but didn't, or acted instead.",
    "user_simulator_error": "The simulated customer departed from the scenario (wrong details, changed goal), so the agent was not at fault.",
    "infra_error": "Episode crashed (API, network, or code error).",
    "other": "Anything else.",
}
RESPONSIBLE = ("agent", "user_simulator", "task", "infra")

_ORDER_OWNER = {o[0]: o[1] for o in seed_data.ORDERS}


def _norm_args(a: dict) -> str:
    a = {k: (sorted(v) if isinstance(v, list) else v) for k, v in a.items() if k != "policy_checklist"}
    return json.dumps(a, sort_keys=True)


def load_messages(run_id: str) -> list[dict]:
    with get_engine().connect() as c:
        rows = c.execute(select(run_messages).where(run_messages.c.run_id == run_id).order_by(run_messages.c.idx)).mappings().all()
    return [dict(r) for r in rows]


def compute_signals(messages: list[dict], task: dict | None) -> dict:
    results = {m["tool_call_id"]: m["content"] or "" for m in messages if m["role"] == "tool"}
    authed: str | None = None
    last_user: str | None = None
    writes, tool_errors, guard_blocks = [], 0, 0
    for m in messages:
        if m["role"] == "user":
            last_user = m["content"]
        for tc in m.get("tool_calls") or []:
            res = results.get(tc["id"], "")
            err = res.startswith("Error")
            tool_errors += err
            guard_blocks += res.startswith("Error: Blocked by guard")
            if tc["name"] in AUTH_TOOLS and not err:
                authed = res.strip()
            if tc["name"] in WRITE_TOOLS:
                oid = tc["arguments"].get("order_id")
                writes.append({
                    "msg_idx": m["idx"], "name": tc["name"], "args": tc["arguments"], "ok": not err,
                    "error": res if err else None, "authenticated_as": authed,
                    "order_owner": _ORDER_OWNER.get(oid), "confirmed": is_affirmative(last_user),
                })

    ok_writes = [w for w in writes if w["ok"]]
    violations = []
    for w in (w for w in ok_writes if w["name"] in CONFIRM_TOOLS):
        if not w["authenticated_as"]:
            violations.append(f"{w['name']} (msg {w['msg_idx']}) executed without authenticating the customer")
        elif w["order_owner"] and w["order_owner"] != w["authenticated_as"]:
            violations.append(f"{w['name']} (msg {w['msg_idx']}) touched another customer's order")
        if not w["confirmed"]:
            violations.append(f"{w['name']} (msg {w['msg_idx']}) executed without explicit customer confirmation")

    gold = (task or {}).get("gold_actions", [])
    gold_names = Counter(a["name"] for a in gold)
    done_names = Counter(w["name"] for w in ok_writes)
    gold_exact = Counter((a["name"], _norm_args(a["arguments"])) for a in gold)
    done_exact = Counter((w["name"], _norm_args(w["args"])) for w in ok_writes)
    return {
        "write_calls": writes,
        "n_tool_calls": sum(len(m.get("tool_calls") or []) for m in messages),
        "tool_errors": tool_errors,
        "guard_blocks": guard_blocks,
        "violations": violations,
        "matched_gold_actions": sum((gold_exact & done_exact).values()),
        "gold_actions": sum(gold_exact.values()),
        "missed_gold_tools": sorted((gold_names - done_names).elements()),
        "extra_writes": sorted((done_names - gold_names).elements()),
        "wrong_args_tools": sorted({n for (n, _a) in (done_exact - gold_exact) if n in gold_names}),
        "customer_turns": sum(1 for m in messages if m["role"] == "user"),
    }


def rule_based(run: dict, sig: dict, task: dict | None) -> dict:
    def out(cat, why, resp="agent"):
        return {"category": cat, "responsible": resp, "explanation": why, "confidence": "low", "method": "rules"}

    if run.get("status") == "error":
        return out("infra_error", (run.get("error") or "")[:300], "infra")
    if run.get("grounded_ok") is False:
        return out("hallucination", f"Unsupported claims: {(run.get('hallucinations') or [])[:5]}")
    if run.get("db_match") and not run.get("outputs_ok"):
        return out("wrong_info", f"Missing required info: {run.get('missing_outputs')}")
    if any("another customer's order" in v for v in sig["violations"]):
        return out("unauthorized_action", "Modified an order that belongs to a different customer.")
    if any("without authenticating" in v for v in sig["violations"]):
        return out("authentication", "Wrote to the database before authenticating the customer.")
    if "transfer_to_human_agents" in sig["missed_gold_tools"]:
        return out("missed_escalation", "The task required a transfer to a human agent, which did not happen.")
    if sig["extra_writes"] == ["transfer_to_human_agents"]:
        return out("unnecessary_transfer", "Transferred to a human when the task didn't call for it.")
    if sig["extra_writes"]:
        if not (task or {}).get("gold_actions"):
            return out("unauthorized_action", f"Made changes on a task that required none: {sig['extra_writes']}")
        return out("extra_action", f"Unrequested writes: {sig['extra_writes']}")
    if sig["wrong_args_tools"]:
        return out("wrong_arguments", f"Right action, wrong arguments: {sig['wrong_args_tools']}")
    if sig["missed_gold_tools"]:
        if run.get("end_reason") == "transfer":
            return out("unnecessary_transfer", f"Transferred instead of performing {sig['missed_gold_tools']}")
        return out("missed_action", f"Required action(s) not performed: {sig['missed_gold_tools']}")
    if any("confirmation" in v for v in sig["violations"]):
        return out("no_confirmation", "A write was made without explicit confirmation.")
    return out("other", "No rule matched.")


def _compact_transcript(messages: list[dict], limit: int = 14000) -> str:
    lines = []
    for m in messages:
        if m["role"] == "user":
            lines.append(f"[{m['idx']}] CUSTOMER: {m['content']}")
        elif m["role"] == "assistant":
            if m.get("content"):
                lines.append(f"[{m['idx']}] AGENT: {m['content']}")
            for tc in m.get("tool_calls") or []:
                lines.append(f"[{m['idx']}] AGENT CALLS {tc['name']}({json.dumps(tc['arguments'])})")
        else:
            c = (m["content"] or "").replace("\n", " ")
            lines.append(f"[{m['idx']}] TOOL {m.get('tool_name')} -> {c[:400]}")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit // 2] + "\n...\n" + text[-limit // 2:]


JUDGE_SYSTEM = """You are an expert evaluator of customer-support agents in a tau-bench style benchmark.
A run FAILED: the final database state did not match the expected (gold) state, required information was missing,
or the agent broke a process rule (see "violations" in the signals: a write without authenticating the customer, on
another customer's order, or without the customer's explicit confirmation). A run can fail on process alone even if
the database matches gold; then the category is usually no_confirmation, authentication or unauthorized_action.
On bait tasks a run also fails if the agent stated unsupported information (GROUNDED OK = False); then the category
is usually hallucination.
Decide the single primary cause of failure.

Categories:
{taxonomy}

responsible must be one of: agent, user_simulator, task, infra.
Use user_simulator only if the simulated customer clearly departed from its scenario (gave wrong details,
changed its goal, accepted something it was told to refuse) in a way that made success impossible.
Use task only if the gold actions themselves look inconsistent with the scenario.

Reply with ONLY a JSON object:
{{"category": "...", "responsible": "...", "explanation": "2-3 sentences citing message numbers",
  "critical_message_idx": <int or null>, "confidence": "high|medium|low"}}"""


def _parse_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def judge(run: dict, messages: list[dict], sig: dict, task: dict | None, judge_llm=None, trace=None) -> dict:
    prior = rule_based(run, sig, task)
    if run.get("status") == "error":
        return prior
    spec = get_settings().judge_model
    if judge_llm is None:
        if not provider_configured(parse_spec(spec)[0]):
            return prior
        judge_llm = make_llm(spec)
    if trace is not None:
        judge_llm = trace.wrap(judge_llm, "judge", spec)
    diff = json.dumps(run.get("diff") or [], default=str)[:5000]
    user_msg = f"""SCENARIO GIVEN TO THE SIMULATED CUSTOMER:
{(task or {}).get('user_scenario')}

GOLD ACTIONS (the only database changes that should happen):
{json.dumps((task or {}).get('gold_actions'), indent=1)}
REQUIRED INFO IN AGENT REPLIES: {(task or {}).get('required_outputs')}  missing: {run.get('missing_outputs')}
DB MATCHES GOLD: {run.get('db_match')}  PROCESS OK: {run.get('process_ok')}  VIOLATIONS: {sig.get('violations')}
GROUNDED OK: {run.get('grounded_ok')}  HALLUCINATIONS FLAGGED BY THE CHECKER: {run.get('hallucinations')}

DB DIFF (expected vs actual): {diff}

DETERMINISTIC SIGNALS: {json.dumps({k: v for k, v in sig.items() if k != 'write_calls'})}
RULE-BASED GUESS: {prior['category']} ({prior['explanation']})
END REASON: {run.get('end_reason')}

TRANSCRIPT:
{_compact_transcript(messages)}"""
    system = JUDGE_SYSTEM.format(taxonomy="\n".join(f"- {k}: {v}" for k, v in TAXONOMY.items()))
    try:
        r = judge_llm.chat(system, [{"role": "user", "content": user_msg}], tools=None, temperature=0.0)
        data = _parse_json(r.content) or {}
    except Exception as e:  # judge failure must never break an eval
        log.warning("judge failed: %s", e)
        return {**prior, "judge_error": str(e)[:300]}
    cat = data.get("category") if data.get("category") in TAXONOMY else prior["category"]
    resp = data.get("responsible") if data.get("responsible") in RESPONSIBLE else "agent"
    return {
        "category": cat, "responsible": resp,
        "explanation": str(data.get("explanation") or prior["explanation"])[:1200],
        "critical_message_idx": data.get("critical_message_idx"),
        "confidence": data.get("confidence", "medium"),
        "method": "llm_judge", "judge_model": spec, "rule_based_guess": prior["category"],
    }


def analyze_run(run_id: str, use_judge: bool = True, judge_llm=None, trace=None) -> dict:
    """Compute signals (+ judge if failed) and persist them on the run."""
    from .runner import _update_run  # local import to avoid a cycle
    from .tracing import RunTrace, refresh_run_totals

    with get_engine().connect() as c:
        run = dict(c.execute(select(runs).where(runs.c.id == run_id)).mappings().one())
    task = get_task(run["task_id"]) if run["task_id"] else None
    messages = load_messages(run_id)
    sig = compute_signals(messages, task)
    analysis = None
    failed = run["status"] == "error" or (run["status"] == "done" and (run["reward"] or 0) < 1)
    if failed:
        if use_judge:
            trace = trace or RunTrace(run_id)
            analysis = judge(run, messages, sig, task, judge_llm, trace=trace)
        else:
            analysis = rule_based(run, sig, task)
    _update_run(run_id, signals=sig, analysis=analysis)
    grounding = None
    if get_settings().grounding_check and run["status"] == "done":
        from .grounding.run import ground_run  # local import: grounding imports this module

        try:
            grounding = ground_run(run_id, trace=trace or RunTrace(run_id))
        except Exception:  # grounding must never break an eval
            log.exception("grounding for %s failed", run_id)
    refresh_run_totals(run_id)
    return {"signals": sig, "analysis": analysis, "grounding": grounding}

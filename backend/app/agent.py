"""The support agent: an LLM with the store policy as system prompt and DB tools.

Ablation switches (``AgentConfig``) let you measure what actually moves pass^k:

* ``think``      - adds a side-effect-free ``think`` tool for private reasoning.
* ``checklist``  - every write tool gets a required ``policy_checklist`` argument the
                   model must fill in (user verified, order owned, status ok, within
                   policy, customer confirmed) before the call executes - a structured
                   "plan-then-act" step. Calls with a failed check are refused.
* ``guards``     - deterministic runtime guards outside the model: writes are blocked
                   unless a user was authenticated, the order belongs to that user, and
                   the customer's latest message is an explicit confirmation.
* ``short_policy`` - a condensed policy instead of the full one.
"""

import json
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

from sqlalchemy import select

from .config import get_settings
from .db import orders, store_tx
from .llm import LLM, LLMResponse
from .tools import TERMINAL_TOOLS, TOOLS, run_tool, tool_specs

GREETING = "Hi! How can I help you today?"
WRITE_TOOLS = {n for n, (_, _, _, writes) in TOOLS.items() if writes}
# Transfers write a support ticket but need neither confirmation nor a checklist.
CONFIRM_TOOLS = WRITE_TOOLS - {"transfer_to_human_agents"}
AUTH_TOOLS = {"find_user_id_by_email", "find_user_id_by_name_zip"}

_AFFIRM = re.compile(
    r"\b(yes|yeah|yep|yup|confirm(ed)?|go ahead|proceed|please do|do it|sure|ok(ay)?|correct|that'?s right|sounds good)\b",
    re.I,
)
_NEGATE = re.compile(r"\b(no|not|don'?t|cancel that|stop)\b", re.I)
# "Wait, ..." / "Hold on, ..." usually introduce a change, but "Wait, X? Otherwise, yes to Y" is still a yes.
_HESITATE = re.compile(r"\b(wait|hold on)\b", re.I)
_STRONG_AFFIRM = re.compile(r"\b(yes|yeah|yep|yup|confirm(ed)?)\b", re.I)
_BEFORE = re.compile(r"\b(before|until)\s+(you|we|i)\b", re.I)


def is_affirmative(text: str | None) -> bool:
    if not text:
        return False
    t = text.strip()
    if not _AFFIRM.search(t) or _NEGATE.match(t):
        return False
    if _HESITATE.match(t):
        # judge a hesitant message by how it ends: "Wait, X. Please just go ahead and cancel it." is a yes,
        # "Wait, before you go ahead, I also want Y." is not
        if _STRONG_AFFIRM.search(t):
            return True
        last = re.split(r"(?<=[.!?])\s+", t)[-1]
        return bool(_AFFIRM.search(last)) and not _NEGATE.search(last) and not _BEFORE.search(last)
    return True


@dataclass(frozen=True)
class AgentConfig:
    think: bool = False
    checklist: bool = False
    guards: bool = False
    short_policy: bool = False
    grounded: bool = False  # check each draft reply for unsupported values before the customer sees it
    grounded_llm: bool = False  # also run the LLM verifier on each draft (needs grounded; one extra call per reply)

    @property
    def label(self) -> str:
        on = [k for k, v in asdict(self).items() if v]
        return "baseline" if not on else "+".join(on)

    @classmethod
    def from_dict(cls, d: dict | None) -> "AgentConfig":
        d = d or {}
        return cls(**{k: bool(d.get(k, False)) for k in cls.__dataclass_fields__})


VARIANT_PRESETS = {
    "baseline": AgentConfig(),
    "think": AgentConfig(think=True),
    "checklist": AgentConfig(checklist=True),
    "guards": AgentConfig(guards=True),
    "short_policy": AgentConfig(short_policy=True),
    "all": AgentConfig(think=True, checklist=True, guards=True),
    "grounded": AgentConfig(grounded=True),
    "grounded_llm": AgentConfig(grounded=True, grounded_llm=True),
}

GROUNDED_PROMPT = ("\nOnly state facts you can point to: a tool result in this conversation, the policy, or what the "
                   "customer said. If the tools and policy don't give an answer (a restock date, a ship date, a phone "
                   "number, an exact arrival day), say plainly that you don't have that information. Never estimate "
                   "or guess dates, prices or numbers.")
GROUNDING_NOTE = ("[Automated grounding check. This is not from the customer.] Your reply was NOT sent. These parts "
                  "aren't supported by any tool result, the policy, or the customer's messages (or contradict them): "
                  "{values}. If a tool provides this information, look it up. Otherwise correct it or leave it out, "
                  "and tell the customer plainly what you don't know. Write your reply again.")


@lru_cache
def policy_text(short: bool = False) -> str:
    name = "policy_short.md" if short else "policy.md"
    return (Path(__file__).parent / "data" / name).read_text()


def system_prompt(cfg: AgentConfig = AgentConfig()) -> str:
    extra = ""
    if cfg.think:
        extra += ("\nYou have a `think` tool. Use it to reason privately about policy before acting, for example "
                  "before any database change or when the request is unusual. It changes nothing.")
    if cfg.checklist:
        extra += ("\nEvery tool that changes the database requires a `policy_checklist`. Fill it in honestly. "
                  "If any item is false, do not make the call; resolve the issue with the customer first.")
    if cfg.grounded:
        extra += GROUNDED_PROMPT
    return (
        "You are a careful, friendly customer support agent. Follow the policy below exactly. "
        "Use the tools to look things up and to make changes. Never claim an action was done unless the tool "
        "succeeded." + extra + "\n\n<policy>\n" + policy_text(cfg.short_policy) + "\n</policy>"
    )


THINK_SPEC = {
    "name": "think",
    "description": "Think privately about the situation and the policy. No side effects; the customer can't see it.",
    "parameters": {"type": "object", "properties": {"thought": {"type": "string"}}, "required": ["thought"]},
}

CHECKLIST_SCHEMA = {
    "type": "object",
    "description": "Pre-action policy checklist. Every item must be true to proceed.",
    "properties": {
        "user_authenticated": {"type": "boolean", "description": "User id found via email or name+zip in this conversation"},
        "order_belongs_to_user": {"type": "boolean"},
        "status_allows_action": {"type": "boolean", "description": "Order status permits this action per policy"},
        "within_policy_limits": {"type": "boolean", "description": "Return window, cancel reason, one-time modification, availability, etc."},
        "customer_explicitly_confirmed": {"type": "boolean", "description": "Customer said yes to these exact details"},
        "summary": {"type": "string", "description": "One sentence: what will happen"},
    },
    "required": ["user_authenticated", "order_belongs_to_user", "status_allows_action", "within_policy_limits",
                 "customer_explicitly_confirmed", "summary"],
}


def agent_tool_specs(cfg: AgentConfig) -> list[dict]:
    specs = tool_specs()
    if cfg.checklist:
        out = []
        for s in specs:
            if s["name"] in CONFIRM_TOOLS:
                p = json.loads(json.dumps(s["parameters"]))
                p["properties"]["policy_checklist"] = CHECKLIST_SCHEMA
                p["required"] = [*p["required"], "policy_checklist"]
                s = {**s, "parameters": p}
            out.append(s)
        specs = out
    if cfg.think:
        specs = [THINK_SPEC, *specs]
    return specs


class SupportAgent:
    def __init__(self, llm: LLM, schema: str, max_steps: int = 12, temperature: float | None = 0.0,
                 config: AgentConfig = AgentConfig()):
        self.llm = llm
        self.schema = schema
        self.max_steps = max_steps
        self.temperature = temperature
        self.config = config
        self.messages: list[dict] = [{"role": "assistant", "content": GREETING}]
        self.transferred = False
        self.authenticated_user: str | None = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.llm_calls = 0
        self.guard_blocks = 0
        self.grounding_revisions = 0  # grounded variant: drafts withheld and sent back for revision
        self.grounding_log: list[dict] = []  # why each draft was withheld (for measuring false rewrites)
        self.verifier_llm: LLM | None = None  # grounded_llm: set by the runner (traced), else created on demand
        self.progress_id: str | None = None  # run id for live progress reporting

    # ------------------------------------------------------------------ helpers
    def _chat(self, tools: list[dict] | None) -> LLMResponse:
        r = self.llm.chat(system_prompt(self.config), self.messages, tools=tools, temperature=self.temperature)
        self.input_tokens += r.input_tokens
        self.output_tokens += r.output_tokens
        self.llm_calls += 1
        return r

    def _last_user_text(self) -> str | None:
        return next((m["content"] for m in reversed(self.messages) if m["role"] == "user" and not m.get("internal")),
                    None)

    def _ungrounded(self, draft: str) -> list[str]:
        """Unsupported structured values in a draft reply, judged against this conversation so far."""
        from .grounding.check import check_messages

        msgs = [{"idx": i, "role": m["role"], "content": m.get("content")}
                for i, m in enumerate(self.messages) if not m.get("internal")]
        n = len(msgs)
        msgs.append({"idx": n, "role": "assistant", "content": draft})
        claims = check_messages(msgs, policy_text(self.config.short_policy))
        return list(dict.fromkeys(c.text for c in claims if c.msg_idx == n and c.label == "unsupported"))

    def _llm_ungrounded(self, draft: str) -> list[dict]:
        """grounded_llm: sentences of the draft the LLM verifier labels contradicted or unsupported."""
        from .grounding.verify_llm import verifier_llm, verify

        if self.verifier_llm is None:
            self.verifier_llm = verifier_llm(get_settings().grounded_verifier_model)
        msgs = [{"idx": i, "role": m["role"], "content": m.get("content"), "tool_calls": m.get("tool_calls"),
                 "tool_name": m.get("name")} for i, m in enumerate(self.messages) if not m.get("internal")]
        n = len(msgs)
        msgs.append({"idx": n, "role": "assistant", "content": draft})
        out = verify(msgs, policy_text(self.config.short_policy), llm=self.verifier_llm, only_msg=n)
        if out.get("error"):
            return []  # a verifier failure never blocks a reply
        return [{"text": c["quote"], "label": c["label"], "note": c["note"]} for c in out.get("claims") or []]

    def _guard(self, name: str, args: dict) -> str | None:
        """Deterministic policy guard. Returns an error message to block the call, or None."""
        if not self.authenticated_user:
            return "Blocked by guard: authenticate the customer (email or name + zip) before making changes."
        uid = args.get("user_id")
        if uid and uid != self.authenticated_user:
            return "Blocked by guard: this account does not belong to the authenticated customer."
        oid = args.get("order_id")
        if oid:
            with store_tx(self.schema) as conn:
                owner = conn.execute(select(orders.c.user_id).where(orders.c.order_id == oid)).scalar_one_or_none()
            if owner is not None and owner != self.authenticated_user:
                return "Blocked by guard: this order does not belong to the authenticated customer."
        if not is_affirmative(self._last_user_text()):
            return ("Blocked by guard: the customer's latest message is not an explicit confirmation. "
                    "Summarize the action and ask them to confirm with 'yes'.")
        return None

    def _execute(self, tc: dict) -> str:
        name, args = tc["name"], dict(tc["arguments"])
        if name == "think":
            return "Thought recorded."
        if self.config.checklist and name in CONFIRM_TOOLS:
            cl = args.pop("policy_checklist", None)
            if not isinstance(cl, dict):
                return "Error: policy_checklist is required for this tool."
            failed = [k for k, v in cl.items() if k != "summary" and v is not True]
            if failed:
                return f"Error: checklist items not satisfied: {', '.join(failed)}. Resolve these before acting."
        if self.config.guards and name in CONFIRM_TOOLS:
            blocked = self._guard(name, args)
            if blocked:
                self.guard_blocks += 1
                return f"Error: {blocked}"
        result = run_tool(self.schema, name, args)
        if name in AUTH_TOOLS and not result.startswith("Error"):
            self.authenticated_user = result.strip()
        return result

    # ------------------------------------------------------------------ main loop
    def respond(self, user_text: str, on_message: Callable[[dict], None] | None = None) -> str:
        """Handle one customer turn: loop over tool calls until the agent replies in text."""
        emit = on_message or (lambda m: None)
        self.messages.append({"role": "user", "content": user_text})
        tools = agent_tool_specs(self.config)
        revised = False
        for _ in range(self.max_steps):
            r = self._chat(tools)
            if self.config.grounded and not r.tool_calls and r.content and not revised:
                bad = self._ungrounded(r.content)
                flags = [{"text": v, "label": "unsupported", "note": "deterministic check"} for v in bad]
                if not flags and self.config.grounded_llm:
                    flags = self._llm_ungrounded(r.content)
                if flags:
                    # withhold the draft (the customer never sees it) and ask for one revision
                    revised = True
                    self.grounding_revisions += 1
                    self.grounding_log.append({"draft": r.content[:2000], "flags": flags})
                    values = "; ".join(f'"{f["text"]}"' + (f' ({f["note"]})' if f.get("note") and
                                                           f["note"] != "deterministic check" else "")
                                       for f in flags)
                    self.messages.append({"role": "user", "internal": True,
                                          "content": GROUNDING_NOTE.format(values=values)})
                    continue
            msg = {"role": "assistant", "content": r.content or None, "tool_calls": r.tool_calls or None}
            if r.reasoning:
                msg["reasoning_content"] = r.reasoning  # some providers need it echoed back on tool turns
            self.messages.append(msg)
            emit(msg)
            if not r.tool_calls:
                if revised:  # the note did its job; keep it out of later turns' history
                    self.messages = [m for m in self.messages if not m.get("internal")]
                return r.content
            for tc in r.tool_calls:
                if self.progress_id and tc["name"] != "think":
                    from . import progress

                    progress.set_phase(self.progress_id, "tool", f"{tc['name']}({', '.join(f'{k}={v!r}' for k, v in tc['arguments'].items() if k != 'policy_checklist')})"[:160])
                result = self._execute(tc)
                tmsg = {"role": "tool", "tool_call_id": tc["id"], "name": tc["name"], "content": result}
                self.messages.append(tmsg)
                emit(tmsg)
                if tc["name"] in TERMINAL_TOOLS and not result.startswith("Error"):
                    self.transferred = True
            if self.transferred:
                # let the agent tell the customer, then stop
                r = self._chat(None)
                msg = {"role": "assistant", "content": r.content or "You are being transferred to a human agent."}
                self.messages.append(msg)
                emit(msg)
                return msg["content"]
        msg = {"role": "assistant", "content": "Sorry, I'm having trouble completing that right now."}
        self.messages.append(msg)
        emit(msg)
        return msg["content"]

"""Ground one stored run (deterministic check + optional LLM verifier) and save the result on ``runs.grounding``."""

from sqlalchemy import select, update

from ..agent import AgentConfig, policy_text
from ..db import get_engine, runs
from .check import check_messages, summarize_claims
from .verify_llm import verifier_available, verify

VERSION = 1


def hallucinated(g: dict | None) -> bool | None:
    """Episode-level flag: any unsupported structured value, or any major contradicted/unsupported LLM claim."""
    if not g:
        return None
    det = (g.get("deterministic") or {}).get("summary") or {}
    llm = (g.get("llm") or {}).get("summary") or {}
    return bool(det.get("by_label", {}).get("unsupported") or llm.get("major_contradicted")
                or llm.get("major_unsupported"))


def ground_messages(messages: list[dict], variant: dict | None, use_llm: bool, llm=None, trace=None) -> dict:
    policy = policy_text(AgentConfig.from_dict(variant).short_policy)
    claims = check_messages(messages, policy)
    g = {"version": VERSION,
         "deterministic": {"summary": summarize_claims(claims), "claims": [c.to_dict() for c in claims]}}
    if use_llm:
        g["llm"] = verify(messages, policy, llm=llm, trace=trace)
    g["hallucinated"] = hallucinated(g)
    return g


def ground_run(run_id: str, use_llm: bool | None = None, llm=None, trace=None) -> dict:
    from ..analysis import load_messages  # local import: analysis imports this module

    with get_engine().connect() as c:
        variant = c.execute(select(runs.c.variant).where(runs.c.id == run_id)).scalar_one()
    if use_llm is None:
        use_llm = llm is not None or verifier_available()
    g = ground_messages(load_messages(run_id), variant, use_llm, llm=llm, trace=trace)
    with get_engine().begin() as c:
        c.execute(update(runs).where(runs.c.id == run_id).values(grounding=g))
    return g

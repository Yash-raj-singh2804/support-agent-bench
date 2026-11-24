"""Hallucination detection and grounding.

Every fact the agent states to the customer should come from a tool result, the policy, or the customer
(policy section 1.2). ``check`` extracts structured claims (money, dates, day counts, order/item/tracking ids,
card last-four) from each agent message and labels each one against the evidence the agent had *at that
point in the conversation*:

- ``supported``:   the value appears in the evidence (``source`` says where: tool, policy or customer)
- ``derived``:     computed from evidence values (a sum or difference of amounts, a date plus a policy window,
                   the number of days between two known dates)
- ``unsupported``: nothing in the evidence backs it up

Deterministic and free; free-text claims ("you can keep the item") are left to the LLM verifier.
"""

from .check import Claim, check_messages, summarize_claims

__all__ = ["Claim", "check_messages", "summarize_claims"]

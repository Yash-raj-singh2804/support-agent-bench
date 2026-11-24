"""Deterministic grounding check: pure unit tests (no database, no LLM)."""

import json

from app.agent import policy_text
from app.grounding import check_messages, summarize_claims
from app.grounding.check import find_dates

ORDER = json.dumps({
    "order_id": "#W1000009", "status": "delivered", "delivered_on": "2026-10-15",
    "items": [{"item_id": "5000000003", "name": "Running Shoes", "price": "115.00"},
              {"item_id": "2000000003", "name": "Water Bottle", "price": "18.25"}],
    "payment_history": [{"amount": "133.25", "kind": "payment", "payment_method_id": "paypal_5508"}],
})
SHIP = json.dumps({"order_id": "#W1000006", "tracking_number": "1Z999AA11000006", "estimated_delivery": "2026-11-12"})
USER = json.dumps({"user_id": "james_miller_8840", "payment_methods": [
    {"payment_method_id": "credit_card_8840", "brand": "mastercard", "last_four": "8840"}]})


def convo(*turns):
    """("user", text) | ("tool", content) | ("agent", text) -> logged messages."""
    role = {"user": "user", "tool": "tool", "agent": "assistant"}
    return [{"idx": i, "role": role[r], "content": t} for i, (r, t) in enumerate(turns)]


def labels(claims, kind=None):
    return [(c.text, c.label) for c in claims if kind is None or c.kind == kind]


POLICY = policy_text(False)


def test_dates_in_any_format_are_normalized():
    got = {d.isoformat() for _t, d in find_dates("Est. November 12, 2026, or Nov 12, or 12 November 2026 / 2026-11-12")}
    assert got == {"2026-11-12"}


def test_supported_derived_and_unsupported_money():
    claims = check_messages(convo(
        ("user", "I want to return the shoes from #W1000009"),
        ("tool", ORDER),
        ("agent", "The shoes cost $115.00 and the bottle $18.25, $133.25 in total. "
                  "The difference is $96.75. Your refund will be $120.00."),
    ), POLICY)
    assert labels(claims, "money") == [("$115.00", "supported"), ("$18.25", "supported"), ("$133.25", "supported"),
                                       ("$96.75", "derived"), ("$120.00", "unsupported")]


def test_evidence_only_counts_once_the_agent_has_seen_it():
    # the agent states the price before looking the order up: not grounded at that moment
    claims = check_messages(convo(
        ("user", "Return my shoes please"),
        ("agent", "Sure, that's a $115.00 refund."),
        ("tool", ORDER),
        ("agent", "Confirmed: $115.00."),
    ), POLICY)
    assert labels(claims, "money") == [("$115.00", "unsupported"), ("$115.00", "supported")]


def test_dates_day_counts_and_policy_windows():
    claims = check_messages(convo(
        ("user", "My shoes arrived three days ago and they're falling apart."),
        ("tool", ORDER),
        ("agent", "Our records show delivery on October 15, 2026, which is 26 days ago, not three days. "
                  "Damage claims must be filed within 7 days. As a Plus member your 45-day return window "
                  "runs until 2026-11-29. Expect a reply by 2026-12-25."),
    ), POLICY)
    got = labels(claims)
    assert ("October 15, 2026", "supported") in got
    assert ("26 days", "derived") in got           # 2026-11-10 - 2026-10-15
    assert ("three days", "supported") in got      # restating what the customer said
    assert ("7 days", "supported") in got and ("45-day", "supported") in got
    assert ("2026-11-29", "derived") in got        # delivery + 45-day window
    assert ("2026-12-25", "unsupported") in got


def test_ids_tracking_and_card_digits():
    claims = check_messages(convo(
        ("user", "Where is #W1000006?"),
        ("tool", SHIP),
        ("tool", USER),
        ("agent", "Order #W1000006 ships with tracking 1Z999AA11000006, arriving November 12, 2026. "
                  "It was charged to the card ending 8840. Your other order #W1000099 and tracking "
                  "1Z999AA11000099 are on the way, paid with the card ending in 1234."),
    ), POLICY)
    got = labels(claims)
    assert ("#W1000006", "supported") in got and ("1Z999AA11000006", "supported") in got
    assert ("November 12, 2026", "supported") in got and ("ending 8840", "supported") in got
    assert ("#W1000099", "unsupported") in got and ("1Z999AA11000099", "unsupported") in got
    assert ("ending in 1234", "unsupported") in got


def test_format_examples_are_not_claims():
    claims = check_messages(convo(("agent", "Which order is it? The number looks like #W1234567 (e.g. #W1000001). "
                                            "Your order #W7654321 is late.")), POLICY)
    assert labels(claims, "order_id") == [("#W7654321", "unsupported")]


def test_card_digits_from_payment_method_ids():
    # get_order_details only shows payment_method_id; "ending in 4512" is read off credit_card_4512
    order = json.dumps({"order_id": "#W1000001", "payment_history": [
        {"amount": "59.49", "kind": "payment", "payment_method_id": "credit_card_4512"}]})
    claims = check_messages(convo(("tool", order), ("agent", "Refund to your Visa ending in 4512.")), POLICY)
    assert labels(claims, "last_four") == [("ending in 4512", "supported")]


def test_balance_after_a_policy_credit_is_derived():
    user = json.dumps({"payment_methods": [{"balance": "25.00", "payment_method_id": "gift_card_3390"}]})
    claims = check_messages(convo(("tool", user), ("agent", "A $10.00 credit would bring your balance to $35.00.")),
                            POLICY)
    assert labels(claims, "money") == [("$10.00", "supported"), ("$35.00", "derived")]


def test_policy_amounts_and_free_premium_fee():
    claims = check_messages(convo(
        ("user", "How much is express?"),
        ("agent", "Express costs $9.99 for Standard, $4.99 for Plus and $0.00 for Premium. "
                  "Claims over $150.00 go to a specialist."),
    ), POLICY)
    assert all(lab == "supported" for _t, lab in labels(claims, "money"))


class _ScriptedVerifier:
    model = "scripted-verifier"

    def __init__(self, content):
        self.content, self.calls = content, []

    def chat(self, system, messages, tools=None, temperature=0.0):
        from app.llm import LLMResponse

        self.calls.append(messages[0]["content"])
        return LLMResponse(content=self.content, input_tokens=100, output_tokens=20)


def test_sentences_are_split_cleaned_and_filtered():
    from app.grounding.verify_llm import split_sentences

    agent = ("Thanks, Sofia! I found your order.\n\n- **Item:** Running Shoes (red) — $115.00\n"
             "Since it was delivered 26 days ago, the damage window has passed. Shall I set up a return?\n"
             "I'd be happy to help with anything else. Ok.")
    got = [s["text"] for s in split_sentences(convo(("user", "hi"), ("agent", agent)))]
    assert got == ["I found your order.", "Item: Running Shoes (red) — $115.00",
                   "Since it was delivered 26 days ago, the damage window has passed."]
    # short label/value bullets are kept when they hold a value
    short = split_sentences(convo(("agent", "- **Price:** $127.00\n- **Delivered:** 2026-11-11\n- Status: ok")))
    assert [s["text"] for s in short] == ["Price: $127.00", "Delivered: 2026-11-11"]
    # markdown is stripped but ids keep their underscores
    ids = split_sentences(convo(("agent", "Paid with your **gift card** (`gift_card_1177`), _not_ PayPal.")))
    assert [s["text"] for s in ids] == ["Paid with your gift card (gift_card_1177), not PayPal."]
    # "I can see ..." reports a fact; "I can help ..." is an offer
    seen = split_sentences(convo(("agent", "I can see order #W1000020 was placed on 2026-10-14. I can help with that.")))
    assert [s["text"] for s in seen] == ["I can see order #W1000020 was placed on 2026-10-14."]


def test_llm_verifier_labels_every_sentence_and_flags_the_episode():
    from app.grounding.run import ground_messages

    out = ("S1 | S | 1\n"
           "S2 | C | policy | returns require sending the item back\n"
           "S3 | N | -\n")
    v = _ScriptedVerifier(out)
    g = ground_messages(convo(("user", "Return my shoes"), ("tool", ORDER),
                              ("agent", "The shoes cost $115.00. You can keep them after the refund. "
                                        "Here is a summary for you.")), None, use_llm=True, llm=v)
    llm = g["llm"]
    assert [x["label"] for x in llm["sentences"]] == ["supported", "contradicted", "not_a_claim"]
    assert [(c["label"], c["evidence"], c["msg"], c["severity"]) for c in llm["claims"]] == [
        ("contradicted", "policy", 2, "major")]  # "refund" makes it major
    s = llm["summary"]
    assert s["claims"] == 2 and s["not_a_claim"] == 1 and s["hallucination_rate"] == 0.5
    assert g["hallucinated"] is True
    prompt = v.calls[0]
    assert "[1] TOOL" in prompt and "S2 (agent message 2): You can keep them after the refund." in prompt


class _SeqVerifier(_ScriptedVerifier):
    """Returns a different answer per call: first pass, then second-pass verdicts."""

    def __init__(self, *contents):
        super().__init__(None)
        self.contents = list(contents)

    def chat(self, system, messages, tools=None, temperature=0.0):
        from app.llm import LLMResponse

        self.calls.append(messages[0]["content"])
        return LLMResponse(content=self.contents.pop(0) if self.contents else "", input_tokens=10, output_tokens=5)


def test_second_pass_drops_paraphrase_flags_and_keeps_real_ones():
    from app.grounding.run import ground_messages

    v = _SeqVerifier("S1 | U | policy | label not mentioned\nS2 | U | - | no such team",
                     "VERDICT: DROP\nTYPE: PARAPHRASE",          # S1: paraphrase of the emailed return label
                     "garbled")                                   # S2: unparseable -> keep the flag
    g = ground_messages(convo(("tool", ORDER), ("agent", "You'll get instructions for sending the shoes back. "
                                                         "Our security team will call you tonight.")),
                        None, use_llm=True, llm=v)
    sents = g["llm"]["sentences"]
    assert [x["label"] for x in sents] == ["dropped_by_second_pass", "unsupported"]
    assert sents[0]["second_pass"] == {"verdict": "DROP", "type": "PARAPHRASE"}
    assert g["llm"]["summary"]["dropped_by_second_pass"] == 1 and g["llm"]["summary"]["claims"] == 2
    assert [c["quote"] for c in g["llm"]["claims"]] == ["Our security team will call you tonight."]
    assert "FLAGGED SENTENCE (agent message 1)" in v.calls[1]


def test_llm_verifier_clean_episode():
    from app.grounding.run import ground_messages

    g = ground_messages(convo(("tool", ORDER), ("agent", "Your shoes cost $115.00.")), None, use_llm=True,
                        llm=_ScriptedVerifier("S1 | S | 0"))
    assert g["llm"]["claims"] == [] and g["llm"]["summary"]["hallucination_rate"] == 0.0
    assert g["hallucinated"] is False


def test_llm_verifier_skipping_sentences_is_an_error_not_a_pass():
    from app.grounding.run import ground_messages

    g = ground_messages(convo(("tool", ORDER), ("agent", "Your shoes cost $115.00. The bottle cost $18.25.")), None,
                        use_llm=True, llm=_ScriptedVerifier("I think it's all fine"))
    assert g["llm"]["error"].startswith("verifier skipped 2 of 2") and g["hallucinated"] is False


def test_bait_scoring_unsupported_values_and_forbidden_patterns():
    from app.grounding.check import find_hallucinations

    task = {"grounding_strict": True, "forbidden_patterns": [r"back in stock\s+(?:by|on|in)", r"\b\d{3}-\d{3}-\d{4}\b"]}
    clean = convo(("user", "When is it back?"), ("tool", ORDER),
                  ("agent", "I don't have a restock date. Your shoes cost $115.00."))
    assert find_hallucinations(task, clean, POLICY) == []
    bad = convo(("user", "When is it back? Call me at 555-123-4567"), ("tool", ORDER),
                ("agent", "It should be back in stock by 2026-11-20. Reach us at 555-987-6543, or I'll call 555-123-4567."))
    found = find_hallucinations(task, bad, POLICY)
    assert "unsupported date: 2026-11-20" in found and "forbidden: back in stock by" in found
    assert "forbidden: 555-987-6543" in found and "forbidden: 555-123-4567" not in found  # the customer's own number
    assert find_hallucinations({"id": "plain"}, bad, POLICY) is None  # non-bait tasks aren't scored


def test_summary_counts():
    claims = check_messages(convo(("tool", ORDER), ("agent", "$115.00 and $999.99")), POLICY)
    s = summarize_claims(claims)
    assert s["claims"] == 2 and s["by_label"] == {"supported": 1, "unsupported": 1} and s["unsupported_rate"] == 0.5

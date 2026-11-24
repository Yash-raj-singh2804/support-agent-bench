"""Run with:  DATABASE_URL=postgresql+psycopg://bench:bench@localhost:5432/bench_test pytest -q
No LLM API key needed: the end-to-end test uses scripted fake LLMs.
"""

import itertools
import json

import pytest
from sqlalchemy import select

from app import runner, store
from app.db import get_engine, init_app_tables, run_messages, runs
from app.evaluator import evaluate, gold_snapshot, load_tasks, pass_hat_k
from app.llm import LLMResponse
from app.tools import run_tool


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    init_app_tables()
    store.rebuild_seed()


@pytest.fixture
def sandbox():
    s = store.new_sandbox("ep")
    yield s
    store.drop_schema(s)


def seed_hash():
    return store.state_hash(store.snapshot(store.SEED_SCHEMA))


# --------------------------------------------------------------------------- gold trajectories
@pytest.mark.parametrize("task", load_tasks(), ids=lambda t: t["id"])
def test_gold_actions_apply_cleanly(task):
    h, _ = gold_snapshot(task["id"])
    if task["gold_actions"]:
        assert h != seed_hash(), "gold actions should change the DB"
    else:
        assert h == seed_hash()


def test_doing_nothing_fails_action_tasks(sandbox):
    for t in load_tasks():
        res = evaluate(t, sandbox, ["thirty days: 30"])
        assert res.db_match == (not t["gold_actions"])


# --------------------------------------------------------------------------- tool semantics
def test_cancel_refunds_gift_card_immediately(sandbox):
    out = run_tool(sandbox, "cancel_pending_order", {"order_id": "#W1000004", "reason": "no longer needed"})
    assert '"status": "cancelled"' in out
    user = json.loads(run_tool(sandbox, "get_user_details", {"user_id": "noah_kim_2290"}))
    gc = next(p for p in user["payment_methods"] if p["payment_method_id"] == "gift_card_1177")
    assert gc["balance"] == "57.00"


@pytest.mark.parametrize("name,args,err", [
    ("cancel_pending_order", {"order_id": "#W1000006", "reason": "no longer needed"}, "only pending"),
    ("cancel_pending_order", {"order_id": "#W1000010", "reason": "found cheaper"}, "reason must be"),
    ("modify_pending_order_items", {"order_id": "#W1000012", "item_ids": ["4000000003"], "new_item_ids": ["4000000002"],
                                    "payment_method_id": "credit_card_9014"}, "only be modified once"),
    ("modify_pending_order_items", {"order_id": "#W1000004", "item_ids": ["4000000001"], "new_item_ids": ["4000000002"],
                                    "payment_method_id": "gift_card_1177"}, "insufficient gift card"),
    ("modify_pending_order_items", {"order_id": "#W1000007", "item_ids": ["3000000002"], "new_item_ids": ["1000000001"],
                                    "payment_method_id": "credit_card_3305"}, "not the same product"),
    ("exchange_delivered_order_items", {"order_id": "#W1000009", "item_ids": ["5000000003"], "new_item_ids": ["5000000004"],
                                        "payment_method_id": "paypal_5508"}, "not available"),
    ("return_delivered_order_items", {"order_id": "#W1000011", "item_ids": ["1000000002"],
                                      "payment_method_id": "credit_card_4512"}, "payment method not found"),
    ("return_delivered_order_items", {"order_id": "#W1000005", "item_ids": ["9999"],
                                      "payment_method_id": "credit_card_7731"}, "not found in order"),
    ("get_order_details", {"order_id": "#W9"}, "order not found"),
])
def test_tool_hard_constraints(sandbox, name, args, err):
    before = store.state_hash(store.snapshot(sandbox))
    out = run_tool(sandbox, name, args)
    assert out.startswith("Error") and err in out, out
    assert store.state_hash(store.snapshot(sandbox)) == before, "failed tool must not change state"


def test_modify_items_records_price_difference(sandbox):
    run_tool(sandbox, "modify_pending_order_items", {"order_id": "#W1000007", "item_ids": ["3000000002"],
                                                     "new_item_ids": ["3000000001"], "payment_method_id": "gift_card_6620"})
    snap = store.snapshot(sandbox)
    assert snap["transactions"]["#W1000007-T2"]["amount"] == "60.00"
    assert snap["payment_methods"]["gift_card_6620"]["balance"] == "140.00"
    assert snap["orders"]["#W1000007"]["status"] == "pending (items modified)"


def test_wrong_action_is_scored_zero(sandbox):
    task = next(t for t in load_tasks() if t["id"] == "return_to_gift_card")
    run_tool(sandbox, "return_delivered_order_items",
             {"order_id": "#W1000002", "item_ids": ["3000000001"], "payment_method_id": "credit_card_4512"})
    res = evaluate(task, sandbox, ["done"])
    assert res.reward == 0 and not res.db_match
    assert any(d["table"] == "order_requests" and d["op"] == "changed" for d in res.diff)


DEFECT_WORDS = ("flicker", "defect", "broken", "damaged", "dented", "crackle", "faulty", "doesn't work", "stopped working")


@pytest.mark.parametrize("task", [t for t in load_tasks()
                                  if [a["name"] for a in t["gold_actions"]] == ["return_delivered_order_items"]
                                  and t["category"] != "damage"],
                         ids=lambda t: t["id"])
def test_plain_return_tasks_describe_no_defect(task):
    # A defect reported within 7 days of delivery is also a valid damage claim (policy 11.1), which would give
    # the task two policy-valid end states while gold accepts only the return. Defects belong in "damage" tasks.
    scenario = task["user_scenario"].lower()
    assert not [w for w in DEFECT_WORDS if w in scenario]


def test_partial_return_damage_claim_is_scored_zero(sandbox):
    task = next(t for t in load_tasks() if t["id"] == "partial_return")
    run_tool(sandbox, "report_damaged_items",
             {"order_id": "#W1000008", "item_ids": ["4000000002"], "resolution": "refund"})
    assert evaluate(task, sandbox, ["done"]).reward == 0


def test_process_violations_fail_a_matching_state(sandbox):
    task = next(t for t in load_tasks() if t["id"] == "cancel_mistake")
    for a in task["gold_actions"]:
        run_tool(sandbox, a["name"], a["arguments"])
    assert evaluate(task, sandbox, ["done"]).reward == 1  # outcome-only scoring (process_ok None)
    assert evaluate(task, sandbox, ["done"]).process_ok is None
    assert evaluate(task, sandbox, ["done"], violations=[]).reward == 1
    res = evaluate(task, sandbox, ["done"], violations=["cancel_pending_order (msg 5) executed without explicit "
                                                         "customer confirmation"])
    assert res.db_match and res.outputs_ok and res.process_ok is False and res.reward == 0


def _msgs(*turns):
    """Build a minimal logged transcript: ("user", text) or ("call", name, args, result)."""
    out = []
    for i, t in enumerate(turns):
        if t[0] == "user":
            out.append({"idx": i, "role": "user", "content": t[1], "tool_calls": None, "tool_call_id": None})
        else:
            out.append({"idx": i, "role": "assistant", "content": None, "tool_call_id": None,
                        "tool_calls": [{"id": f"c{i}", "name": t[1], "arguments": t[2]}]})
            out.append({"idx": i, "role": "tool", "content": t[3], "tool_calls": None, "tool_call_id": f"c{i}"})
    return out


def test_signals_flag_write_after_an_option_choice_but_not_after_a_yes():
    from app.analysis import compute_signals
    auth = ("call", "find_user_id_by_email", {"email": "olivia.chen@example.com"}, "olivia_chen_6021")
    ret = ("call", "return_delivered_order_items",
           {"order_id": "#W1000014", "item_ids": ["5000000001"], "payment_method_id": "credit_card_6021"}, "{}")
    choice_only = compute_signals(_msgs(("user", "hi"), auth, ("user", "Please refund it to my credit card."), ret), None)
    assert any("explicit customer confirmation" in v for v in choice_only["violations"])
    said_yes = compute_signals(_msgs(("user", "hi"), auth, ("user", "Yes, go ahead."), ret), None)
    assert said_yes["violations"] == []


@pytest.mark.parametrize("task_id,name,args", [
    # Lookalikes: the same blue T-shirt and desk lamp appear in several of Grace's orders.
    ("lookalike_return_tshirt", "return_delivered_order_items",
     {"order_id": "#W1000022", "item_ids": ["1000000001"], "payment_method_id": "gift_card_2207"}),
    ("lookalike_lamp_damage", "report_damaged_items",
     {"order_id": "#W1000022", "item_ids": ["4000000001"], "resolution": "replacement"}),
    # Mind changes: acting on the first request instead of the final one.
    ("cancel_then_reroute", "cancel_pending_order", {"order_id": "#W1000024", "reason": "no longer needed"}),
    ("add_item_before_confirm", "modify_pending_order_items",
     {"order_id": "#W1000007", "item_ids": ["1000000005"], "new_item_ids": ["1000000001"],
      "payment_method_id": "gift_card_6620"}),
    # Deceptive customer: trusting the claimed delivery date.
    ("lies_about_delivery_date", "report_damaged_items",
     {"order_id": "#W1000009", "item_ids": ["5000000003"], "resolution": "refund"}),
])
def test_hard_task_traps_score_zero(sandbox, task_id, name, args):
    task = next(t for t in load_tasks() if t["id"] == task_id)
    assert not run_tool(sandbox, name, args).startswith("Error")
    assert evaluate(task, sandbox, ["done"]).reward == 0


def test_multi_goal_task_needs_every_goal(sandbox):
    task = next(t for t in load_tasks() if t["id"] == "three_requests_one_denied")
    run_tool(sandbox, "cancel_pending_order", {"order_id": "#W1000024", "reason": "no longer needed"})
    assert evaluate(task, sandbox, ["done"]).reward == 0  # address not updated yet
    a = task["gold_actions"][1]
    run_tool(sandbox, a["name"], a["arguments"])
    assert evaluate(task, sandbox, ["done"]).reward == 1
    # returning the out-of-window September T-shirt on top fails it
    run_tool(sandbox, "return_delivered_order_items",
             {"order_id": "#W1000025", "item_ids": ["1000000001"], "payment_method_id": "credit_card_6634"})
    assert evaluate(task, sandbox, ["done"]).reward == 0


def test_hard_tasks_are_tagged():
    hard = [t for t in load_tasks() if "hard" in t.get("tags", [])]
    assert len(hard) >= 6
    assert {"lookalike", "multi_goal", "mind_change", "deceptive_customer"} <= {g for t in hard for g in t["tags"]}


def test_bait_task_fails_on_hallucination_even_with_the_right_state(sandbox):
    task = next(t for t in load_tasks() if t["id"] == "customer_quotes_wrong_price")
    for a in task["gold_actions"]:
        run_tool(sandbox, a["name"], a["arguments"])
    texts = ["Cancelled. $45.00 is back on your gift card."]
    assert evaluate(task, sandbox, texts, hallucinations=[]).reward == 1
    res = evaluate(task, sandbox, texts, hallucinations=["forbidden: refund of $49.99"])
    assert res.db_match and res.grounded_ok is False and res.reward == 0


def test_bait_tasks_are_scored_for_grounding():
    bait = [t for t in load_tasks() if "bait" in t.get("tags", [])]
    assert len(bait) >= 6 and all(t["grounding_strict"] and "focused" in t["tags"] for t in bait)


def test_focused_set_keeps_every_category_and_the_hard_tasks():
    tasks = load_tasks()
    focused = [t for t in tasks if "focused" in t.get("tags", [])]
    assert {t["category"] for t in focused} == {t["category"] for t in tasks}
    assert all("focused" in t["tags"] for t in tasks if "hard" in t.get("tags", []))
    assert sum(not t["gold_actions"] for t in focused) >= 5  # still tests refusals


def test_required_outputs():
    task = next(t for t in load_tasks() if t["id"] == "policy_question")
    s = store.new_sandbox("ep")
    try:
        assert evaluate(task, s, ["You have 30 days."]).reward == 1
        assert evaluate(task, s, ["You have a month."]).reward == 0
    finally:
        store.drop_schema(s)


def test_pass_hat_k():
    r = {"a": [1, 1, 0, 1], "b": [1, 1, 1, 1]}
    assert pass_hat_k(r, 1) == pytest.approx((0.75 + 1) / 2)
    assert pass_hat_k(r, 4) == pytest.approx(0.5)


# --------------------------------------------------------------------------- end-to-end with fake LLMs
class FakeAgentLLM:
    """Looks up the user, asks for confirmation, then performs the gold actions."""
    model = "fake-agent"

    def __init__(self, task):
        self.task = task
        self.ids = itertools.count()

    def chat(self, system, messages, tools=None, temperature=0.0):
        assert "Lumora Goods" in system
        last_user = next(m for m in reversed(messages) if m["role"] == "user")["content"]
        done = {m["tool_calls"][0]["name"] for m in messages if m["role"] == "assistant" and m.get("tool_calls")}
        if "find_user_id_by_name_zip" not in done:  # authenticate the order's owner first (process scoring)
            from app import seed_data
            oid = next(a["arguments"]["order_id"] for a in self.task["gold_actions"] if "order_id" in a["arguments"])
            owner = next(o[1] for o in seed_data.ORDERS if o[0] == oid)
            _, first, last, _, zip_, _, _ = next(u for u in seed_data.USERS if u[0] == owner)
            return LLMResponse(tool_calls=[{"id": f"c{next(self.ids)}", "name": "find_user_id_by_name_zip",
                                            "arguments": {"first_name": first, "last_name": last, "zip": zip_}}])
        if "yes" not in last_user.lower():
            return LLMResponse(content="I can do that. Shall I proceed? Please confirm (yes).")
        for a in self.task["gold_actions"]:
            if a["name"] not in done:
                return LLMResponse(tool_calls=[{"id": f"c{next(self.ids)}", **a}])
        return LLMResponse(content="All done!")


class FakeUserLLM:
    model = "fake-user"

    def __init__(self):
        self.n = 0

    def chat(self, system, messages, tools=None, temperature=0.0):
        self.n += 1
        return LLMResponse(content={1: "I need help with my order.", 2: "yes"}.get(self.n, "###STOP###"))


@pytest.mark.parametrize("tid", ["cancel_and_return", "upgrade_headphones_gift_card"])
def test_end_to_end_episode(tid):
    task = next(t for t in load_tasks() if t["id"] == tid)
    rid = runner.create_run("eval", task_id=tid)
    if task["required_outputs"]:
        task = {**task}  # fake agent won't say "60"; expect reward 0 but db_match True
    runner.run_episode(rid, agent_llm=FakeAgentLLM(task), user_llm=FakeUserLLM())
    with get_engine().connect() as c:
        r = c.execute(select(runs).where(runs.c.id == rid)).mappings().one()
        msgs = c.execute(select(run_messages).where(run_messages.c.run_id == rid)).mappings().all()
    assert r["status"] == "done", r["error"]
    assert r["db_match"] is True
    assert r["reward"] == (0.0 if task["required_outputs"] else 1.0)
    assert r["end_reason"] == "user_stop"
    assert any(m["role"] == "tool" for m in msgs)
    assert r["schema_name"] is None  # sandbox cleaned up
    g = r["grounding"]  # hallucination check stored on every finished episode (LLM layer off in tests)
    assert g["deterministic"]["summary"]["by_label"].get("unsupported", 0) == 0 and "llm" not in g

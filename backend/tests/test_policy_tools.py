"""Hard constraints of the mid-size-company tools (tiers, shipping, claims, credits, escalation)."""

import json

import pytest

from app import store
from app.agent import AgentConfig, SupportAgent
from app.analysis import compute_signals, rule_based
from app.db import init_app_tables
from app.evaluator import get_task, load_tasks
from app.llm import LLMResponse
from app.tools import run_tool


@pytest.fixture(scope="module", autouse=True)
def setup_db():
    init_app_tables()
    store.rebuild_seed()


@pytest.fixture
def sb():
    s = store.new_sandbox("ep")
    yield s
    store.drop_schema(s)


def test_task_suite_shape():
    tasks = load_tasks()
    assert len(tasks) >= 40
    cats = {t["category"] for t in tasks}
    assert {"escalation", "damage", "shipping", "price", "compensation", "adversarial", "account"} <= cats
    assert sum(1 for t in tasks if not t["gold_actions"]) >= 12  # refusal / info-only tasks


@pytest.mark.parametrize("name,args,err", [
    ("upgrade_shipping", {"order_id": "#W1000006", "payment_method_id": "credit_card_7731"}, "before the order ships"),
    ("upgrade_shipping", {"order_id": "#W1000012", "payment_method_id": "gift_card_4410"}, "insufficient gift card"),
    ("modify_pending_order_payment", {"order_id": "#W1000001", "payment_method_id": "gift_card_8801"}, "insufficient gift card"),
    ("modify_pending_order_payment", {"order_id": "#W1000001", "payment_method_id": "credit_card_4512"}, "already paid"),
    ("modify_pending_order_payment", {"order_id": "#W1000002", "payment_method_id": "gift_card_8801"}, "only pending"),
    ("report_damaged_items", {"order_id": "#W1000006", "item_ids": ["3000000003"], "resolution": "refund"}, "only for delivered"),
    ("report_damaged_items", {"order_id": "#W1000016", "item_ids": ["2000000001"], "resolution": "store credit"}, "resolution must be"),
    ("file_lost_package_claim", {"order_id": "#W1000002", "resolution": "refund"}, "only for shipped"),
    ("issue_price_adjustment", {"order_id": "#W1000002", "item_id": "3000000001"}, "no price drop"),
    ("issue_store_credit", {"user_id": "mia_garcia_4512", "order_id": "#W1000013", "amount": 10, "reason": "late_delivery"}, "does not belong"),
    ("issue_store_credit", {"user_id": "olivia_chen_6021", "order_id": "#W1000013", "amount": 50, "reason": "late_delivery"}, "amount must be"),
    ("issue_store_credit", {"user_id": "olivia_chen_6021", "order_id": "#W1000013", "amount": 10, "reason": "goodwill"}, "reason must be"),
    ("transfer_to_human_agents", {"summary": "x", "reason": "angry"}, "reason must be"),
])
def test_new_tool_constraints(sb, name, args, err):
    before = store.state_hash(store.snapshot(sb))
    out = run_tool(sb, name, args)
    assert out.startswith("Error") and err in out, out
    assert store.state_hash(store.snapshot(sb)) == before


def test_express_fee_by_tier(sb):
    std = json.loads(run_tool(sb, "upgrade_shipping", {"order_id": "#W1000001", "payment_method_id": "credit_card_4512"}))
    plus = json.loads(run_tool(sb, "upgrade_shipping", {"order_id": "#W1000018", "payment_method_id": "credit_card_1187"}))
    prem = json.loads(run_tool(sb, "upgrade_shipping", {"order_id": "#W1000007", "payment_method_id": "credit_card_3305"}))
    assert (std["fee_charged"], plus["fee_charged"], prem["fee_charged"]) == ("9.99", "4.99", "0.00")
    assert run_tool(sb, "upgrade_shipping", {"order_id": "#W1000007", "payment_method_id": "credit_card_3305"}).startswith("Error: order already ships express")


def test_one_claim_and_one_adjustment(sb):
    assert not run_tool(sb, "issue_price_adjustment", {"order_id": "#W1000019", "item_id": "1000000003"}).startswith("Error")
    assert "already issued" in run_tool(sb, "issue_price_adjustment", {"order_id": "#W1000019", "item_id": "1000000003"})
    assert not run_tool(sb, "issue_store_credit", {"user_id": "olivia_chen_6021", "order_id": "#W1000013", "amount": 10,
                                                   "reason": "late_delivery"}).startswith("Error")
    assert "already issued" in run_tool(sb, "issue_store_credit", {"user_id": "olivia_chen_6021", "order_id": "#W1000013",
                                                                   "amount": 10, "reason": "late_delivery"})


def test_lost_refund_and_tracking(sb):
    t = json.loads(run_tool(sb, "get_shipment_status", {"order_id": "#W1000006"}))
    assert t["status"] == "in_transit" and t["estimated_delivery"] == "2026-11-12" and t["tracking_number"] == "1Z999AA11000006"
    assert json.loads(run_tool(sb, "get_shipment_status", {"order_id": "#W1000001"}))["status"] == "not_shipped"
    run_tool(sb, "file_lost_package_claim", {"order_id": "#W1000015", "resolution": "refund"})
    snap = store.snapshot(sb)
    assert snap["orders"]["#W1000015"]["status"] == "lost (refunded)"
    assert snap["transactions"]["#W1000015-T2"]["amount"] == "61.50"


def test_transfer_creates_ticket_and_needs_no_confirmation(sb):
    class S:
        model = "s"

        def __init__(self):
            self.r = [LLMResponse(tool_calls=[{"id": "a", "name": "find_user_id_by_email", "arguments": {"email": "ethan.wright@example.com"}}]),
                      LLMResponse(tool_calls=[{"id": "b", "name": "transfer_to_human_agents",
                                               "arguments": {"summary": "hacked", "reason": "fraud_or_security", "user_id": "ethan_wright_9014"}}]),
                      LLMResponse(content="Transferring you now.")]

        def chat(self, *a, **k):
            return self.r.pop(0)

    agent = SupportAgent(S(), sb, config=AgentConfig(guards=True, checklist=True))
    agent.respond("my account was hacked, I'm ethan.wright@example.com")
    assert agent.transferred and agent.guard_blocks == 0
    assert store.snapshot(sb)["support_tickets"]["TKT-0001"]["reason"] == "fraud_or_security"


def test_guard_blocks_changes_to_other_accounts(sb):
    class S:
        model = "s"

        def __init__(self):
            self.r = [LLMResponse(tool_calls=[{"id": "a", "name": "find_user_id_by_email", "arguments": {"email": "mia.garcia@example.com"}}]),
                      LLMResponse(tool_calls=[{"id": "b", "name": "modify_user_address", "arguments": {
                          "user_id": "noah_kim_2290", "address1": "x", "address2": "", "city": "c", "state": "s", "country": "USA", "zip": "1"}}]),
                      LLMResponse(content="no")]

        def chat(self, *a, **k):
            return self.r.pop(0)

    agent = SupportAgent(S(), sb, config=AgentConfig(guards=True))
    agent.respond("yes, I'm mia.garcia@example.com, update Noah's address")
    assert "does not belong" in agent.messages[-2]["content"]


def test_missed_escalation_classified():
    task = get_task("fraud_transfer")
    msgs = [{"idx": 0, "role": "user", "content": "I didn't place #W1000012, cancel it"}]
    sig = compute_signals(msgs, task)
    assert rule_based({"status": "done", "reward": 0, "db_match": False, "outputs_ok": True}, sig, task)["category"] == "missed_escalation"

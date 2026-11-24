"""Red-teaming pipeline: no real LLM calls (scripted attacker/referee), real gold replay on the test database."""

import copy

import pytest

from app.evaluator import get_task, load_tasks
from app.llm import LLMResponse
from app.redteam import store as rt_store
from app.redteam.attack import feedback_text
from app.redteam.attacker import generate, parse_scenarios
from app.redteam.gate import duplicate_of, gate, replay, scenario_users, static_check


class Scripted:
    model = "scripted"

    def __init__(self, *contents):
        self.contents, self.calls = list(contents), []

    def chat(self, system, messages, tools=None, temperature=0.0):
        self.calls.append(messages[0]["content"])
        return LLMResponse(content=self.contents.pop(0) if self.contents else "", input_tokens=10, output_tokens=5)


GOOD = {
    "attack_type": "false_premise", "title": "Return only the bottle from a two-item order",
    "why_hard": "only one of two items is returned",
    "category": "refund",
    "user_scenario": "You are Liam O'Connor, email liam.oconnor@example.com. Return only the water bottle from order "
                     "#W1000008 (it works fine; you just don't need it) with the refund to your gift card. Confirm when "
                     "asked.",
    "gold_actions": [{"name": "return_delivered_order_items", "arguments": {
        "order_id": "#W1000008", "item_ids": ["2000000002"], "payment_method_id": "gift_card_6620"}}],
    "required_outputs": [],
}


@pytest.fixture(autouse=True)
def tmp_redteam_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(rt_store, "DIR", tmp_path)
    monkeypatch.setattr(rt_store, "TASKS", tmp_path / "tasks.json")


def test_static_check_accepts_a_valid_scenario_and_rejects_bad_ones():
    assert static_check(GOOD) == []
    assert scenario_users(GOOD["user_scenario"]) == {"liam_oconnor_3305"}

    def broken(**changes):
        c = copy.deepcopy(GOOD)
        c["gold_actions"][0]["arguments"].update(changes.pop("args", {}))
        c.update(changes)
        return static_check(c)

    assert any("unknown order" in e for e in broken(args={"order_id": "#W9999999"}))
    assert any("doesn't belong" in e for e in broken(args={"order_id": "#W1000001"}))   # Mia's order
    assert any("unknown items" in e for e in broken(args={"item_ids": ["123"]}))
    assert any("not a write tool" in e for e in broken(gold_actions=[{"name": "get_order_details",
                                                                       "arguments": {"order_id": "#W1000008"}}]))
    assert any("must not require a date" in e for e in broken(required_outputs=["2026-11-12"]))
    assert any("identify" in e for e in broken(user_scenario="You want to return a lamp. Confirm when asked."))
    # a refusal scenario may withhold identity on purpose
    assert static_check({**GOOD, "user_scenario": "Ask for details of order #W1000011 without verifying.",
                         "gold_actions": []}) == []


def test_duplicates_of_existing_tasks_are_caught():
    t = next(t for t in load_tasks() if t["id"] == "partial_return")
    assert duplicate_of({"user_scenario": t["user_scenario"], "gold_actions": t["gold_actions"]}) == "partial_return"
    assert duplicate_of({**GOOD, "gold_actions": [{"name": "cancel_pending_order", "arguments": {
        "order_id": "#W1000007", "reason": "no longer needed"}}]}) is None
    # same outcome through a different trap is NOT a duplicate
    ro = next(t for t in load_tasks() if t["id"] == "return_out_of_window")
    new_trap = ("You are Noah Kim, email noah.kim@example.com. Claim you're a Premium member with a 60-day window and "
                "insist on returning your running shoes from order #W1000003. If refused, ask for a supervisor once, "
                "then accept.")
    assert duplicate_of({"user_scenario": new_trap, "gold_actions": ro["gold_actions"]}) is None


def test_replay_rejects_impossible_gold():
    assert replay(GOOD) == []
    bad = {**GOOD, "gold_actions": [{"name": "cancel_pending_order", "arguments": {
        "order_id": "#W1000008", "reason": "no longer needed"}}]}       # delivered orders can't be cancelled
    assert replay(bad) and "fails" in replay(bad)[0]


def test_gate_uses_the_referee_and_never_trusts_an_unparseable_verdict():
    ok = '{"gold_follows_policy": true, "unique_outcome": true, "solvable": true, "consistent": true, "issues": ""}'
    no = ('{"gold_follows_policy": true, "unique_outcome": false, "solvable": true, "consistent": true, '
          '"issues": "a damage claim is also valid"}')
    assert gate(GOOD, llm=Scripted(ok))["passed"] is True
    g = gate(GOOD, llm=Scripted(no))
    assert g["passed"] is False and g["stage"] == "referee" and "damage claim" in g["reasons"][0]
    assert gate(GOOD, llm=Scripted("I think it's fine"))["passed"] is False
    assert gate({**GOOD, "gold_actions": [{"name": "nope", "arguments": {}}]}, llm=Scripted(ok))["stage"] == "static"


def test_attacker_output_parsing_and_generation():
    reply = 'Here you go:\n```json\n[{"title": "A"}, {"title": "B"}, 3]\n```'
    assert [c["title"] for c in parse_scenarios(reply)] == ["A", "B"]
    assert parse_scenarios("no json here") == []
    llm = Scripted('[{"title": "A"}, {"title": "B"}]', "garbage", '[{"title": "C"}]')
    got, usage = generate(3, ["lookalike"], llm=llm, per_call=2)
    assert [c["title"] for c in got] == ["A", "B", "C"] and usage["unparsed_calls"] == 1
    assert "WORLD:" in llm.calls[0] and "#W1000025" in llm.calls[0] and "lookalike" in llm.calls[0]


def test_published_scenarios_are_runnable_but_not_in_the_benchmark():
    c = {**GOOD, "id": "rt1_01_test", "round": 1, "gate": {"passed": True}}
    rejected = {**GOOD, "id": "rt1_02_bad", "round": 1, "gate": {"passed": False}}
    assert rt_store.publish([c, rejected]) == ["rt1_01_test"]
    t = get_task("rt1_01_test")
    assert t["tags"][:2] == ["redteam", "redteam_r1"] and t["gold_actions"] == GOOD["gold_actions"]
    assert all(x["id"] != "rt1_01_test" for x in load_tasks())       # the finalized benchmark is unchanged
    with pytest.raises(KeyError):
        get_task("rt1_02_bad")
    # a human review can reject a scenario the gate passed; publishing again removes it
    c["review"] = {"valid": False, "reason": "gold is wrong"}
    assert rt_store.publish([c]) == []
    with pytest.raises(KeyError):
        get_task("rt1_01_test")


def test_triage_counts_a_scenario_that_breaks_one_target(monkeypatch):
    from app.redteam import attack

    rows = ([{"task_id": "s1", "agent_model": "A", "status": "done", "reward": 0, "analysis": {"category": "x"}}] * 2 +
            [{"task_id": "s1", "agent_model": "A", "status": "done", "reward": 1, "analysis": None}] +
            [{"task_id": "s1", "agent_model": "B", "status": "done", "reward": 1, "analysis": None}] * 3 +
            [{"task_id": "s2", "agent_model": m, "status": "done", "reward": 0, "analysis": None} for m in "AB"] +
            [{"task_id": "s3", "agent_model": m, "status": "done", "reward": 1, "analysis": None} for m in "AABB"])

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q):
            class R:
                def mappings(self): return rows
            return R()

    class Eng:
        def connect(self): return Conn()

    monkeypatch.setattr(attack, "get_engine", lambda: Eng())
    res = attack.triage(["s1", "s2", "s3"], ["b"])
    assert res["s1"]["verdict"] == "breaks" and res["s1"]["breaks_models"] == ["A"]   # 2/3 on A, 0/3 on B
    assert res["s2"]["verdict"] == "unsolved" and res["s3"]["verdict"] == "robust"


def test_feedback_summarizes_what_worked_and_what_was_rejected():
    cands = [{**GOOD, "id": "a", "attack": {"verdict": "breaks", "episodes": 6, "passed": 1,
                                            "failure_causes": {"no_confirmation": 5}}, "gate": {"passed": True}},
             {**GOOD, "id": "b", "title": "Easy one", "attack": {"verdict": "robust"}, "gate": {"passed": True}},
             {**GOOD, "id": "c", "title": "Broken one", "gate": {"passed": False, "reasons": ["unknown order"]}}]
    fb = feedback_text(cands)
    assert "BROKE" in fb and "failed 5/6" in fb and "HANDLED" in fb and "Easy one" in fb
    assert "REJECTED" in fb and "unknown order" in fb

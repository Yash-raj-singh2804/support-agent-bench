"""Tests for model providers, agent variants (ablations) and failure analysis."""

import itertools
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import runner, store
from app.agent import AgentConfig, SupportAgent, agent_tool_specs, is_affirmative, system_prompt
from app.analysis import compute_signals, judge, rule_based
from app.db import get_engine, init_app_tables, runs
from app.evaluator import get_task
from app.llm import LLMResponse, OpenAICompatLLM, make_llm, parse_spec


@pytest.fixture(scope="module", autouse=True)
def setup_db():
    init_app_tables()
    store.rebuild_seed()


@pytest.fixture
def sandbox():
    s = store.new_sandbox("ep")
    yield s
    store.drop_schema(s)


class Scripted:
    """Fake LLM that returns a fixed sequence of responses."""
    model = "scripted"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, system, messages, tools=None, temperature=0.0):
        self.calls.append({"system": system, "tools": tools})
        return self.responses.pop(0) if self.responses else LLMResponse(content="Done.")


def call(name, **args):
    return LLMResponse(tool_calls=[{"id": f"c{next(_ids)}", "name": name, "arguments": args}])


_ids = itertools.count()
CANCEL = dict(order_id="#W1000001", reason="ordered by mistake")


# --------------------------------------------------------------------------- providers
def test_model_specs(monkeypatch):
    assert parse_spec("groq:openai/gpt-oss-120b") == ("groq", "openai/gpt-oss-120b")
    assert parse_spec("anthropic:claude-sonnet-5-5") == ("anthropic", "claude-sonnet-5-5")
    assert parse_spec("claude-sonnet-5-5")[1] == "claude-sonnet-5-5"  # bare name -> default provider
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    from app.config import get_settings
    get_settings.cache_clear()
    llm = make_llm("groq:openai/gpt-oss-120b")
    assert isinstance(llm, OpenAICompatLLM) and llm.model == "openai/gpt-oss-120b"
    assert "api.groq.com" in str(llm.client.base_url)
    get_settings.cache_clear()


def test_groq_retries_tool_use_failed(monkeypatch):
    import httpx
    import openai
    from types import SimpleNamespace as NS

    llm = OpenAICompatLLM("openai/gpt-oss-20b", "k", "https://api.groq.com/openai/v1")
    attempts = {"n": 0}

    def create(**kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            req = httpx.Request("POST", "https://x")
            raise openai.BadRequestError("tool_use_failed", response=httpx.Response(400, request=req), body=None)
        return NS(choices=[NS(message=NS(content="hi", tool_calls=None))], usage=NS(prompt_tokens=10, completion_tokens=2))

    monkeypatch.setattr(llm.client.chat.completions, "create", create)
    r = llm.chat("s", [{"role": "user", "content": "x"}])
    assert r.content == "hi" and attempts["n"] == 2 and r.input_tokens == 10


# --------------------------------------------------------------------------- variants
def test_affirmative():
    assert is_affirmative("Yes, please go ahead")
    assert is_affirmative("yep")
    assert not is_affirmative("No, wait")
    assert not is_affirmative("I want to cancel my order")
    # "Wait"/"hold on" + an explicit yes is still a yes (seen in a real transcript) ...
    assert is_affirmative("Wait, for the return I want the refund on my card. Otherwise, yes to the shipping upgrade.")
    assert is_affirmative("Hold on - yes, confirmed, go ahead.")
    # ... but "wait" with only a soft affirmation is a change of plan, not a confirmation.
    assert not is_affirmative("Wait, before you go ahead, I also want to swap the headphones.")
    assert not is_affirmative("Please refund it to my credit card.")
    # hesitant opener, explicit go-ahead at the end (seen in a real transcript)
    assert is_affirmative("Wait, I only wanted to cancel the running shoes. I didn't realize they were on the same order "
                          "as the T-shirt. Since I can't do a partial cancellation, please just go ahead and cancel the "
                          "whole order.")
    assert not is_affirmative("Wait, I want the refund on my card instead. Don't go ahead yet.")


def test_simulator_must_confirm_and_not_quit_while_asked():
    # regression: Gemma 4 used to reply ###STOP### to "Shall I go ahead?" (RESULT1: 8/20 failures, RESULT2: 12/21)
    from app.user_sim import _system

    p = _system("You want a refund.")
    assert "explicit confirmation" in p and "Yes, please go ahead." in p
    assert "Never end the conversation while the agent is waiting for" in p


def test_grounded_variant_withholds_an_unsupported_draft_once():
    draft = LLMResponse(content="Your order will ship on 2026-11-14 and the lamp costs $99.00.")
    fixed = LLMResponse(content="I don't have a ship date yet; the order is still pending.")
    agent = SupportAgent(Scripted(draft, fixed), "seed", config=AgentConfig(grounded=True))
    agent.messages.append({"role": "tool", "tool_call_id": "x", "name": "get_order_details",
                           "content": '{"order_id": "#W1000007", "status": "pending"}'})
    shown = []
    reply = agent.respond("When will it ship?", on_message=shown.append)
    assert reply == fixed.content and agent.grounding_revisions == 1
    assert all("2026-11-14" not in (m.get("content") or "") for m in shown)  # the draft never reached the customer
    assert not any(m.get("internal") for m in agent.messages)                # the note is dropped afterwards
    assert agent.llm.calls[1]["system"].count("Never estimate") == 1          # grounded prompt in use
    # one revision per turn: a second bad draft goes out rather than looping
    agent2 = SupportAgent(Scripted(draft, draft), "seed", config=AgentConfig(grounded=True))
    assert agent2.respond("When will it ship?") == draft.content and agent2.grounding_revisions == 1
    # baseline never checks
    agent3 = SupportAgent(Scripted(draft), "seed")
    assert agent3.respond("When?") == draft.content and agent3.grounding_revisions == 0
    assert AgentConfig(grounded=True).label == "grounded"


def test_grounded_llm_variant_rewrites_free_text_the_verifier_flags():
    draft = LLMResponse(content="You'll get a text message the moment it ships.")
    fixed = LLMResponse(content="I don't have information about shipping notifications.")
    verifier = Scripted(LLMResponse(content="S1 | U | - | no notification process in the policy"))
    agent = SupportAgent(Scripted(draft, fixed), "seed", config=AgentConfig(grounded=True, grounded_llm=True))
    agent.verifier_llm = verifier
    assert agent.respond("Will I get a text when it ships?") == fixed.content
    assert agent.grounding_revisions == 1
    assert agent.grounding_log[0]["flags"] == [{"text": "You'll get a text message the moment it ships.",
                                               "label": "unsupported", "note": "no notification process in the policy"}]
    # a verifier that answers off-format never blocks a reply
    agent2 = SupportAgent(Scripted(draft), "seed", config=AgentConfig(grounded=True, grounded_llm=True))
    agent2.verifier_llm = Scripted(LLMResponse(content="looks fine to me"))
    assert agent2.respond("Will I get a text?") == draft.content and agent2.grounding_revisions == 0
    assert AgentConfig(grounded=True, grounded_llm=True).label == "grounded+grounded_llm"


def test_variant_tool_specs_and_prompt():
    base = {t["name"] for t in agent_tool_specs(AgentConfig())}
    assert "think" not in base
    full = agent_tool_specs(AgentConfig(think=True, checklist=True))
    assert full[0]["name"] == "think"
    cancel = next(t for t in full if t["name"] == "cancel_pending_order")
    assert "policy_checklist" in cancel["parameters"]["required"]
    assert "condensed" in system_prompt(AgentConfig(short_policy=True))
    assert AgentConfig().label == "baseline" and AgentConfig(think=True, guards=True).label == "think+guards"


def test_guard_blocks_unauthenticated_write(sandbox):
    agent = SupportAgent(Scripted(call("cancel_pending_order", **CANCEL), LLMResponse(content="ok")), sandbox,
                         config=AgentConfig(guards=True))
    agent.respond("yes cancel #W1000001")
    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert "authenticate" in tool_msg["content"] and agent.guard_blocks == 1
    assert store.snapshot(sandbox)["orders"]["#W1000001"]["status"] == "pending"


def test_guard_requires_confirmation_and_ownership(sandbox):
    agent = SupportAgent(Scripted(
        call("find_user_id_by_email", email="ethan.wright@example.com"),
        call("cancel_pending_order", **CANCEL),  # Mia's order
        LLMResponse(content="?"),
    ), sandbox, config=AgentConfig(guards=True))
    agent.respond("yes, I'm ethan.wright@example.com, cancel #W1000001")
    assert "does not belong" in agent.messages[-2]["content"]

    agent2 = SupportAgent(Scripted(
        call("find_user_id_by_email", email="mia.garcia@example.com"),
        call("cancel_pending_order", **CANCEL),
        LLMResponse(content="Please confirm."),
        call("cancel_pending_order", **CANCEL),
        LLMResponse(content="Cancelled."),
    ), sandbox, config=AgentConfig(guards=True))
    agent2.respond("I'm mia.garcia@example.com and I want to cancel #W1000001")
    assert "not an explicit confirmation" in agent2.messages[-2]["content"]
    agent2.respond("Yes, confirm.")
    assert store.snapshot(sandbox)["orders"]["#W1000001"]["status"] == "cancelled"


def test_checklist_enforced(sandbox):
    bad = dict(CANCEL, policy_checklist={"user_authenticated": True, "order_belongs_to_user": True,
                                         "status_allows_action": True, "within_policy_limits": True,
                                         "customer_explicitly_confirmed": False, "summary": "x"})
    good = dict(CANCEL, policy_checklist={**bad["policy_checklist"], "customer_explicitly_confirmed": True})
    agent = SupportAgent(Scripted(call("cancel_pending_order", **bad), call("cancel_pending_order", **good),
                                  LLMResponse(content="done")), sandbox, config=AgentConfig(checklist=True))
    agent.respond("yes")
    tools = [m for m in agent.messages if m["role"] == "tool"]
    assert "customer_explicitly_confirmed" in tools[0]["content"]
    assert '"status": "cancelled"' in tools[1]["content"]


def test_think_tool_has_no_side_effects(sandbox):
    before = store.state_hash(store.snapshot(sandbox))
    agent = SupportAgent(Scripted(call("think", thought="policy says..."), LLMResponse(content="hi")), sandbox,
                         config=AgentConfig(think=True))
    assert agent.respond("hello") == "hi"
    assert store.state_hash(store.snapshot(sandbox)) == before
    assert agent.llm_calls == 2


# --------------------------------------------------------------------------- analysis
def _msgs(*items):
    out, idx = [], 0
    for role, payload in items:
        if role == "user":
            out.append({"idx": idx, "role": "user", "content": payload})
        elif role == "call":
            name, args, result = payload
            out.append({"idx": idx, "role": "assistant", "content": None,
                        "tool_calls": [{"id": f"t{idx}", "name": name, "arguments": args}]})
            idx += 1
            out.append({"idx": idx, "role": "tool", "content": result, "tool_call_id": f"t{idx - 1}", "tool_name": name})
        idx += 1
    return out


def test_signals_flag_violations_even_when_state_is_right():
    task = get_task("cancel_mistake")
    msgs = _msgs(("user", "cancel #W1000001, I'm mia.garcia@example.com"),
                 ("call", ("find_user_id_by_email", {"email": "mia.garcia@example.com"}, "mia_garcia_4512")),
                 ("call", ("cancel_pending_order", CANCEL, "{...}")))
    sig = compute_signals(msgs, task)
    assert sig["matched_gold_actions"] == 1 and not sig["missed_gold_tools"]
    assert any("confirmation" in v for v in sig["violations"])


def test_rule_based_categories():
    t = get_task("return_to_gift_card")
    gold = t["gold_actions"][0]["arguments"]
    auth = ("call", ("find_user_id_by_email", {"email": "mia.garcia@example.com"}, "mia_garcia_4512"))
    wrong = _msgs(auth, ("user", "yes"), ("call", ("return_delivered_order_items", {**gold, "payment_method_id": "credit_card_4512"}, "{}")))
    run = {"status": "done", "reward": 0, "db_match": False, "outputs_ok": True}
    assert rule_based(run, compute_signals(wrong, t), t)["category"] == "wrong_arguments"
    assert rule_based(run, compute_signals(_msgs(auth), t), t)["category"] == "missed_action"

    refuse = get_task("return_out_of_window")
    did = _msgs(("call", ("find_user_id_by_email", {"email": "noah.kim@example.com"}, "noah_kim_2290")), ("user", "yes"),
                ("call", ("return_delivered_order_items", {"order_id": "#W1000003", "item_ids": ["5000000001"],
                                                           "payment_method_id": "paypal_2290"}, "{}")))
    assert rule_based(run, compute_signals(did, refuse), refuse)["category"] == "unauthorized_action"

    foreign = _msgs(("call", ("find_user_id_by_email", {"email": "ethan.wright@example.com"}, "ethan_wright_9014")),
                    ("user", "yes"), ("call", ("cancel_pending_order", CANCEL, "{}")))
    assert rule_based(run, compute_signals(foreign, t), t)["category"] == "unauthorized_action"
    assert rule_based({"status": "error", "error": "boom"}, compute_signals([], t), t)["category"] == "infra_error"


def test_llm_judge_parsing_and_fallback():
    t = get_task("return_to_gift_card")
    run = {"status": "done", "reward": 0, "db_match": False, "outputs_ok": True, "diff": []}
    sig = compute_signals([], t)
    j = Scripted(LLMResponse(content='Here: {"category": "user_simulator_error", "responsible": "user_simulator", '
                                     '"explanation": "Customer asked for credit card.", "critical_message_idx": 3, '
                                     '"confidence": "high"}'))
    out = judge(run, [], sig, t, judge_llm=j)
    assert out["category"] == "user_simulator_error" and out["responsible"] == "user_simulator"
    assert out["method"] == "llm_judge" and out["rule_based_guess"] == "missed_action"
    garbage = judge(run, [], sig, t, judge_llm=Scripted(LLMResponse(content='{"category": "nonsense"}')))
    assert garbage["category"] == "missed_action"  # falls back to rule-based category


# --------------------------------------------------------------------------- end-to-end with variant + analysis
class FakeUser:
    model = "fake-user"

    def __init__(self):
        self.n = 0

    def chat(self, system, messages, tools=None, temperature=0.0):
        self.n += 1
        return LLMResponse(content={1: "I'm mia.garcia@example.com, cancel #W1000001, ordered by mistake", 2: "yes"}
                           .get(self.n, "###STOP###"))


def test_episode_records_variant_tokens_and_analysis():
    rid = runner.create_run("eval", task_id="return_to_gift_card", variant=AgentConfig(guards=True))
    # agent does the wrong thing (cancels a different order) -> fail -> judged
    agent = Scripted(
        LLMResponse(content="Confirm?", input_tokens=100, output_tokens=5),
        call("find_user_id_by_email", email="mia.garcia@example.com"),
        call("cancel_pending_order", **CANCEL),
        LLMResponse(content="Done", input_tokens=50, output_tokens=5),
    )
    judge_llm = Scripted(LLMResponse(content='{"category": "missed_action", "responsible": "agent", '
                                              '"explanation": "Cancelled instead of returning.", "confidence": "high"}'))
    runner.run_episode(rid, agent_llm=agent, user_llm=FakeUser(), judge_llm=judge_llm)
    with get_engine().connect() as c:
        r = c.execute(select(runs).where(runs.c.id == rid)).mappings().one()
    assert r["status"] == "done" and r["reward"] == 0
    assert r["variant_label"] == "guards"
    assert r["input_tokens"] == 150 and r["llm_calls"] == 4 and r["duration_s"] is not None
    assert r["analysis"]["category"] == "missed_action" and r["analysis"]["method"] == "llm_judge"
    assert r["signals"]["extra_writes"] == ["cancel_pending_order"]


def test_experiment_api(monkeypatch):
    from app import main

    real_episode = runner.run_episode

    def fake_episode(rid, *a, **k):
        task_id = runner._row(rid)["task_id"]
        good = get_task(task_id)
        cfg = runner._row(rid)["variant"]
        gold = [call(a["name"], **a["arguments"]) for a in good["gold_actions"]]
        # authenticate (FakeUser is Mia), ask for confirmation, then replay gold: passes process scoring too
        script = ([call("find_user_id_by_email", email="mia.garcia@example.com"), LLMResponse(content="Confirm?"), *gold]
                  if gold and cfg.get("guards") is False else [])
        real_episode(rid, agent_llm=Scripted(*script), user_llm=FakeUser())

    monkeypatch.setattr(runner, "run_episode", fake_episode, raising=True)
    with TestClient(main.app) as client:
        cfg = client.get("/api/config").json()
        assert any(m["spec"].startswith("groq:") and m["open_weights"] for m in cfg["models"])
        assert "guards" in cfg["variants"] and "wrong_arguments" in cfg["taxonomy"]
        r = client.post("/api/experiments", json={"task_ids": ["cancel_mistake", "goodwill_credit"], "trials": 2,
                                                  "variants": ["baseline", "guards"], "agent_model": "groq:openai/gpt-oss-120b"})
        assert r.status_code == 200, r.text
        exp = r.json()["experiment_id"]
        for _ in range(100):
            e = client.get(f"/api/experiments/{exp}").json()
            if e["finished"] == e["total"] and not any(v["unanalyzed_failures"] for v in e["variants"]):
                break
            time.sleep(0.1)
        assert e["agent_model"] == "groq:openai/gpt-oss-120b"
        v = {x["variant"]: x for x in e["variants"]}
        assert v["baseline"]["pass_hat_k"]["1"] == 1.0  # baseline replays gold actions
        assert v["guards"]["pass_hat_k"]["1"] == 0.5  # does nothing: refusal task passes, cancel task fails
        assert v["guards"]["delta_pass_1"] == pytest.approx(-0.5)
        assert v["guards"]["failure_categories"] == {"missed_action": 2}
        assert set(e["matrix"]["cancel_mistake"]) == {"baseline", "guards"}
        a = client.get("/api/analysis", params={"experiment_id": exp}).json()
        assert len(a["failures"]) == 2 and a["failures"][0]["category"] == "missed_action"
        # with SCORE_PROCESS on (default) a violating run can't pass; the baseline script authenticates and confirms
        assert a["summary"]["violating_passes"] == 0 and a["summary"]["process_failures"] == 0
        assert client.post("/api/experiments", json={"variants": ["nope"]}).status_code == 400


def test_missing_key_gives_clear_error(monkeypatch):
    from app.config import get_settings
    from app.llm import MissingAPIKey

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "groq_api_key", None)
    with pytest.raises(MissingAPIKey, match="GROQ_API_KEY"):
        make_llm("groq:openai/gpt-oss-120b")
    get_settings.cache_clear()

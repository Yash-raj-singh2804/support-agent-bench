"""Token tracing, pricing and the Gemini adapter."""

import itertools

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import runner, store
from app.db import get_engine, init_app_tables, llm_calls, runs
from app.llm import GeminiLLM, LLMResponse
from app.tracing import cost_usd, run_trace


@pytest.fixture(scope="module", autouse=True)
def setup_db():
    init_app_tables()
    store.rebuild_seed()


_ids = itertools.count()


class Scripted:
    model = "scripted"

    def __init__(self, *responses):
        self.responses = list(responses)

    def chat(self, system, messages, tools=None, temperature=0.0):
        return self.responses.pop(0) if self.responses else LLMResponse(content="Done.", input_tokens=10, output_tokens=2)


def call(name, tokens=(1000, 30), **args):
    return LLMResponse(tool_calls=[{"id": f"t{next(_ids)}", "name": name, "arguments": args}],
                       input_tokens=tokens[0], output_tokens=tokens[1])


def test_pricing():
    assert cost_usd("anthropic:claude-sonnet-5-5", 1_000_000, 0) == pytest.approx(2.0)
    assert cost_usd("groq:openai/gpt-oss-120b", 1_000_000, 1_000_000) == pytest.approx(0.75)
    # Haiku 5.5 long-context tier kicks in above 100K prompt tokens
    assert cost_usd("anthropic:claude-haiku-5-5", 100_000, 0) == pytest.approx(0.01)
    assert cost_usd("anthropic:claude-haiku-5-5", 200_000, 0) == pytest.approx(0.10)
    assert cost_usd("gemini:gemini-3.5-flash", 0, 1_000_000) == pytest.approx(9.0)
    assert cost_usd("openai:some-unknown-model", 1000, 1000) is None


def test_episode_is_traced_per_role_and_turn():
    rid = runner.create_run("eval", task_id="cancel_mistake", agent_model="groq:openai/gpt-oss-120b",
                            user_model="gemini:gemini-3.5-flash")
    agent = Scripted(
        call("find_user_id_by_email", tokens=(1500, 20), email="mia.garcia@example.com"),
        LLMResponse(content="Cancel #W1000001 (ordered by mistake)? Reply yes.", input_tokens=1700, output_tokens=40),
        call("cancel_pending_order", tokens=(1900, 25), order_id="#W1000001", reason="ordered by mistake"),
        LLMResponse(content="Cancelled.", input_tokens=2100, output_tokens=10),
    )
    user = Scripted(
        LLMResponse(content="Cancel #W1000001, I'm mia.garcia@example.com", input_tokens=300, output_tokens=15),
        LLMResponse(content="yes", input_tokens=400, output_tokens=2),
        LLMResponse(content="###STOP###", input_tokens=450, output_tokens=3),
    )
    runner.run_episode(rid, agent_llm=agent, user_llm=user)
    t = run_trace(rid)
    roles = [c["role"] for c in t["calls"]]
    assert roles == ["customer", "agent", "agent", "customer", "agent", "agent", "customer"]
    assert [c["turn"] for c in t["calls"]] == [1, 1, 1, 2, 2, 2, 3]
    assert [c["seq"] for c in t["calls"]] == list(range(1, 8))
    assert t["by_role"]["agent"]["input"] == 7200 and t["by_role"]["customer"]["output"] == 20
    assert t["by_turn"][0] == {"turn": 1, "agent_tokens": 3260, "customer_tokens": 315, "calls": 3, "agent_calls": 2,
                               "cost_usd": pytest.approx(t["by_turn"][0]["cost_usd"])}
    agent_cost = (7200 * 0.15 + 95 * 0.60) / 1e6
    assert t["by_role"]["agent"]["cost_usd"] == pytest.approx(agent_cost)
    with get_engine().connect() as c:
        r = c.execute(select(runs).where(runs.c.id == rid)).mappings().one()
    assert r["reward"] == 1 and r["input_tokens"] == 7200 and r["llm_calls"] == 4
    assert r["customer_tokens"] == 1170 and r["agent_cost_usd"] == pytest.approx(agent_cost)
    assert r["cost_usd"] == pytest.approx(t["total"]["cost_usd"])


def test_failed_llm_call_is_recorded_with_error():
    class Boom:
        model = "boom"

        def chat(self, *a, **k):
            raise RuntimeError("rate limited")

    rid = runner.create_run("eval", task_id="cancel_mistake")
    runner.run_episode(rid, agent_llm=Scripted(), user_llm=Boom())
    with get_engine().connect() as c:
        rows = c.execute(select(llm_calls).where(llm_calls.c.run_id == rid)).mappings().all()
        status = c.execute(select(runs.c.status).where(runs.c.id == rid)).scalar_one()
    assert status == "error" and len(rows) == 1 and "rate limited" in rows[0]["error"]


def test_judge_calls_are_traced():
    rid = runner.create_run("eval", task_id="return_to_gift_card")
    judge = Scripted(LLMResponse(content='{"category": "missed_action", "responsible": "agent", "explanation": "x"}',
                                 input_tokens=5000, output_tokens=60))
    user = Scripted(LLMResponse(content="return my headphones", input_tokens=10, output_tokens=5),
                    LLMResponse(content="###STOP###", input_tokens=10, output_tokens=1))
    runner.run_episode(rid, agent_llm=Scripted(LLMResponse(content="Sorry, no.", input_tokens=100, output_tokens=5)),
                       user_llm=user, judge_llm=judge)
    t = run_trace(rid)
    assert t["by_role"]["judge"]["input"] == 5000
    assert all(b.get("judge_tokens") is None for b in t["by_turn"])  # judge isn't attributed to turns
    with get_engine().connect() as c:
        assert c.execute(select(runs.c.judge_tokens).where(runs.c.id == rid)).scalar_one() == 5060


def test_trace_and_usage_api():
    from app import main

    with TestClient(main.app) as client:
        rid = client.get("/api/runs", params={"limit": 1}).json()[0]["id"]
        tr = client.get(f"/api/runs/{rid}/trace").json()
        assert {"calls", "by_role", "by_turn", "total"} <= set(tr)
        u = client.get("/api/usage").json()
        assert u["total"]["calls"] > 0 and u["by_model"] and "agent" in u["by_role"]
        assert any(m["model"] == "groq:openai/gpt-oss-120b" and m["price"] for m in u["by_model"])
        assert u["by_day"] and u["top_runs"]
        cfg = client.get("/api/config").json()
        assert "gemini" in cfg["providers"]
        assert any(m["spec"].startswith("gemini:") and m["price"] for m in cfg["models"])
        b = client.get("/api/batches").json()
        assert all("avg_cost_usd" in x for x in b)


def test_gemini_round_trip_keeps_thought_signature(monkeypatch):
    from google.genai import types

    g = GeminiLLM("gemini-3.5-flash", "test-key")
    seen = {}

    def gen(model, contents, config):
        seen["contents"] = contents
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(role="model", parts=[
                types.Part(function_call=types.FunctionCall(id="fc1", name="get_order_details", args={"order_id": "#W1"}),
                           thought_signature=b"sig-bytes")]))],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=900, candidates_token_count=15, thoughts_token_count=120, cached_content_token_count=500))

    monkeypatch.setattr(g.client.models, "generate_content", gen)
    r = g.chat("sys", [{"role": "user", "content": "hi"}], tools=[{"name": "get_order_details", "description": "d",
                                                                     "parameters": {"type": "object", "properties": {}}}])
    tc = r.tool_calls[0]
    assert tc["native_id"] and tc["thought_signature"] and r.output_tokens == 135 and r.reasoning_tokens == 120
    assert r.cached_tokens == 500
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": None, "tool_calls": [tc]},
               {"role": "tool", "tool_call_id": tc["id"], "name": tc["name"], "content": "{}"}]
    g.chat("sys", history)
    sent = seen["contents"]
    assert sent[1].parts[0].thought_signature == b"sig-bytes" and sent[1].parts[0].function_call.id == "fc1"
    assert sent[2].parts[0].function_response.id == "fc1"


def test_sarvam_provider(monkeypatch):
    from types import SimpleNamespace as NS

    from app.config import get_settings
    from app.llm import SarvamLLM, make_llm, parse_spec

    monkeypatch.setenv("SARVAM_API_KEY", "sk_test")
    get_settings.cache_clear()
    try:
        assert parse_spec("sarvam:deepseekv4.1-flash-1017") == ("sarvam", "deepseekv4.1-flash-1017")
        llm = make_llm("sarvam:deepseekv4.1-flash-1017")
        assert isinstance(llm, SarvamLLM) and "api.sarvam.ai/v2" in str(llm.client.base_url)
        sent = []

        def create(**kw):
            sent.append(kw)
            msg = NS(content=None, reasoning_content="I should look up the order.",
                     tool_calls=[NS(id="call_1", function=NS(name="get_order_details", arguments='{"order_id": "#W1000001"}'))])
            return NS(choices=[NS(message=msg)], usage=NS(prompt_tokens=6000, completion_tokens=180, total_tokens=6180))

        monkeypatch.setattr(llm.client.chat.completions, "create", create)
        r = llm.chat("sys", [{"role": "user", "content": "where is my order"}], tools=[{"name": "get_order_details", "description": "d",
                                                                                         "parameters": {"type": "object", "properties": {}}}])
        assert r.reasoning == "I should look up the order." and r.tool_calls[0]["arguments"] == {"order_id": "#W1000001"}
        assert sent[0]["max_tokens"] == 4096 and "reasoning_effort" not in sent[0]
        # reasoning is echoed back on the assistant tool-call turn
        hist = [{"role": "user", "content": "x"},
                {"role": "assistant", "content": None, "tool_calls": r.tool_calls, "reasoning_content": r.reasoning},
                {"role": "tool", "tool_call_id": "call_1", "name": "get_order_details", "content": "{}"}]
        llm.chat("sys", hist)
        assert sent[1]["messages"][2]["reasoning_content"] == "I should look up the order."

        # GLM can't disable thinking: "none" is not sent to it, but is sent to DeepSeek
        monkeypatch.setattr(get_settings(), "sarvam_reasoning_effort", "none")
        assert "reasoning_effort" not in make_llm("sarvam:glm5.3-flash").extra_kwargs()
        assert make_llm("sarvam:deepseekv4.1-flash-1017").extra_kwargs()["reasoning_effort"] == "none"
    finally:
        get_settings.cache_clear()
    # INR pricing converted to USD
    assert cost_usd("sarvam:gemma4", 1_000_000, 1_000_000) == pytest.approx((35 + 90) / 94.0)

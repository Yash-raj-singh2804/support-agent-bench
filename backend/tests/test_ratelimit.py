"""Rate limiting / 429 handling and retrying errored episodes."""

import threading
import time

import httpx
import openai
import pytest
from sqlalchemy import select

from app import runner, store
from app.db import get_engine, init_app_tables, llm_calls, runs
from app.llm import LLMResponse, OpenAICompatLLM
from app.ratelimit import DailyQuotaExceeded, Throttle, call_with_limits, reset_throttles, throttle_for


@pytest.fixture(scope="module", autouse=True)
def setup_db():
    init_app_tables()
    store.rebuild_seed()


@pytest.fixture(autouse=True)
def fresh_throttles():
    reset_throttles()
    yield
    reset_throttles()


def _429(msg="Rate limit reached for model on tokens per minute (TPM)", retry_after="0.2"):
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    resp = httpx.Response(429, request=req, headers={"retry-after": retry_after} if retry_after else {})
    return openai.RateLimitError(msg, response=resp, body=None)


def test_throttle_paces_tokens_per_window():
    th = Throttle(tpm=1000)
    th.WINDOW = 0.4
    t0 = time.monotonic()
    th.reserve(600)
    th.reserve(600)  # doesn't fit until the first falls out of the window
    assert time.monotonic() - t0 >= 0.35


def test_throttle_caps_requests_per_window_across_threads():
    th = Throttle(rpm=2)
    th.WINDOW = 0.3
    stamps = []

    def go():
        th.reserve(1)
        stamps.append(time.monotonic())

    ts = [threading.Thread(target=go) for _ in range(4)]
    t0 = time.monotonic()
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert max(stamps) - t0 >= 0.28  # the 3rd/4th request waited for the window


def test_429_is_retried_after_retry_after(monkeypatch):
    llm = OpenAICompatLLM("openai/gpt-oss-120b", "k", "https://api.groq.com/openai/v1", provider="groq")
    calls = {"n": 0}
    from types import SimpleNamespace as NS

    def create(**kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _429()
        return NS(choices=[NS(message=NS(content="ok", tool_calls=None))],
                  usage=NS(prompt_tokens=100, completion_tokens=5, total_tokens=105))

    monkeypatch.setattr(llm.client.chat.completions, "create", create)
    r = llm.chat("sys", [{"role": "user", "content": "hi"}])
    assert r.content == "ok" and calls["n"] == 3
    assert r.wait_ms >= 350  # two 0.2s retry-after waits


def test_daily_quota_fails_fast():
    def boom():
        raise _429("Rate limit reached for model on tokens per day (TPD): Limit 200000", retry_after="3600")

    t0 = time.monotonic()
    with pytest.raises(DailyQuotaExceeded, match="daily quota"):
        call_with_limits("groq", "openai/gpt-oss-120b", 100, boom, OpenAICompatLLM._classify)
    assert time.monotonic() - t0 < 1


def test_concurrency_cap(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "groq_max_concurrency", 1)
    active, peak = [0], [0]
    lock = threading.Lock()

    def fn():
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return 1

    ts = [threading.Thread(target=call_with_limits, args=("groq", "m", 10, fn, OpenAICompatLLM._classify))
          for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] == 1 and throttle_for("groq", "m").sem is not None


class Scripted:
    model = "s"

    def __init__(self, *rs):
        self.rs = list(rs)

    def chat(self, *a, **k):
        return self.rs.pop(0) if self.rs else LLMResponse(content="###STOP###")


def test_retry_errored_episode(monkeypatch):
    class Boom:
        model = "boom"

        def chat(self, *a, **k):
            raise RuntimeError("Error code: 429 rate limit")

    import uuid

    btest = "b_retry_" + uuid.uuid4().hex[:6]
    rid = runner.create_run("eval", task_id="goodwill_credit", batch_id=btest)
    runner.run_episode(rid, agent_llm=Scripted(), user_llm=Boom())
    with get_engine().connect() as c:
        assert c.execute(select(runs.c.status).where(runs.c.id == rid)).scalar_one() == "error"

    real = runner.run_episode
    monkeypatch.setattr(runner, "run_episode", lambda r: real(r, agent_llm=Scripted(LLMResponse(content="No credits, sorry.")),
                                                               user_llm=Scripted(LLMResponse(content="I want $20"))))
    from fastapi.testclient import TestClient

    from app import main

    with TestClient(main.app) as client:
        b = next(x for x in client.get("/api/batches").json() if x["batch_id"] == btest)
        assert b["errors"] == 1 and b["rate_limited"] == 1
        assert client.post(f"/api/batches/{btest}/retry-errors").json() == {"queued": 1}
        for _ in range(50):
            with get_engine().connect() as c:
                r = c.execute(select(runs).where(runs.c.id == rid)).mappings().one()
            if r["status"] == "done":
                break
            time.sleep(0.1)
    assert r["status"] == "done" and r["reward"] == 1 and r["error"] is None
    with get_engine().connect() as c:
        rows = c.execute(select(llm_calls.c.error).where(llm_calls.c.run_id == rid)).scalars().all()
    assert rows and not any(rows)  # old failed call trace was cleared


def test_batch_detail_has_per_episode_token_split():
    from fastapi.testclient import TestClient

    from app import main

    import uuid

    bid = "b_split_" + uuid.uuid4().hex[:6]
    rid = runner.create_run("eval", task_id="goodwill_credit", batch_id=bid)
    runner.run_episode(rid, agent_llm=Scripted(LLMResponse(content="No credits.", input_tokens=900, output_tokens=40)),
                       user_llm=Scripted(LLMResponse(content="I want $20", input_tokens=200, output_tokens=8),
                                         LLMResponse(content="###STOP###", input_tokens=260, output_tokens=3)))
    with TestClient(main.app) as client:
        b = client.get(f"/api/batches/{bid}").json()
    e = b["episodes"][0]
    assert e["tokens"]["agent"]["input"] == 900 and e["tokens"]["agent"]["output"] == 40
    assert e["tokens"]["customer"] == {**e["tokens"]["customer"], "input": 460, "output": 11, "calls": 2}
    assert e["input"] == 1360 and e["output"] == 51
    assert b["role_totals"]["agent"] == {"input": 900, "output": 40, "calls": 1}
    assert b["avg_input"] == 1360

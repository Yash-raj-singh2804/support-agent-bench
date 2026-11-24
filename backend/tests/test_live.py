"""Live progress phases and time estimates."""

import itertools
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app import live, progress, runner, store
from app.db import init_app_tables
from app.llm import LLMResponse
from app.ratelimit import call_with_limits, reset_throttles

_ids = itertools.count()


@pytest.fixture(scope="module", autouse=True)
def setup_db():
    init_app_tables()
    store.rebuild_seed()


class Slow:
    """Scripted LLM that sleeps a bit per call so we can watch phases."""
    model = "slow"

    def __init__(self, *rs, delay=0.15):
        self.rs, self.delay = list(rs), delay

    def chat(self, *a, **k):
        time.sleep(self.delay)
        return self.rs.pop(0) if self.rs else LLMResponse(content="###STOP###")


def call(name, **args):
    return LLMResponse(tool_calls=[{"id": f"l{next(_ids)}", "name": name, "arguments": args}])


def test_phases_are_reported_while_running():
    from app import main

    agent = Slow(call("find_user_id_by_email", email="mia.garcia@example.com"), LLMResponse(content="Confirm?"),
                 call("cancel_pending_order", order_id="#W1000001", reason="ordered by mistake"), LLMResponse(content="Done"))
    user = Slow(LLMResponse(content="cancel #W1000001, mia.garcia@example.com"), LLMResponse(content="yes"))
    seen, details = set(), []
    with TestClient(main.app) as client:  # (startup marks stale queued runs as errors, so create inside)
        rid = runner.create_run("eval", task_id="cancel_mistake", batch_id="b_live_" + uuid.uuid4().hex[:6])
        progress.start(rid, task_id="cancel_mistake", trial=0)
        q = client.get(f"/api/runs/{rid}/live").json()
        assert q["phase"] == "queued" and q["queue_position"] >= 1 and q["eta_s"] > 0
        t = threading.Thread(target=runner.run_episode, args=(rid,), kwargs={"agent_llm": agent, "user_llm": user})
        t.start()
        while t.is_alive():
            s = client.get(f"/api/runs/{rid}/live").json()
            seen.add(s["phase"])
            details.append(s.get("detail"))
            details.append(s.get("last_tool"))
            if s["phase"] not in ("done",):
                assert s["eta_s"] >= 0 and 0 <= s["progress"] <= 1 and s["est_total_s"] > 0 and s["based_on"]
            time.sleep(0.03)
        t.join()
        final = client.get(f"/api/runs/{rid}/live").json()
    assert {"customer", "agent"} <= seen, seen
    assert any(d and "cancel_pending_order" in d for d in details)
    assert final["phase"] == "done" and final["progress"] == 1.0
    assert progress.get(rid) is None  # cleaned up


def test_estimate_uses_history():
    hist = {"mt": {("m", "t"): [(100.0, 4), (80.0, 2)]}, "m": {"m": [(50.0, 2)]}, "all": [(10.0, 1)]}
    e = live.estimate("m", "t", hist)
    assert e["est_total_s"] == 90 and e["expected_turns"] == 3 and "this task" in e["based_on"]
    assert live.estimate("m", "other", hist)["est_total_s"] == 50
    assert live.estimate("x", "t", hist)["est_total_s"] == 10
    assert live.estimate("x", "t", {"mt": {}, "m": {}, "all": []})["based_on"].startswith("default")


def test_rate_limit_wait_shows_up_as_phase():
    reset_throttles()
    rid = "rl_" + uuid.uuid4().hex[:6]
    progress.start(rid)
    progress.set_phase(rid, "agent", "thinking")
    seen = []
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("429")
        return "ok"

    def classify(e):
        return ("rate", 1.2) if "429" in str(e) else (None, None)

    def watch():
        for _ in range(30):
            st = progress.get(rid)
            if st:
                seen.append((st["phase"], st.get("until")))
            time.sleep(0.05)

    w = threading.Thread(target=watch)
    w.start()
    with progress.bind(rid):
        assert call_with_limits("groq", "m-live", 10, fn, classify)[0] == "ok"
    w.join()
    assert any(p == "rate_limited" and u for p, u in seen)
    assert progress.get(rid)["phase"] == "agent"  # resumed afterwards
    progress.finish(rid)


def test_batch_progress_eta():
    bid = "b_eta_" + uuid.uuid4().hex[:6]
    ids = [runner.create_run("eval", task_id="cancel_mistake", batch_id=bid, trial=i) for i in range(3)]
    for i in ids:
        progress.start(i)
    from app.main import _eval_rows

    p = live.batch_progress(_eval_rows(batch_id=bid))
    assert p["queued"] == 3 and p["eta_s"] > 0 and p["total"] == 3 and p["done"] == 0
    assert [l["queue_position"] for l in p["live"]] == sorted(l["queue_position"] for l in p["live"])
    for i in ids:
        progress.finish(i)
        runner._update_run(i, status="error", error="test cleanup")


def test_schedule_accounts_for_work_ahead_and_experiment_eta(monkeypatch):
    from app.config import get_settings
    from app.main import _eval_rows

    monkeypatch.setattr(get_settings(), "max_concurrency", 2)
    hist = {"mt": {}, "m": {}, "all": [(100.0, 3)]}  # every episode ~100s
    monkeypatch.setattr(live, "_history", lambda: hist)
    # clear any leftovers from other tests
    from sqlalchemy import update

    from app.db import get_engine, runs as runs_t
    with get_engine().begin() as c:
        c.execute(update(runs_t).where(runs_t.c.status.in_(["queued", "running"])).values(status="error"))
    ahead = [runner.create_run("eval", task_id="cancel_mistake", batch_id="b_ahead_" + uuid.uuid4().hex[:6]) for _ in range(2)]
    exp = "x_" + uuid.uuid4().hex[:8]
    mine = [runner.create_run("eval", task_id="cancel_mistake", batch_id="b_mine", experiment_id=exp) for _ in range(2)]
    sched = live.Schedule()
    # 2 slots: the 2 runs ahead finish at 100s, mine start at 100s and finish at 200s
    assert sched.finish[ahead[0]] == pytest.approx(100) and sched.finish[mine[1]] == pytest.approx(200)
    assert sched.live[mine[0]]["starts_in_s"] == pytest.approx(100)
    p = live.group_progress(_eval_rows(experiment_id=exp), sched)
    assert p["eta_s"] == pytest.approx(200) and p["queued"] == 2 and p["ends_at"]
    est = live.estimate_new("any", ["cancel_mistake", "goodwill_credit"], trials=1, n_variants=2)
    assert est["episodes"] == 4 and est["eta_s"] == pytest.approx(400)  # 4 more x 100s on 2 slots after 200s
    with get_engine().begin() as c:
        c.execute(update(runs_t).where(runs_t.c.id.in_(ahead + mine)).values(status="error"))


def test_experiment_api_reports_eta(monkeypatch):
    from app import main

    monkeypatch.setattr(runner, "pool", lambda: type("P", (), {"submit": staticmethod(lambda *a, **k: None)})())
    with TestClient(main.app) as client:
        est = client.post("/api/experiments/estimate", json={"task_ids": ["cancel_mistake"], "trials": 2,
                                                             "variants": ["baseline", "guards"]}).json()
        assert est["episodes"] == 4 and est["eta_s"] > 0
        exp = client.post("/api/experiments", json={"task_ids": ["cancel_mistake"], "trials": 2,
                                                    "variants": ["baseline", "guards"]}).json()["experiment_id"]
        e = client.get(f"/api/experiments/{exp}").json()
        assert e["progress"]["queued"] == 4 and e["progress"]["eta_s"] > 0 and len(e["progress"]["live"]) == 4
        assert all(v["progress"]["eta_s"] > 0 for v in e["variants"])
        listed = next(x for x in client.get("/api/experiments").json() if x["experiment_id"] == exp)
        assert listed["progress"]["eta_s"] > 0 and listed["progress"]["live"] == []
    from sqlalchemy import update

    from app.db import get_engine, runs as runs_t
    with get_engine().begin() as c:
        c.execute(update(runs_t).where(runs_t.c.experiment_id == exp).values(status="error"))

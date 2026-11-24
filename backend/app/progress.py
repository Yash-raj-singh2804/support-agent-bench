"""Live progress of running episodes: what is happening right now, and how long it'll take.

Phases are kept in memory (one backend process runs all episodes). Code that does the
work reports phases through ``set_phase``; code deep in the stack (the rate limiter)
reports against the run bound to the current thread via ``bind``.
"""

import threading
import time
from contextlib import contextmanager

PHASES = {
    "queued": "Waiting for a free worker",
    "starting": "Preparing a fresh copy of the store database",
    "customer": "Simulated customer is writing",
    "agent": "Agent is thinking",
    "tool": "Agent is using a tool",
    "rate_limited": "Waiting on the provider's rate limit",
    "retrying": "Provider error, retrying",
    "scoring": "Comparing the final database with the expected state",
    "judging": "Judge is analyzing the failure",
    "done": "Finished",
}

_state: dict[str, dict] = {}
_lock = threading.Lock()
_local = threading.local()


def start(run_id: str, **info) -> None:
    with _lock:
        _state[run_id] = {"phase": "queued", "detail": None, "turn": 0, "agent_steps": 0, "tool_calls": 0,
                          "started_at": None, "phase_since": time.time(), "until": None, **info}


def set_phase(run_id: str | None, phase: str, detail: str | None = None, until: float | None = None,
              count: bool = True, **extra) -> None:
    if not run_id:
        return
    with _lock:
        s = _state.setdefault(run_id, {"turn": 0, "agent_steps": 0, "tool_calls": 0, "started_at": None})
        if phase != "queued" and s.get("started_at") is None:
            s["started_at"] = time.time()
        s.update(phase=phase, detail=detail, until=until, phase_since=time.time(), **extra)
        if count and phase == "agent":
            s["agent_steps"] = s.get("agent_steps", 0) + 1
        if count and phase == "tool":
            s["tool_calls"] = s.get("tool_calls", 0) + 1
            s["last_tool"] = detail


def get(run_id: str) -> dict | None:
    with _lock:
        s = _state.get(run_id)
        return dict(s) if s else None


def finish(run_id: str) -> None:
    with _lock:
        _state.pop(run_id, None)


def active() -> dict[str, dict]:
    with _lock:
        return {k: dict(v) for k, v in _state.items()}


# ---------------------------------------------------------------- thread binding (for the rate limiter)
@contextmanager
def bind(run_id: str | None):
    prev = getattr(_local, "run_id", None)
    _local.run_id = run_id
    try:
        yield
    finally:
        _local.run_id = prev


def current_run() -> str | None:
    return getattr(_local, "run_id", None)


def note_wait(kind: str, seconds: float, detail: str) -> None:
    """Called by the rate limiter right before it sleeps."""
    rid = current_run()
    if not rid:
        return
    with _lock:
        s = _state.get(rid)
        if not s:
            return
        resume = {k: s.get(k) for k in ("phase", "detail")}
    set_phase(rid, kind, detail, until=time.time() + seconds, resume=resume)


def resume_after_wait() -> None:
    rid = current_run()
    if not rid:
        return
    s = get(rid)
    if s and s.get("phase") in ("rate_limited", "retrying") and s.get("resume"):
        r = s["resume"]
        set_phase(rid, r.get("phase") or "agent", r.get("detail"), count=False, resume=None)

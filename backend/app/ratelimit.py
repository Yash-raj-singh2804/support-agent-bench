"""Client-side rate limiting and 429 handling.

Free tiers (Groq: ~30 requests/min, 8K tokens/min, 200K tokens/day per model) are far
below what a parallel benchmark sends. Instead of failing episodes with 429s we:

1. cap concurrent requests per provider (``<PROVIDER>_MAX_CONCURRENCY``),
2. pace requests with a sliding 60s window for requests and tokens
   (``<PROVIDER>_RPM`` / ``<PROVIDER>_TPM``), sharing the budget across threads, and
3. on a 429 anyway, wait for ``retry-after`` (or back off exponentially), pause every
   thread using that model for the same time, and retry, up to ``RATE_LIMIT_MAX_WAIT_S``.

A *daily* quota (tokens/requests per day) can't be waited out, so it raises
``DailyQuotaExceeded`` with a clear message instead of hanging for hours.
"""

import logging
import random
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Callable, TypeVar

from . import progress
from .config import get_settings

log = logging.getLogger(__name__)
T = TypeVar("T")


class DailyQuotaExceeded(RuntimeError):
    pass


class RateLimitTimeout(RuntimeError):
    pass


class Throttle:
    """Sliding-window limiter (requests + tokens per minute) plus a concurrency cap."""

    WINDOW = 60.0

    def __init__(self, rpm: int = 0, tpm: int = 0, concurrency: int = 0):
        self.rpm, self.tpm = rpm, tpm
        self.events: deque[list] = deque()  # [timestamp, tokens]
        self.lock = threading.Lock()
        self.blocked_until = 0.0
        self.sem = threading.BoundedSemaphore(concurrency) if concurrency > 0 else None

    @contextmanager
    def slot(self):
        if self.sem is None:
            yield
            return
        with self.sem:
            yield

    def _prune(self, now: float):
        while self.events and now - self.events[0][0] >= self.WINDOW:
            self.events.popleft()

    def reserve(self, est_tokens: int) -> tuple[list, float]:
        """Block until a request of ``est_tokens`` fits the budget. Returns (entry, seconds waited)."""
        waited = 0.0
        while True:
            with self.lock:
                now = time.monotonic()
                self._prune(now)
                delay = max(0.0, self.blocked_until - now)
                if not delay and self.rpm and len(self.events) >= self.rpm:
                    delay = self.WINDOW - (now - self.events[0][0])
                if not delay and self.tpm and self.events:
                    used = sum(e[1] for e in self.events)
                    if used + min(est_tokens, self.tpm) > self.tpm:
                        # wait until enough old entries fall out of the window
                        need, freed = used + min(est_tokens, self.tpm) - self.tpm, 0
                        for t, tok in self.events:
                            freed += tok
                            if freed >= need:
                                delay = self.WINDOW - (now - t)
                                break
                if delay <= 0:
                    entry = [now, est_tokens]
                    self.events.append(entry)
                    return entry, waited
            delay = min(delay + 0.05, self.WINDOW)
            if delay > 1:
                progress.note_wait("rate_limited", delay, "pacing requests to stay under the per-minute limit")
            time.sleep(delay)
            waited += delay
            if delay > 1:
                progress.resume_after_wait()

    def settle(self, entry: list, actual_tokens: int):
        with self.lock:
            entry[1] = actual_tokens

    def block_for(self, seconds: float):
        with self.lock:
            self.blocked_until = max(self.blocked_until, time.monotonic() + seconds)


_throttles: dict[str, Throttle] = {}
_reg_lock = threading.Lock()


def throttle_for(provider: str, model: str) -> Throttle:
    key = f"{provider}:{model}"  # free-tier quotas are per model
    with _reg_lock:
        if key not in _throttles:
            s = get_settings()
            _throttles[key] = Throttle(
                rpm=getattr(s, f"{provider}_rpm", 0) or 0,
                tpm=getattr(s, f"{provider}_tpm", 0) or 0,
                concurrency=getattr(s, f"{provider}_max_concurrency", 0) or 0,
            )
        return _throttles[key]


def reset_throttles():
    with _reg_lock:
        _throttles.clear()


# classify(exc) -> ("rate" | "daily" | "transient" | None, retry_after_seconds | None)
Classifier = Callable[[Exception], tuple[str | None, float | None]]


def call_with_limits(provider: str, model: str, est_tokens: int, fn: Callable[[], T], classify: Classifier,
                     actual_tokens: Callable[[T], int] = lambda r: 0) -> tuple[T, float]:
    """Run ``fn`` under the provider's throttle with 429/5xx retries. Returns (result, seconds waited)."""
    th = throttle_for(provider, model)
    max_wait = get_settings().rate_limit_max_wait_s
    waited, attempt = 0.0, 0
    while True:
        with th.slot():
            entry, w = th.reserve(est_tokens)
            waited += w
            try:
                result = fn()
                th.settle(entry, actual_tokens(result) or est_tokens)
                return result, waited
            except Exception as e:
                kind, retry_after = classify(e)
                if kind is None:
                    raise
                if kind == "daily" or (retry_after and retry_after > max_wait):
                    raise DailyQuotaExceeded(
                        f"{provider}:{model} daily quota exhausted (retry in {retry_after or '?'}s). "
                        f"Upgrade the plan, switch model, or try again later. Original error: {e}"[:600]) from e
                delay = retry_after if retry_after else min(60.0, 2 ** attempt + random.random())
                if kind == "rate":
                    th.block_for(delay)  # pause every thread hitting this model
                log.warning("%s %s from %s:%s, waiting %.1fs (attempt %d)", kind, type(e).__name__, provider, model,
                            delay, attempt + 1)
        if waited + delay > max_wait:
            raise RateLimitTimeout(f"{provider}:{model} still rate-limited after waiting {waited:.0f}s") from None
        progress.note_wait("rate_limited" if kind == "rate" else "retrying", delay,
                           f"{provider} returned {'429 Too Many Requests' if kind == 'rate' else 'an error'}; retry {attempt + 1}")
        time.sleep(delay)
        progress.resume_after_wait()
        waited += delay
        attempt += 1


def estimate_tokens(system: str, messages: list, tools: list | None, reserve_output: int = 400) -> int:
    import json

    chars = len(system) + len(json.dumps(messages, default=str)) + (len(json.dumps(tools)) if tools else 0)
    return chars // 4 + reserve_output

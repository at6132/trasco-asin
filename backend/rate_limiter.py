"""Thread-safe rate limiters for Keepa (token-bucket) and Anthropic (RPM)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

log = logging.getLogger(__name__)


class TokenBucketLimiter:
    """
    Token-bucket rate limiter for Keepa.

    - ``acquire()`` blocks until a token is available.
    - ``report_response(data)`` reads Keepa's ``tokensLeft`` / ``refillIn``
      from a response and pauses all threads when the bucket is near-empty.
    - Thread-safe via a single lock + condition variable.
    """

    def __init__(self, tokens_per_minute: float = 60.0) -> None:
        self._interval = 60.0 / max(tokens_per_minute, 1.0)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._last = 0.0
        self._paused_until = 0.0

    def acquire(self) -> None:
        with self._cond:
            while True:
                now = time.time()
                if now < self._paused_until:
                    self._cond.wait(timeout=self._paused_until - now + 0.05)
                    continue
                gap = self._interval - (now - self._last)
                if gap > 0:
                    self._cond.wait(timeout=gap + 0.01)
                    continue
                self._last = time.time()
                self._cond.notify_all()
                return

    def report_response(self, data: dict[str, Any]) -> None:
        tokens = data.get("tokensLeft")
        refill_ms = data.get("refillIn")
        if tokens is not None and tokens < 2 and refill_ms is not None:
            wait = min(float(refill_ms) / 1000.0 + 0.25, 90.0)
            with self._cond:
                self._paused_until = time.time() + wait
                self._cond.notify_all()
                log.info(
                    "Keepa tokens near-zero (%s left, refill in %sms) — pausing %.1fs",
                    tokens,
                    refill_ms,
                    wait,
                )


class RpmLimiter:
    """
    Sliding-window RPM limiter for Anthropic Haiku.

    Ensures no more than ``requests_per_minute`` calls proceed within any 60s window.
    ``acquire()`` blocks until a slot is available.
    """

    def __init__(self, requests_per_minute: int = 50) -> None:
        self._rpm = max(requests_per_minute, 1)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._timestamps: list[float] = []

    def acquire(self) -> None:
        with self._cond:
            while True:
                now = time.time()
                cutoff = now - 60.0
                self._timestamps = [
                    t for t in self._timestamps if t > cutoff
                ]
                if len(self._timestamps) < self._rpm:
                    self._timestamps.append(now)
                    self._cond.notify_all()
                    return
                oldest = self._timestamps[0]
                wait = oldest + 60.0 - now + 0.05
                if wait > 0:
                    self._cond.wait(timeout=wait)

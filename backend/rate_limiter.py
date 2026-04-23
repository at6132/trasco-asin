"""Thread-safe reactive rate limiter for Keepa (server-signal driven)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

log = logging.getLogger(__name__)


class KeepaReactiveLimiter:
    """
    Reactive pacer driven by Keepa's ``tokensLeft`` / ``refillIn`` response fields.

    - No proactive spacing — workers burst as fast as possible.
    - ``report_response(data)`` reads ``tokensLeft``; when tokens are critically
      low it pauses ALL threads for ``refillIn`` ms so no one fires into an
      empty account.
    - 429s are already retried at the HTTP layer (``_keepa_get_json``); this
      catches the softer "tokens nearly exhausted" signal before hitting 429.
    """

    LOW_TOKEN_THRESHOLD = 5

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._paused_until = 0.0

    def acquire(self) -> None:
        """Block only if the server told us to wait (near-zero tokens)."""
        with self._cond:
            while True:
                now = time.time()
                remaining = self._paused_until - now
                if remaining <= 0:
                    return
                self._cond.wait(timeout=remaining + 0.05)

    def report_response(self, data: dict[str, Any]) -> None:
        """Read Keepa's token state; pause all threads when tokens are critically low."""
        tokens = data.get("tokensLeft")
        refill_ms = data.get("refillIn")

        if tokens is None or not isinstance(tokens, (int, float)):
            return

        if int(tokens) < self.LOW_TOKEN_THRESHOLD and refill_ms is not None:
            wait = min(float(refill_ms) / 1000.0 + 0.5, 90.0)
            with self._cond:
                new_until = time.time() + wait
                if new_until > self._paused_until:
                    self._paused_until = new_until
                    log.info(
                        "Keepa tokens low (%s left, refill in %sms) — pausing all workers %.1fs",
                        tokens,
                        refill_ms,
                        wait,
                    )
                self._cond.notify_all()
        else:
            with self._cond:
                if self._paused_until > 0:
                    self._paused_until = 0.0
                    self._cond.notify_all()

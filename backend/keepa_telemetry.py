"""Global sliding-window stats for live Keepa API usage (all jobs share one API key)."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Optional

_WINDOW_SEC = 60.0

_lock = threading.Lock()
_events: deque[tuple[float, int]] = deque()
_last_tokens_left: Optional[int] = None
_last_refill_rate: Optional[int] = None


def _trim_locked(now: float) -> None:
    cutoff = now - _WINDOW_SEC
    while _events and _events[0][0] < cutoff:
        _events.popleft()


def record_keepa_response(data: dict[str, Any]) -> None:
    """
    Call once per successful live Keepa JSON response (not cache hits).

    Uses ``tokensConsumed`` when Keepa returns it; otherwise assumes ``1``.
    """
    global _last_tokens_left, _last_refill_rate

    raw = data.get("tokensConsumed")
    if isinstance(raw, bool):
        consumed = 1
    elif isinstance(raw, (int, float)):
        consumed = max(1, int(raw))
    else:
        consumed = 1

    now = time.monotonic()
    with _lock:
        tl = data.get("tokensLeft")
        if isinstance(tl, (int, float)):
            _last_tokens_left = int(tl)
        rr = data.get("refillRate")
        if isinstance(rr, (int, float)):
            _last_refill_rate = int(rr)
        _events.append((now, consumed))
        _trim_locked(now)


def get_keepa_telemetry() -> dict[str, Any]:
    """Rolling last-60s aggregate + last-seen account hints from Keepa responses."""
    now = time.monotonic()
    with _lock:
        _trim_locked(now)
        total_60s = sum(c for _, c in _events)
        n = len(_events)
        tl = _last_tokens_left
        rr = _last_refill_rate
    return {
        "keepa_tokens_consumed_last_60s": total_60s,
        "keepa_live_calls_last_60s": n,
        "keepa_tokens_left_last": tl,
        "keepa_refill_rate_last": rr,
    }

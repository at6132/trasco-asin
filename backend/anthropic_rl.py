"""
Process-wide coordination for Anthropic 429/529: all workers wait the same minimum
wall-clock (default 60s) before any thread sends another /v1/messages request.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import httpx

ANTHROPIC_RATE_LIMIT_MAX_RETRIES = 15
ANTHROPIC_RL_MIN_WAIT_SEC = 60.0

_COND = threading.Condition()
_UNTIL: float = 0.0


def await_anthropic_shared_rl() -> None:
    """Block until a shared 429/529 cooldown (triggered by any thread) has ended."""
    with _COND:
        while time.time() < _UNTIL:
            remaining = _UNTIL - time.time()
            _COND.wait(timeout=max(remaining, 0.05))


def bump_and_wait_anthropic_shared_rl(wait_sec: float) -> None:
    """Set / extend the shared wait; this thread and all others wait in lockstep."""
    global _UNTIL
    with _COND:
        new_end = time.time() + max(0.0, wait_sec)
        _UNTIL = max(_UNTIL, new_end)
        _COND.notify_all()
        while time.time() < _UNTIL:
            remaining = _UNTIL - time.time()
            _COND.wait(timeout=max(remaining, 0.05))

def parse_http_retry_after_seconds(resp: httpx.Response) -> Optional[float]:
    """``Retry-After`` as seconds or HTTP-date; cap for safety."""
    raw = (resp.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        sec = float(raw)
        if sec >= 0:
            return min(600.0, sec)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delta = (dt - datetime.now(timezone.utc)).total_seconds()
            if delta > 0:
                return min(600.0, delta)
    except Exception:
        pass
    return None

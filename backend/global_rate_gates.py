"""Process-wide shared reactive limiter for Keepa (all concurrent jobs share one)."""

from __future__ import annotations

import threading
from typing import Optional

from backend.rate_limiter import KeepaReactiveLimiter

_lock = threading.Lock()
_shared_keepa_limiter: Optional[KeepaReactiveLimiter] = None


def get_shared_keepa_reactive_limiter() -> KeepaReactiveLimiter:
    """Return a single reactive limiter for the whole process."""
    global _shared_keepa_limiter
    with _lock:
        if _shared_keepa_limiter is None:
            _shared_keepa_limiter = KeepaReactiveLimiter()
        return _shared_keepa_limiter

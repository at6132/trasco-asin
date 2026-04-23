"""Process-wide rate gates shared by all concurrent pipeline jobs."""

from __future__ import annotations

import threading
from typing import Optional

from backend.rate_limiter import RpmLimiter, TokenBucketLimiter

_lock = threading.Lock()
_shared_keepa_bucket: Optional[TokenBucketLimiter] = None
_shared_keepa_tpm: float = -1.0

_shared_haiku_asin_rpm: Optional[RpmLimiter] = None
_shared_haiku_asin_rpm_cap: int = -1


def get_shared_keepa_token_bucket(tokens_per_minute: float) -> Optional[TokenBucketLimiter]:
    """Return one token bucket per process so every job shares the same Keepa pacing."""
    global _shared_keepa_bucket, _shared_keepa_tpm
    if tokens_per_minute <= 0:
        return None
    tpm = float(tokens_per_minute)
    with _lock:
        if _shared_keepa_bucket is None or _shared_keepa_tpm != tpm:
            _shared_keepa_bucket = TokenBucketLimiter(tpm)
            _shared_keepa_tpm = tpm
        return _shared_keepa_bucket


def get_shared_haiku_asin_validate_rpm_limiter(requests_per_minute: int) -> RpmLimiter:
    """Return one RPM limiter per process for Haiku ASIN validation across all jobs."""
    global _shared_haiku_asin_rpm, _shared_haiku_asin_rpm_cap
    cap = max(1, int(requests_per_minute))
    with _lock:
        if _shared_haiku_asin_rpm is None or _shared_haiku_asin_rpm_cap != cap:
            _shared_haiku_asin_rpm = RpmLimiter(requests_per_minute=cap)
            _shared_haiku_asin_rpm_cap = cap
        return _shared_haiku_asin_rpm

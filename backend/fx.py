"""
Free FX rates via Frankfurter (https://www.frankfurter.app, ECB-sourced, no API key).

We only need ``USD per <ccy>`` (e.g. EUR → USD ≈ 1.08) to convert vendor cost from the
resolver's local marketplace currency into USD, since the **output price columns** are
always pulled from Amazon.com.

Rates are cached for ~24h in the same SQLite cache used for Keepa payloads.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from backend.cache import Cache

logger = logging.getLogger(__name__)

FX_NAMESPACE = "fx_usd_per_ccy_v1"
FX_TTL_SECONDS = 24 * 3600

FRANKFURTER_BASE = "https://api.frankfurter.dev/v1"
_TIMEOUT_SEC = 8.0

# Fallback approximate rates if the network call fails AND no override env var is set.
# Order of magnitude only — used so ROI is at least roughly correct when offline.
_OFFLINE_FALLBACK_USD_PER: dict[str, float] = {
    "EUR": 1.08,
    "GBP": 1.27,
    "CAD": 0.74,
    "JPY": 0.0065,
    "MXN": 0.058,
    "BRL": 0.20,
    "INR": 0.012,
    "AUD": 0.66,
    "CHF": 1.12,
}


def _env_override(ccy: str) -> Optional[float]:
    """Allow ``FX_USD_PER_EUR=1.09`` style overrides for predictable backtests."""
    key = f"FX_USD_PER_{ccy.upper()}"
    raw = os.environ.get(key)
    if not raw:
        return None
    try:
        val = float(raw.strip())
    except ValueError:
        return None
    if val <= 0:
        return None
    return val


def usd_per_currency(
    cache: Optional[Cache],
    ccy: str,
    *,
    timeout: float = _TIMEOUT_SEC,
) -> Optional[float]:
    """USD per 1 unit of ``ccy``. Returns ``None`` only when the code is unknown."""
    code = (ccy or "").strip().upper()
    if not code:
        return None
    if code == "USD":
        return 1.0

    override = _env_override(code)
    if override is not None:
        return override

    if cache is not None:
        cached = cache.get_json(FX_NAMESPACE, code, "v1")
        if isinstance(cached, dict):
            try:
                v = float(cached.get("rate"))
                if v > 0:
                    return v
            except (TypeError, ValueError):
                pass

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            r = client.get(f"{FRANKFURTER_BASE}/latest", params={"base": code, "symbols": "USD"})
            r.raise_for_status()
            data = r.json()
            rate = float((data.get("rates") or {}).get("USD"))
    except Exception as e:
        logger.warning("FX fetch failed for %s via Frankfurter: %s", code, e)
        fallback = _OFFLINE_FALLBACK_USD_PER.get(code)
        if fallback is not None:
            logger.warning("Using offline fallback FX rate for %s: %.4f USD", code, fallback)
            return fallback
        return None

    if rate <= 0:
        return _OFFLINE_FALLBACK_USD_PER.get(code)

    if cache is not None:
        try:
            cache.set_json(FX_NAMESPACE, code, "v1", {"rate": rate}, ttl_seconds=FX_TTL_SECONDS)
        except Exception as e:
            logger.debug("FX cache write failed for %s: %s", code, e)
    return rate

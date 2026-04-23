"""
Keepa HTTP API: product (ASIN / code), batch product, product_finder (query), throttling.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Optional

import httpx

from backend.http_pool import get_keepa_client

log = logging.getLogger(__name__)

if __package__:
    from .cache import (
        Cache,
        get_cached_keepa_by_code,
        get_cached_keepa_product,
        set_cached_keepa_by_code,
        set_cached_keepa_product,
    )
    from .keepa_telemetry import record_keepa_response
else:
    from cache import (
        Cache,
        get_cached_keepa_by_code,
        get_cached_keepa_product,
        set_cached_keepa_by_code,
        set_cached_keepa_product,
    )
    from keepa_telemetry import record_keepa_response

KEEPA_API_BASE = "https://api.keepa.com"
KEEPA_PRODUCT_URL = f"{KEEPA_API_BASE}/product"
KEEPA_QUERY_URL = f"{KEEPA_API_BASE}/query"

# Keepa product_finder expects sort as nested arrays, e.g. [["current_SALES","asc"]].
KEEPA_FINDER_SORT: list[list[str]] = [["current_SALES", "asc"]]

# ASINs are 10-character Amazon identifiers (often B0…; Keepa also returns ISBN-10-style IDs).
_KEEPA_ASIN_TOKEN = re.compile(r"^[A-Z0-9]{10}$")


def is_keepa_style_asin(token: str) -> bool:
    t = token.strip().upper()
    return bool(_KEEPA_ASIN_TOKEN.match(t))

DOMAIN_COM = 1
DOMAIN_UK = 2
DOMAIN_DE = 3
DOMAIN_FR = 4
DOMAIN_JP = 5
DOMAIN_CA = 6
DOMAIN_IT = 8
DOMAIN_ES = 9
DOMAIN_IN = 10
DOMAIN_MX = 11
DOMAIN_BR = 12


class KeepaError(Exception):
    pass


_KEEPA_RATE_LIMIT_MAX_RETRIES = 15
# Minimum wall-clock wait between 429/503 attempts; all workers share one deadline.
_KEEPA_RL_MIN_WAIT_SEC = 60.0
_KEEPA_SHARED_RL_COND = threading.Condition()
_KEEPA_SHARED_RL_UNTIL: float = 0.0


def _await_keepa_shared_rl() -> None:
    """All Keepa call paths block here until a process-wide 429/503 cooldown ends."""
    with _KEEPA_SHARED_RL_COND:
        while time.time() < _KEEPA_SHARED_RL_UNTIL:
            remaining = _KEEPA_SHARED_RL_UNTIL - time.time()
            _KEEPA_SHARED_RL_COND.wait(timeout=max(remaining, 0.05))


def _bump_and_wait_keepa_shared_rl(wait_sec: float) -> None:
    """
    A 429/503 extends the shared wait to at least ``wait_sec`` from now (max with any
    in-flight wait). This thread and every other thread must wait it out in harmony.
    """
    global _KEEPA_SHARED_RL_UNTIL
    with _KEEPA_SHARED_RL_COND:
        new_end = time.time() + max(0.0, wait_sec)
        _KEEPA_SHARED_RL_UNTIL = max(_KEEPA_SHARED_RL_UNTIL, new_end)
        _KEEPA_SHARED_RL_COND.notify_all()
        while time.time() < _KEEPA_SHARED_RL_UNTIL:
            remaining = _KEEPA_SHARED_RL_UNTIL - time.time()
            _KEEPA_SHARED_RL_COND.wait(timeout=max(remaining, 0.05))


def _parse_retry_after_seconds(resp: httpx.Response) -> Optional[float]:
    """Parse ``Retry-After`` (seconds or HTTP-date); cap for safety."""
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


def _keepa_get_json(
    client: httpx.Client,
    url: str,
    params: dict[str, Any],
    *,
    context: str,
    max_retries: int = _KEEPA_RATE_LIMIT_MAX_RETRIES,
) -> dict[str, Any]:
    """
    GET JSON from Keepa; on 429 (rate limit) or 503 (overload), all workers coordinate:
    a shared wait of at least 60s (or longer if ``Retry-After`` says so), up to
    ``max_retries`` per request before failing.
    """
    attempt = 0
    while True:
        attempt += 1
        _await_keepa_shared_rl()
        r = client.get(url, params=params)
        transient = r.status_code == 429 or r.status_code == 503
        if transient:
            if attempt > max_retries:
                snippet = (r.text or "").strip().replace("\n", " ")[:400]
                raise KeepaError(
                    f"HTTP {r.status_code} after {max_retries} rate-limit tries ({context}): {snippet}"
                )
            parsed = _parse_retry_after_seconds(r)
            wait = max(
                _KEEPA_RL_MIN_WAIT_SEC,
                parsed if parsed is not None else 0.0,
            )
            log.warning(
                "Keepa HTTP %s — shared cooldown %.0fs+ then retry (attempt %s/%s) (%s)",
                r.status_code,
                _KEEPA_RL_MIN_WAIT_SEC,
                attempt,
                max_retries,
                context,
            )
            _bump_and_wait_keepa_shared_rl(wait)
            continue
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise KeepaError(f"Keepa response was not a JSON object ({context})")
        return data


class KeepaThrottle:
    """Reactive throttle: burst as fast as possible, pause only when Keepa says tokens are low.

    The shared ``KeepaReactiveLimiter`` reads ``tokensLeft`` / ``refillIn`` from
    each response and pauses all threads when the account is near-empty.  HTTP 429s
    are already retried inside ``_keepa_get_json``.  No proactive spacing.
    """

    def __init__(self, *, reactive_limiter: Optional[Any] = None) -> None:
        self._limiter = reactive_limiter

    def before_request(self) -> None:
        _await_keepa_shared_rl()
        if self._limiter is not None:
            self._limiter.acquire()

    def after_response(self, data: dict[str, Any]) -> None:
        if self._limiter is not None:
            self._limiter.report_response(data)


def _raise_if_error(data: dict[str, Any]) -> None:
    if isinstance(data, dict) and data.get("error"):
        raise KeepaError(str(data.get("error")))


def fetch_keepa_product(
    api_key: str,
    asin: str,
    domain: int = DOMAIN_COM,
    *,
    stats_days: int = 90,
    history: int = 0,
    cache: Optional[Cache] = None,
    cache_ttl_seconds: int = 86_400,
    timeout: float = 60.0,
    throttle: Optional[KeepaThrottle] = None,
    on_response: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    asin = asin.strip().upper()
    if not is_keepa_style_asin(asin):
        raise KeepaError(f"Invalid ASIN format: {asin!r}")

    if cache:
        hit = get_cached_keepa_product(cache, domain, asin)
        if hit is not None:
            return hit

    if throttle:
        throttle.before_request()
    params = {
        "key": api_key,
        "domain": domain,
        "asin": asin,
        "stats": stats_days,
        "history": history,
    }
    data = _keepa_get_json(
        get_keepa_client(),
        KEEPA_PRODUCT_URL,
        params,
        context=f"product asin={asin} domain={domain}",
    )

    _raise_if_error(data)
    record_keepa_response(data)
    if on_response:
        on_response(data)
    if throttle:
        throttle.after_response(data)

    if cache and isinstance(data, dict):
        set_cached_keepa_product(cache, domain, asin, data, ttl_seconds=cache_ttl_seconds)

    if log.isEnabledFor(logging.DEBUG):
        fp = first_product(data)
        t0 = (fp.get("title") or "")[:100] if isinstance(fp, dict) else None
        log.debug(
            "keepa product domain=%s asin=%s tokensLeft=%s title0=%r",
            domain,
            asin,
            data.get("tokensLeft") if isinstance(data, dict) else None,
            t0,
        )

    return data


def fetch_keepa_products_batch(
    api_key: str,
    asins: list[str],
    domain: int = DOMAIN_COM,
    *,
    stats_days: int = 90,
    history: int = 0,
    cache: Optional[Cache] = None,
    cache_ttl_seconds: int = 86_400,
    timeout: float = 90.0,
    throttle: Optional[KeepaThrottle] = None,
    on_response: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, dict[str, Any]]:
    """
    Single Keepa product request with comma-separated ASINs (max 100).
    Returns map asin → product dict. Caches each product individually on hit.
    """
    clean = [a.strip().upper() for a in asins if a and is_keepa_style_asin(a)]
    if not clean:
        return {}
    uncached: list[str] = []
    out: dict[str, dict[str, Any]] = {}
    for a in clean:
        if cache:
            hit = get_cached_keepa_product(cache, domain, a)
            if hit is not None:
                p = first_product(hit)
                if p and p.get("asin"):
                    out[p["asin"]] = p
                continue
        uncached.append(a)
    if not uncached:
        return out

    for i in range(0, len(uncached), 100):
        chunk = uncached[i : i + 100]
        if throttle:
            throttle.before_request()
        params = {
            "key": api_key,
            "domain": domain,
            "asin": ",".join(chunk),
            "stats": stats_days,
            "history": history,
        }
        data = _keepa_get_json(
            get_keepa_client(),
            KEEPA_PRODUCT_URL,
            params,
            context=f"batch_product domain={domain} chunk={len(chunk)}",
        )

        _raise_if_error(data)
        record_keepa_response(data)
        if on_response:
            on_response(data)
        if throttle:
            throttle.after_response(data)

        for p in data.get("products") or []:
            if isinstance(p, dict) and p.get("asin"):
                out[str(p["asin"])] = p
                if cache:
                    single = {
                        "products": [p],
                        "tokensLeft": data.get("tokensLeft"),
                        "refillIn": data.get("refillIn"),
                    }
                    set_cached_keepa_product(cache, domain, str(p["asin"]), single, ttl_seconds=cache_ttl_seconds)
        if log.isEnabledFor(logging.DEBUG):
            titles = [
                (str(p.get("title") or "")[:60] if isinstance(p, dict) else "")
                for p in (data.get("products") or [])[:3]
                if isinstance(p, dict)
            ]
            log.debug(
                "keepa batch domain=%s chunk=%s returned_products=%s tokensLeft=%s sample_titles=%s",
                domain,
                len(chunk),
                len(data.get("products") or []),
                data.get("tokensLeft"),
                titles,
            )
    return out


def fetch_keepa_product_by_code(
    api_key: str,
    product_code: str,
    domain: int = DOMAIN_COM,
    *,
    stats_days: int = 90,
    history: int = 0,
    cache: Optional[Cache] = None,
    cache_ttl_seconds: int = 86_400,
    timeout: float = 60.0,
    throttle: Optional[KeepaThrottle] = None,
    on_response: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    digits = re.sub(r"\D", "", str(product_code))
    if len(digits) < 8 or len(digits) > 14:
        raise KeepaError(f"Invalid product code for Keepa (8–14 digits): {product_code!r}")

    if cache:
        hit = get_cached_keepa_by_code(cache, domain, digits)
        if hit is not None:
            return hit

    if throttle:
        throttle.before_request()
    params = {
        "key": api_key,
        "domain": domain,
        "code": digits,
        "stats": stats_days,
        "history": history,
    }
    data = _keepa_get_json(
        get_keepa_client(),
        KEEPA_PRODUCT_URL,
        params,
        context=f"product_by_code domain={domain} code={digits}",
    )

    _raise_if_error(data)
    record_keepa_response(data)
    if on_response:
        on_response(data)
    if throttle:
        throttle.after_response(data)

    if cache and isinstance(data, dict):
        set_cached_keepa_by_code(cache, domain, digits, data, ttl_seconds=cache_ttl_seconds)

    if log.isEnabledFor(logging.DEBUG):
        fp = first_product(data)
        t0 = (fp.get("title") or "")[:100] if isinstance(fp, dict) else None
        log.debug(
            "keepa by_code domain=%s code=%s tokensLeft=%s title0=%r",
            domain,
            digits,
            data.get("tokensLeft") if isinstance(data, dict) else None,
            t0,
        )

    return data


def product_finder_asins(
    api_key: str,
    domain: int,
    selection: dict[str, Any],
    *,
    n_products: int = 20,
    timeout: float = 90.0,
    throttle: Optional[KeepaThrottle] = None,
    on_response: Optional[Callable[[dict[str, Any]], None]] = None,
) -> tuple[list[str], dict[str, Any]]:
    """
    Keepa product_finder → `GET /query` with JSON `selection`.
    Returns (asin_list, raw_json).
    """
    sel = dict(selection)
    sel.setdefault("perPage", min(n_products, 100))
    if throttle:
        throttle.before_request()
    payload = {
        "key": api_key,
        "domain": domain,
        "selection": json.dumps(sel, separators=(",", ":")),
    }
    data = _keepa_get_json(
        get_keepa_client(),
        KEEPA_QUERY_URL,
        payload,
        context=f"product_finder domain={domain}",
    )

    _raise_if_error(data)
    record_keepa_response(data)
    if on_response:
        on_response(data)
    if throttle:
        throttle.after_response(data)

    raw_list = data.get("asinList")
    if not isinstance(raw_list, list):
        return [], data
    asins = [
        str(x).strip().upper()
        for x in raw_list
        if x and is_keepa_style_asin(str(x))
    ]
    if log.isEnabledFor(logging.DEBUG):
        sel_log = {k: v for k, v in sel.items() if k != "key"}
        log.debug(
            "keepa product_finder domain=%s tokensLeft=%s raw_asin_list_len=%s filtered=%s "
            "selection=%s sample_filtered=%s",
            domain,
            data.get("tokensLeft"),
            len(raw_list) if isinstance(raw_list, list) else None,
            len(asins),
            sel_log,
            asins[:10],
        )
    return asins, data


def first_product(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    products = payload.get("products")
    if not products or not isinstance(products, list):
        return None
    first = products[0]
    return first if isinstance(first, dict) else None


def best_product_by_title(
    payload: dict[str, Any], sheet_title: Optional[str]
) -> Optional[dict[str, Any]]:
    """Pick the product whose title best matches sheet_title; falls back to first."""
    products = payload.get("products")
    if not products or not isinstance(products, list):
        return None
    dicts = [p for p in products if isinstance(p, dict)]
    if not dicts:
        return None
    if len(dicts) == 1 or not sheet_title or not sheet_title.strip():
        return dicts[0]

    from backend.validator import title_similarity

    best, best_score = dicts[0], -1.0
    for p in dicts:
        t = p.get("title")
        if not isinstance(t, str):
            continue
        sc = title_similarity(sheet_title, t)
        if sc > best_score:
            best, best_score = p, sc
    return best


def _load_dotenv_simple() -> None:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    path = os.path.join(root, ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


if __name__ == "__main__":
    _load_dotenv_simple()
    if __package__:
        from .cache import Cache as _Cache
    else:
        from cache import Cache as _Cache

    key = os.environ.get("KEEPA_API_KEY", "").strip()
    if not key:
        print("Set KEEPA_API_KEY in .env or the environment, then re-run.")
        sys.exit(1)

    test_asin = (sys.argv[1] if len(sys.argv) > 1 else "B0D1XD1ZV3").strip().upper()
    c = _Cache()
    th = KeepaThrottle()
    try:
        payload = fetch_keepa_product(key, test_asin, DOMAIN_COM, cache=c, history=0, throttle=th)
    except Exception as e:
        print("Request failed:", e)
        sys.exit(2)

    prod = first_product(payload)
    print("tokensLeft:", payload.get("tokensLeft"))
    if prod:
        print("ASIN:", prod.get("asin"))
        title = prod.get("title") or ""
        print("title:", title[:120])
    else:
        print("No product in response (check ASIN / marketplace).")
    c.close()

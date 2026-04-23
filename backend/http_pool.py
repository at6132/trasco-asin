"""Process-wide httpx connection pools for Keepa and Anthropic APIs."""

from __future__ import annotations

import threading

import httpx

_lock = threading.Lock()

_keepa_pool: httpx.Client | None = None
_anthropic_pool: httpx.Client | None = None


def get_keepa_client() -> httpx.Client:
    global _keepa_pool
    with _lock:
        if _keepa_pool is None or _keepa_pool.is_closed:
            _keepa_pool = httpx.Client(
                timeout=90.0,
                limits=httpx.Limits(
                    max_connections=80,
                    max_keepalive_connections=40,
                    keepalive_expiry=120,
                ),
                http2=False,
            )
        return _keepa_pool


def get_anthropic_client() -> httpx.Client:
    global _anthropic_pool
    with _lock:
        if _anthropic_pool is None or _anthropic_pool.is_closed:
            _anthropic_pool = httpx.Client(
                timeout=120.0,
                limits=httpx.Limits(
                    max_connections=60,
                    max_keepalive_connections=30,
                    keepalive_expiry=120,
                ),
                http2=False,
            )
        return _anthropic_pool


def close_pools() -> None:
    global _keepa_pool, _anthropic_pool
    with _lock:
        if _keepa_pool is not None:
            try:
                _keepa_pool.close()
            except Exception:
                pass
            _keepa_pool = None
        if _anthropic_pool is not None:
            try:
                _anthropic_pool.close()
            except Exception:
                pass
            _anthropic_pool = None

"""
SQLite cache for Keepa JSON payloads and optional parse metadata.
All other modules read/write through this layer.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "trasco_cache.sqlite3"


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


class Cache:
    """Small SQLite wrapper keyed by (resource, primary_key, secondary_key)."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._conn = _connect(self.db_path)
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _init_schema(self) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kv_cache (
                    namespace TEXT NOT NULL,
                    key_a TEXT NOT NULL,
                    key_b TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY (namespace, key_a, key_b)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_kv_expires
                ON kv_cache (namespace, expires_at)
                """
            )

    def get_json(self, namespace: str, key_a: str, key_b: str = "") -> Optional[Any]:
        now = time.time()
        cur = self._conn.execute(
            """
            SELECT payload FROM kv_cache
            WHERE namespace = ? AND key_a = ? AND key_b = ? AND expires_at > ?
            """,
            (namespace, key_a, key_b, now),
        )
        row = cur.fetchone()
        if not row:
            return None
        return json.loads(row["payload"])

    def set_json(
        self,
        namespace: str,
        key_a: str,
        key_b: str,
        payload: Any,
        ttl_seconds: int,
    ) -> None:
        now = time.time()
        expires = now + max(1, ttl_seconds)
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO kv_cache (namespace, key_a, key_b, payload, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, key_a, key_b) DO UPDATE SET
                    payload = excluded.payload,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (namespace, key_a, key_b, json.dumps(payload), now, expires),
            )

    def delete_namespace_prefix(self, namespace: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM kv_cache WHERE namespace = ?", (namespace,))

    def purge_expired(self) -> int:
        now = time.time()
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM kv_cache WHERE expires_at <= ?", (now,))
            return cur.rowcount or 0


# Keepa-specific helpers (thin convenience on top of kv_cache)

KEEPA_NAMESPACE = "keepa_product_v1"


def keepa_cache_key(domain: int, asin: str) -> tuple[str, str, str]:
    return KEEPA_NAMESPACE, asin.upper().strip(), str(int(domain))


def get_cached_keepa_product(cache: Cache, domain: int, asin: str) -> Optional[dict[str, Any]]:
    key = keepa_cache_key(domain, asin)
    data = cache.get_json(*key)
    if isinstance(data, dict):
        return data
    return None


def set_cached_keepa_product(
    cache: Cache,
    domain: int,
    asin: str,
    payload: dict[str, Any],
    ttl_seconds: int,
) -> None:
    ns, ka, kb = keepa_cache_key(domain, asin)
    cache.set_json(ns, ka, kb, payload, ttl_seconds=ttl_seconds)


KEEPA_CODE_NAMESPACE = "keepa_product_code_v1"


def keepa_code_cache_key(domain: int, code: str) -> tuple[str, str, str]:
    norm = re.sub(r"\D", "", code)
    return KEEPA_CODE_NAMESPACE, norm, str(int(domain))


def get_cached_keepa_by_code(cache: Cache, domain: int, code: str) -> Optional[dict[str, Any]]:
    ns, ka, kb = keepa_code_cache_key(domain, code)
    data = cache.get_json(ns, ka, kb)
    if isinstance(data, dict):
        return data
    return None


def set_cached_keepa_by_code(
    cache: Cache,
    domain: int,
    code: str,
    payload: dict[str, Any],
    ttl_seconds: int,
) -> None:
    ns, ka, kb = keepa_code_cache_key(domain, code)
    cache.set_json(ns, ka, kb, payload, ttl_seconds=ttl_seconds)


# SKU / MPN → resolved ASIN (product_finder tier), avoids repeat finder calls
KEEPA_SKU_RESOLVE_NAMESPACE = "keepa_sku_resolve_v1"


def sku_resolve_cache_key(domain: int, brand_norm: str, sku_norm: str) -> tuple[str, str, str]:
    return KEEPA_SKU_RESOLVE_NAMESPACE, str(int(domain)), f"{brand_norm}|{sku_norm}"


def get_cached_sku_resolve(cache: Cache, domain: int, brand_norm: str, sku_norm: str) -> Optional[dict[str, Any]]:
    ns, ka, kb = sku_resolve_cache_key(domain, brand_norm, sku_norm)
    data = cache.get_json(ns, ka, kb)
    if isinstance(data, dict):
        return data
    return None


def set_cached_sku_resolve(
    cache: Cache,
    domain: int,
    brand_norm: str,
    sku_norm: str,
    payload: dict[str, Any],
    ttl_seconds: int,
) -> None:
    ns, ka, kb = sku_resolve_cache_key(domain, brand_norm, sku_norm)
    cache.set_json(ns, ka, kb, payload, ttl_seconds=ttl_seconds)

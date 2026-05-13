"""Final ASIN resolution pass: every resolved ASIN becomes a US (Amazon.com) ASIN.

Trasco users are US-based, so the only ASIN that matters in the output is the one a US
buyer can actually purchase / resell on Amazon.com. The multi-marketplace resolver
(``backend.main`` / ``backend.resolution``) is allowed to use the local marketplace for
matching — Amazon.de gives much better matches for a German Philips offer, etc. — but
the **output ASIN is normalized to .com here** before any other downstream step runs.

Pipeline order:
    assemble  →  **normalize_sections_to_us_marketplace**  →  LLM validate
              →  finder retry (for LLM-rejected)  →  price history enrichment

For every row with a resolved ASIN of any confidence (HIGH / MEDIUM / LOW):
  1. Batch-fetch on ``domain=1`` (Amazon.com) with ``history=1&buybox=1``.
  2. If the ASIN comes back with real US data → keep it.
  3. If not, look up the row's UPC/EAN on .com — many EU products are cross-listed
     under a different US ASIN. Swap to the discovered US ASIN if found.
  4. If still no US match → set ASIN cell to ``Not found (Foreign)`` and confidence
     to ``NOT FOUND (Foreign)``.

The Keepa product object for each row's final US ASIN is stashed on the line under
``__trasco_us_prod`` so downstream steps (LLM ASIN validation, price history) can
reuse it without paying for another Keepa fetch.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from backend.cache import Cache
from backend.errors import JobCancelled
from backend.lookup import (
    KeepaError,
    KeepaThrottle,
    fetch_keepa_product_by_code,
    fetch_keepa_products_batch,
    first_product,
    is_keepa_style_asin,
)

logger = logging.getLogger(__name__)

ProgressCb = Optional[Callable[[str, str, int, int], None]]

# Confidence levels we attempt to US-normalize. Anything else (NOT FOUND etc.) is
# untouched since there's nothing to convert.
_CONF_NORMALIZE = frozenset({"HIGH", "MEDIUM", "LOW"})

NOT_FOUND_FOREIGN_ASIN = "Not found (Foreign)"
NOT_FOUND_FOREIGN_CONF = "NOT FOUND (Foreign)"

_US_DOMAIN = 1


def _append_trace(line: dict[str, Any], meta: dict[str, str], note: str) -> None:
    log_h = meta.get("log_h")
    if not log_h or not note:
        return
    prev = str(line.get(log_h) or "")
    line[log_h] = (prev + ("|" if prev else "") + note).strip()


def is_present_on_us(prod: Optional[dict[str, Any]]) -> bool:
    """Keepa returned this ASIN with real US data (not an empty cross-marketplace stub)."""
    if not isinstance(prod, dict):
        return False
    title = prod.get("title")
    if isinstance(title, str) and title.strip():
        return True
    csv = prod.get("csv")
    if isinstance(csv, list) and any(isinstance(lane, list) and lane for lane in csv):
        return True
    bb = prod.get("buyBoxPrice")
    if isinstance(bb, int) and bb >= 0:
        return True
    return False


def _mark_not_found_foreign(line: dict[str, Any], meta: dict[str, str], *, gtin: str) -> None:
    line[meta["asin_h"]] = NOT_FOUND_FOREIGN_ASIN
    line[meta["conf_h"]] = NOT_FOUND_FOREIGN_CONF
    line["__trasco_us_prod"] = None
    _append_trace(line, meta, f"us=foreign_not_found|gtin={gtin or ''}")


def _gtin_lookup_us(
    gtin: str,
    *,
    api_key: str,
    cache: Cache,
    cache_ttl_seconds: int,
    throttle: KeepaThrottle,
) -> Optional[dict[str, Any]]:
    """UPC/EAN → first Keepa product on Amazon.com (with US data) or None."""
    if not gtin:
        return None
    try:
        payload = fetch_keepa_product_by_code(
            api_key,
            gtin,
            _US_DOMAIN,
            cache=cache,
            cache_ttl_seconds=cache_ttl_seconds,
            history=0,
            throttle=throttle,
        )
    except KeepaError as e:
        logger.info("US GTIN lookup failed gtin=%s: %s", gtin, e)
        return None
    except Exception as e:
        logger.warning("US GTIN lookup error gtin=%s: %s", gtin, e)
        return None
    prod = first_product(payload)
    return prod if is_present_on_us(prod) else None


def normalize_sections_to_us_marketplace(
    sections: list[tuple[str, list[str], list[dict[str, Any]], dict[str, str]]],
    *,
    api_key: str,
    cache: Cache,
    cache_ttl_seconds: int,
    throttle: KeepaThrottle,
    keepa_parallel_max: int,
    tracker: Any,
    progress: ProgressCb,
    should_cancel: Optional[Callable[[], bool]],
) -> None:
    """Mutate every section's row dicts so the ASIN column is always a US ASIN or
    ``Not found (Foreign)``. The matching US Keepa product is stashed at
    ``line["__trasco_us_prod"]`` for downstream reuse.
    """

    def _check() -> None:
        if should_cancel and should_cancel():
            raise JobCancelled()

    # ─── Collect every row with a resolved ASIN of any confidence ──────────────────────
    candidates: list[tuple[dict[str, Any], dict[str, str], str]] = []
    for _sheet_title, _headers, out_rows, meta in sections:
        asin_h = meta["asin_h"]
        conf_h = meta["conf_h"]
        for line in out_rows:
            conf = str(line.get(conf_h) or "").strip().upper()
            if conf not in _CONF_NORMALIZE:
                continue
            asin = str(line.get(asin_h) or "").strip().upper()
            if not asin or not is_keepa_style_asin(asin):
                continue
            candidates.append((line, meta, asin))

    if not candidates:
        return

    unique_asins = sorted({asin for _line, _meta, asin in candidates})
    total_chunks = max(1, (len(unique_asins) + 99) // 100)
    if progress:
        progress(
            "us_normalize",
            f"Verifying {len(unique_asins)} ASIN(s) on Amazon.com…",
            0,
            total_chunks,
        )

    # ─── Pass 1: batch-fetch all candidate ASINs on Amazon.com with full data ──────────
    us_products: dict[str, dict[str, Any]] = {}
    done_chunks = 0
    for i in range(0, len(unique_asins), 100):
        chunk = unique_asins[i : i + 100]
        _check()
        with tracker.keepa_slot():
            batch_map = fetch_keepa_products_batch(
                api_key,
                chunk,
                _US_DOMAIN,
                cache=cache,
                cache_ttl_seconds=cache_ttl_seconds,
                history=1,
                buybox=1,
                throttle=throttle,
            )
        for a, prod in batch_map.items():
            us_products[str(a).strip().upper()] = prod
        done_chunks += 1
        if progress:
            progress(
                "us_normalize",
                f"Verifying ASINs on Amazon.com — batch {done_chunks}/{total_chunks}…",
                done_chunks,
                total_chunks,
            )

    # ─── Pass 2: split rows into "on US" / "needs GTIN lookup" / "no GTIN → foreign" ──
    needs_gtin: dict[str, list[tuple[dict[str, Any], dict[str, str], str]]] = {}
    on_us: list[tuple[dict[str, Any], dict[str, str], str]] = []
    for line, meta, asin in candidates:
        prod = us_products.get(asin)
        if is_present_on_us(prod):
            line["__trasco_us_prod"] = prod
            _append_trace(line, meta, f"us=ok|us_asin={asin}")
            on_us.append((line, meta, asin))
            continue
        gtin = str(line.get("__trasco_gtin") or "").strip()
        if not gtin:
            _mark_not_found_foreign(line, meta, gtin="")
            continue
        needs_gtin.setdefault(gtin, []).append((line, meta, asin))

    # ─── Pass 3: concurrent UPC/EAN lookups on Amazon.com for the rest ────────────────
    if needs_gtin:
        unique_gtins = sorted(needs_gtin.keys())
        n_workers = max(1, min(int(keepa_parallel_max or 1), len(unique_gtins)))
        if progress:
            progress(
                "us_normalize_gtin",
                f"Cross-referencing {len(unique_gtins)} non-US ASIN(s) via UPC/EAN…",
                0,
                len(unique_gtins),
            )

        gtin_to_us_prod: dict[str, Optional[dict[str, Any]]] = {}
        done_counter = [0]

        def _resolve_one(gtin: str) -> tuple[str, Optional[dict[str, Any]]]:
            with tracker.keepa_slot():
                prod = _gtin_lookup_us(
                    gtin,
                    api_key=api_key,
                    cache=cache,
                    cache_ttl_seconds=cache_ttl_seconds,
                    throttle=throttle,
                )
            done_counter[0] += 1
            if progress:
                progress(
                    "us_normalize_gtin",
                    f"UPC/EAN cross-reference {done_counter[0]}/{len(unique_gtins)}…",
                    done_counter[0],
                    len(unique_gtins),
                )
            return gtin, prod

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            for gtin, prod in pool.map(_resolve_one, unique_gtins):
                gtin_to_us_prod[gtin] = prod

        # Swap or mark foreign, and collect any newly discovered US ASINs that need
        # a full data fetch (the GTIN endpoint returns minimal data).
        newly_needed: set[str] = set()
        for gtin, rows in needs_gtin.items():
            us_prod = gtin_to_us_prod.get(gtin)
            if not us_prod:
                for line, meta, _old in rows:
                    _mark_not_found_foreign(line, meta, gtin=gtin)
                continue
            new_asin_raw = us_prod.get("asin")
            new_asin = (
                str(new_asin_raw).strip().upper()
                if isinstance(new_asin_raw, str) and is_keepa_style_asin(new_asin_raw)
                else ""
            )
            if not new_asin:
                for line, meta, _old in rows:
                    _mark_not_found_foreign(line, meta, gtin=gtin)
                continue
            for line, meta, old_asin in rows:
                line[meta["asin_h"]] = new_asin
                line["__trasco_us_prod"] = us_prod  # may be replaced by richer fetch below
                _append_trace(
                    line,
                    meta,
                    f"us=foreign_replaced|orig_asin={old_asin}|us_asin={new_asin}|gtin={gtin}",
                )
                on_us.append((line, meta, new_asin))
            if new_asin not in us_products:
                newly_needed.add(new_asin)

        # ─── Pass 4: refetch the swapped ASINs with history=1&buybox=1 ──────────────
        if newly_needed:
            extra = sorted(newly_needed)
            n_extra_chunks = max(1, (len(extra) + 99) // 100)
            if progress:
                progress(
                    "us_normalize",
                    f"Fetching full US data for {len(extra)} swapped ASIN(s)…",
                    0,
                    n_extra_chunks,
                )
            done_extra = 0
            for i in range(0, len(extra), 100):
                chunk = extra[i : i + 100]
                _check()
                with tracker.keepa_slot():
                    batch_map = fetch_keepa_products_batch(
                        api_key,
                        chunk,
                        _US_DOMAIN,
                        cache=cache,
                        cache_ttl_seconds=cache_ttl_seconds,
                        history=1,
                        buybox=1,
                        throttle=throttle,
                    )
                for a, prod in batch_map.items():
                    us_products[str(a).strip().upper()] = prod
                done_extra += 1
                if progress:
                    progress(
                        "us_normalize",
                        f"Fetching full US data for swapped ASINs — batch {done_extra}/{n_extra_chunks}…",
                        done_extra,
                        n_extra_chunks,
                    )
            # Update the stashed prod on lines that just got a richer fetch.
            for line, meta, asin in on_us:
                refreshed = us_products.get(asin)
                if refreshed is not None:
                    line["__trasco_us_prod"] = refreshed

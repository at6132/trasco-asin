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
     under a different US ASIN.
  4. If still not, fall back to Keepa ``product_finder`` on .com using the row's
     MPN / title — many vendor sheets describe a product whose US ASIN is the first
     organic search hit on amazon.com even when the UPC isn't matched in Keepa.
  5. If nothing works → set ASIN cell to ``Not found (Foreign)`` and confidence
     to ``NOT FOUND (Foreign)``, with a detailed trace explaining each attempt.

The Keepa product object for each row's final US ASIN is stashed on the line under
``__trasco_us_prod`` so downstream steps (LLM ASIN validation, price history) can
reuse it without paying for another Keepa fetch.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from backend.cache import Cache
from backend.errors import JobCancelled
from backend.lookup import (
    KEEPA_FINDER_SORT,
    KeepaError,
    KeepaThrottle,
    fetch_keepa_product_by_code,
    fetch_keepa_products_batch,
    first_product,
    is_keepa_style_asin,
    product_finder_asins,
)
from backend.validator import title_similarity

logger = logging.getLogger(__name__)

ProgressCb = Optional[Callable[[str, str, int, int], None]]

# Confidence levels we attempt to US-normalize. Anything else (NOT FOUND etc.) is
# untouched since there's nothing to convert.
_CONF_NORMALIZE = frozenset({"HIGH", "MEDIUM", "LOW"})

NOT_FOUND_FOREIGN_ASIN = "Not found (Foreign)"
NOT_FOUND_FOREIGN_CONF = "NOT FOUND (Foreign)"

_US_DOMAIN = 1

# Title-search fallback acceptance threshold. Below this, we don't believe the
# product_finder hit is the same product the row is describing.
_TITLE_FALLBACK_MIN_SIMILARITY = 0.35


# ─── presence checks ──────────────────────────────────────────────────────────────────


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


def _diagnose_us_miss(prod: Optional[dict[str, Any]]) -> str:
    """Why ``prod`` is NOT considered US-present (short string for the trace column)."""
    if prod is None:
        return "no_product"
    if not isinstance(prod, dict):
        return "not_a_dict"
    title = prod.get("title")
    if isinstance(title, str) and title.strip():
        return "ok"
    csv = prod.get("csv")
    if isinstance(csv, list) and any(isinstance(lane, list) and lane for lane in csv):
        return "ok"
    bb = prod.get("buyBoxPrice")
    if isinstance(bb, int) and bb >= 0:
        return "ok"
    notable_present = [
        k
        for k in (
            "brand",
            "manufacturer",
            "imagesCSV",
            "categoryTree",
            "trackingSince",
            "stats",
        )
        if prod.get(k) not in (None, "", -1, [])
    ]
    if not notable_present:
        return "empty_stub"
    return "no_title_no_csv|has=" + ",".join(notable_present[:5])


# ─── trace helpers ────────────────────────────────────────────────────────────────────


def _append_trace(line: dict[str, Any], meta: dict[str, str], note: str) -> None:
    log_h = meta.get("log_h")
    if not log_h or not note:
        return
    prev = str(line.get(log_h) or "")
    line[log_h] = (prev + ("|" if prev else "") + note).strip()


_TRACE_MAX = 320


def _trace_snip(s: str, n: int = _TRACE_MAX) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _mark_not_found_foreign(
    line: dict[str, Any],
    meta: dict[str, str],
    *,
    notes: list[str],
) -> None:
    line[meta["asin_h"]] = NOT_FOUND_FOREIGN_ASIN
    line[meta["conf_h"]] = NOT_FOUND_FOREIGN_CONF
    line["__trasco_us_prod"] = None
    _append_trace(line, meta, "us=foreign_not_found")
    for n in notes:
        if n:
            _append_trace(line, meta, n)


# ─── UPC/EAN lookup on Amazon.com ─────────────────────────────────────────────────────


def _gtin_lookup_us(
    gtin: str,
    *,
    api_key: str,
    cache: Cache,
    cache_ttl_seconds: int,
    throttle: KeepaThrottle,
) -> tuple[Optional[dict[str, Any]], str]:
    """UPC/EAN → ``(prod, reason)`` where ``prod`` is the first US-present product or None."""
    if not gtin:
        return None, "code_skip=no_gtin"
    digits = re.sub(r"\D", "", str(gtin))
    if len(digits) < 8 or len(digits) > 14:
        return None, f"code_skip=bad_len:{len(digits)}"
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
        return None, f"code_err={_trace_snip(str(e), 120)}"
    except Exception as e:
        logger.warning("US GTIN lookup error gtin=%s: %s", gtin, e)
        return None, f"code_exc={_trace_snip(str(e), 120)}"
    prod = first_product(payload)
    if prod is None:
        # Keepa returned a payload but with no products array (or empty). Check for
        # explicit error hints to surface to the trace.
        keepa_err = (
            str(payload.get("error") or payload.get("message") or "").strip()
            if isinstance(payload, dict)
            else ""
        )
        if keepa_err:
            return None, f"code_lookup=no_result|keepa={_trace_snip(keepa_err, 120)}"
        return None, "code_lookup=no_result"
    if not is_present_on_us(prod):
        return None, f"code_lookup=stub|reason={_diagnose_us_miss(prod)}"
    asin_raw = prod.get("asin")
    asin_norm = str(asin_raw).strip().upper() if isinstance(asin_raw, str) else ""
    if not asin_norm or not is_keepa_style_asin(asin_norm):
        return None, "code_lookup=invalid_asin"
    return prod, f"code_lookup=hit|us_asin={asin_norm}"


# ─── product_finder title/MPN search on Amazon.com ────────────────────────────────────


def _build_finder_selections(row: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Order of attempts for US ``product_finder`` lookup.

    Order matters: stronger signal first. ``partNumber`` is highest precision when the
    MPN is well-formed (Philips ``CA6903/22`` style); ``title`` is a broad backstop.
    """
    out: list[tuple[str, dict[str, Any]]] = []
    brand = (str(row.get("_sheet_brand") or "")).strip()
    mpn = (str(row.get("_mpn") or row.get("_mpn_from_title") or "")).strip()
    sku = (str(row.get("_sku") or "")).strip()
    title = (str(row.get("_sheet_title_text") or "")).strip()

    def _part(p: str, br: Optional[str]) -> dict[str, Any]:
        sel: dict[str, Any] = {
            "partNumber": p.strip(".")[:120],
            "perPage": 30,
            "sort": KEEPA_FINDER_SORT,
        }
        if br:
            sel["brand"] = br[:80]
        return sel

    if mpn:
        if brand:
            out.append((f"finder_us_mpn_brand({mpn[:40]})", _part(mpn, brand)))
        out.append((f"finder_us_mpn({mpn[:40]})", _part(mpn, None)))
    if sku and sku != mpn:
        out.append((f"finder_us_sku({sku[:40]})", _part(sku, brand or None)))
    if title:
        out.append(
            (
                f"finder_us_title({_trace_snip(title, 60)})",
                {
                    "title": title[:160],
                    "title_flag": "0",
                    "perPage": 30,
                    "sort": KEEPA_FINDER_SORT,
                },
            )
        )
    return out


def _title_search_us(
    row: dict[str, Any],
    *,
    api_key: str,
    cache: Cache,
    cache_ttl_seconds: int,
    throttle: KeepaThrottle,
) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Try Keepa product_finder on Amazon.com with MPN / brand / title until we find a
    candidate whose title is similar enough to the row's description. Returns
    ``(product_with_full_history, trace_notes)``.
    """
    notes: list[str] = []
    attempts = _build_finder_selections(row)
    if not attempts:
        notes.append("title_search=skip|no_mpn_no_sku_no_title")
        return None, notes

    title_hint = (str(row.get("_sheet_title_text") or "")).strip()

    for label, selection in attempts:
        try:
            asins, _raw = product_finder_asins(
                api_key,
                _US_DOMAIN,
                selection,
                n_products=20,
                throttle=throttle,
            )
        except KeepaError as e:
            notes.append(f"{label}=err|{_trace_snip(str(e), 120)}")
            continue
        except Exception as e:
            notes.append(f"{label}=exc|{_trace_snip(str(e), 120)}")
            continue

        if not asins:
            notes.append(f"{label}=no_hits")
            continue

        # Pull product data with history+buybox so the same fetch services the price step.
        candidates = asins[:10]
        try:
            prods_map = fetch_keepa_products_batch(
                api_key,
                candidates,
                _US_DOMAIN,
                cache=cache,
                cache_ttl_seconds=cache_ttl_seconds,
                history=1,
                buybox=1,
                throttle=throttle,
            )
        except KeepaError as e:
            notes.append(f"{label}=batch_err|{_trace_snip(str(e), 120)}")
            continue
        except Exception as e:
            notes.append(f"{label}=batch_exc|{_trace_snip(str(e), 120)}")
            continue

        best_asin: Optional[str] = None
        best_score = -1.0
        best_title: str = ""
        for cand in candidates:
            p = prods_map.get(cand)
            if not p or not is_present_on_us(p):
                continue
            cand_title = str(p.get("title") or "")
            sc = title_similarity(title_hint, cand_title) if title_hint else 0.0
            if sc > best_score:
                best_score = sc
                best_asin = cand
                best_title = cand_title

        if not best_asin or best_score < _TITLE_FALLBACK_MIN_SIMILARITY:
            notes.append(
                f"{label}=weak|n_hits={len(candidates)}|best_sim={max(best_score, 0):.2f}"
                + (f"|top_asin={best_asin}" if best_asin else "")
                + (f"|top_title={_trace_snip(best_title, 60)}" if best_title else "")
            )
            continue

        notes.append(
            f"{label}=hit|us_asin={best_asin}|sim={best_score:.2f}"
            f"|us_title={_trace_snip(best_title, 80)}"
        )
        return prods_map.get(best_asin), notes

    return None, notes


# ─── main entry point ─────────────────────────────────────────────────────────────────


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

    # ─── Pass 1: batch-fetch every candidate ASIN on Amazon.com ────────────────────────
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

    # ─── Pass 2: classify rows; record asin-check miss reasons for the trace ──────────
    asin_miss_reason: dict[str, str] = {}
    on_us: list[tuple[dict[str, Any], dict[str, str], str]] = []
    fallback_rows: list[tuple[dict[str, Any], dict[str, str], str]] = []
    for line, meta, asin in candidates:
        prod = us_products.get(asin)
        if is_present_on_us(prod):
            line["__trasco_us_prod"] = prod
            _append_trace(line, meta, f"us=ok|us_asin={asin}")
            on_us.append((line, meta, asin))
            continue
        reason = asin_miss_reason.get(asin) or _diagnose_us_miss(prod)
        asin_miss_reason[asin] = reason
        _append_trace(line, meta, f"asin_check={asin}|{reason}")
        fallback_rows.append((line, meta, asin))

    # ─── Pass 3: GTIN/UPC lookup on .com for the rows that missed in Pass 1 ────────────
    if fallback_rows:
        # group rows by gtin; rows with no gtin go straight to title-search pass
        rows_by_gtin: dict[str, list[tuple[dict[str, Any], dict[str, str], str]]] = {}
        rows_no_gtin: list[tuple[dict[str, Any], dict[str, str], str]] = []
        for line, meta, asin in fallback_rows:
            gtin = str(line.get("__trasco_gtin") or "").strip()
            if gtin:
                rows_by_gtin.setdefault(gtin, []).append((line, meta, asin))
            else:
                _append_trace(line, meta, "gtin=missing")
                rows_no_gtin.append((line, meta, asin))

        gtin_results: dict[str, tuple[Optional[dict[str, Any]], str]] = {}
        if rows_by_gtin:
            unique_gtins = sorted(rows_by_gtin.keys())
            n_workers = max(1, min(int(keepa_parallel_max or 1), len(unique_gtins)))
            if progress:
                progress(
                    "us_normalize_gtin",
                    f"Cross-referencing {len(unique_gtins)} ASIN(s) via UPC/EAN on .com…",
                    0,
                    len(unique_gtins),
                )

            done_counter = [0]

            def _resolve_one_gtin(g: str) -> tuple[str, Optional[dict[str, Any]], str]:
                with tracker.keepa_slot():
                    prod, why = _gtin_lookup_us(
                        g,
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
                return g, prod, why

            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                for g, prod, why in pool.map(_resolve_one_gtin, unique_gtins):
                    gtin_results[g] = (prod, why)

        # Apply GTIN results; rows where the GTIN didn't yield a US product fall through
        # to the title-search pass below.
        gtin_swapped: list[tuple[dict[str, Any], dict[str, str], str]] = []
        title_search_rows: list[tuple[dict[str, Any], dict[str, str], str]] = []
        newly_needed_full_fetch: set[str] = set()

        for line, meta, asin in rows_no_gtin:
            title_search_rows.append((line, meta, asin))

        for gtin, rows in rows_by_gtin.items():
            prod, why = gtin_results.get(gtin, (None, "code_lookup=internal_no_result"))
            for line, meta, old_asin in rows:
                _append_trace(line, meta, f"gtin={gtin}|{why}")
            if not prod:
                for line, meta, old_asin in rows:
                    title_search_rows.append((line, meta, old_asin))
                continue
            new_asin_raw = prod.get("asin")
            new_asin = (
                str(new_asin_raw).strip().upper()
                if isinstance(new_asin_raw, str) and is_keepa_style_asin(new_asin_raw)
                else ""
            )
            if not new_asin:
                for line, meta, old_asin in rows:
                    title_search_rows.append((line, meta, old_asin))
                continue
            for line, meta, old_asin in rows:
                line[meta["asin_h"]] = new_asin
                line["__trasco_us_prod"] = prod
                _append_trace(
                    line,
                    meta,
                    f"us=foreign_replaced|orig_asin={old_asin}|us_asin={new_asin}|via=gtin",
                )
                gtin_swapped.append((line, meta, new_asin))
            if new_asin not in us_products:
                newly_needed_full_fetch.add(new_asin)

        # ─── Pass 3b: refetch swapped ASINs with history+buybox for downstream prices ─
        if newly_needed_full_fetch:
            extra = sorted(newly_needed_full_fetch)
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
            for line, meta, asin in gtin_swapped:
                refreshed = us_products.get(asin)
                if refreshed is not None:
                    line["__trasco_us_prod"] = refreshed
        on_us.extend(gtin_swapped)

        # ─── Pass 4: title / MPN finder on .com for rows still without a US ASIN ──────
        if title_search_rows:
            n_workers = max(1, min(int(keepa_parallel_max or 1), len(title_search_rows)))
            if progress:
                progress(
                    "us_normalize_title",
                    f"US title/MPN search for {len(title_search_rows)} remaining row(s)…",
                    0,
                    len(title_search_rows),
                )

            done_counter_t = [0]

            def _resolve_one_title(
                item: tuple[dict[str, Any], dict[str, str], str],
            ) -> tuple[dict[str, Any], dict[str, str], str, Optional[dict[str, Any]], list[str]]:
                line, meta, asin = item
                src_row = line.get("__trasco_src_row")
                if not isinstance(src_row, dict):
                    return line, meta, asin, None, ["title_search=skip|no_src_row"]
                with tracker.keepa_slot():
                    prod, notes = _title_search_us(
                        src_row,
                        api_key=api_key,
                        cache=cache,
                        cache_ttl_seconds=cache_ttl_seconds,
                        throttle=throttle,
                    )
                done_counter_t[0] += 1
                if progress:
                    progress(
                        "us_normalize_title",
                        f"US title/MPN search {done_counter_t[0]}/{len(title_search_rows)}…",
                        done_counter_t[0],
                        len(title_search_rows),
                    )
                return line, meta, asin, prod, notes

            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                title_results = list(pool.map(_resolve_one_title, title_search_rows))

            for line, meta, old_asin, prod, notes in title_results:
                for n in notes:
                    _append_trace(line, meta, n)
                if not prod or not is_present_on_us(prod):
                    _mark_not_found_foreign(line, meta, notes=[])
                    continue
                new_asin_raw = prod.get("asin")
                new_asin = (
                    str(new_asin_raw).strip().upper()
                    if isinstance(new_asin_raw, str) and is_keepa_style_asin(new_asin_raw)
                    else ""
                )
                if not new_asin:
                    _mark_not_found_foreign(line, meta, notes=[])
                    continue
                line[meta["asin_h"]] = new_asin
                line["__trasco_us_prod"] = prod
                _append_trace(
                    line,
                    meta,
                    f"us=foreign_replaced|orig_asin={old_asin}|us_asin={new_asin}|via=title_search",
                )
                us_products[new_asin] = prod
                on_us.append((line, meta, new_asin))

"""After ASIN + confidence are final: fetch Keepa price history for HIGH/MEDIUM rows."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from backend.cache import Cache
from backend.errors import JobCancelled
from backend.keepa_price_history import (
    average_price_cell,
    buy_box_landed_money,
    buy_box_price_incl_shipping_cell,
    monthly_sold_quantity_cell,
    parse_spreadsheet_unit_price,
)
from backend.lookup import KeepaThrottle, fetch_keepa_products_batch, is_keepa_style_asin

logger = logging.getLogger(__name__)

ProgressCb = Optional[Callable[[str, str, int, int], None]]

_CONF_FETCH = frozenset({"HIGH", "MEDIUM"})


def _apply_take_home_roi(
    line: dict[str, Any],
    meta: dict[str, str],
    prod: dict[str, Any],
    *,
    domain: int,
) -> None:
    """``Take home profit`` = landed Buy Box minus mapped vendor **cost**; ``ROI`` = profit / cost."""
    take_h = meta["take_home_h"]
    roi_h = meta["roi_h"]
    cost_col = (meta.get("cost_source_h") or "").strip()
    landed = buy_box_landed_money(prod, domain=domain)
    cost_val = parse_spreadsheet_unit_price(line.get(cost_col)) if cost_col else None
    if not landed or cost_val is None:
        line[take_h] = ""
        line[roi_h] = ""
        return
    cur, land_amt = landed
    profit = round(land_amt - cost_val, 2)
    line[take_h] = f"{cur} {profit:.2f}"
    if cost_val > 0:
        line[roi_h] = f"{round((profit / cost_val) * 100, 2):.2f}%"
    else:
        line[roi_h] = ""


def _domain_asin_lists(buckets: dict[tuple[int, str], Any]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for dom, asin in buckets:
        out.setdefault(dom, []).append(asin)
    for dom in out:
        out[dom] = sorted(set(out[dom]))
    return out


def enrich_sections_price_history(
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
    months: float = 6.0,
) -> None:
    """Mutates row dicts: fills **Average price**, **Buy Box**, **Take home profit**, **ROI**, and **Monthly sales quantity** for HIGH/MEDIUM rows."""
    _ = keepa_parallel_max

    def _check() -> None:
        if should_cancel and should_cancel():
            raise JobCancelled()

    buckets: dict[tuple[int, str], list[tuple[dict[str, Any], dict[str, str], str, int]]] = {}
    for sheet_title, _headers, out_rows, meta in sections:
        asin_h = meta["asin_h"]
        conf_h = meta["conf_h"]
        for ri, line in enumerate(out_rows, start=1):
            conf = str(line.get(conf_h) or "").strip().upper()
            if conf not in _CONF_FETCH:
                continue
            asin = str(line.get(asin_h) or "").strip().upper()
            if not asin or not is_keepa_style_asin(asin):
                continue
            dom = int(line.get("__trasco_domain") or 1)
            key = (dom, asin)
            buckets.setdefault(key, []).append((line, meta, sheet_title, ri))

    if not buckets:
        return

    by_dom = _domain_asin_lists(buckets)
    total_batches = sum((len(asins) + 99) // 100 for asins in by_dom.values())
    n_asin = len(buckets)
    if progress:
        progress(
            "keepa_price_hist",
            f"Keepa 6m average price, buy box & monthly sales — {n_asin} unique ASIN(s)…",
            0,
            max(total_batches, 1),
        )

    products_by_key: dict[tuple[int, str], dict[str, Any]] = {}
    done_batches = 0
    for dom, asin_list in sorted(by_dom.items()):
        for i in range(0, len(asin_list), 100):
            chunk = asin_list[i : i + 100]
            _check()
            with tracker.keepa_slot():
                batch_map = fetch_keepa_products_batch(
                    api_key,
                    chunk,
                    dom,
                    cache=cache,
                    cache_ttl_seconds=cache_ttl_seconds,
                    history=1,
                    buybox=1,
                    throttle=throttle,
                )
            for a, prod in batch_map.items():
                products_by_key[(dom, str(a).strip().upper())] = prod
            done_batches += 1
            if progress:
                progress(
                    "keepa_price_hist",
                    f"Keepa 6m average price, buy box & monthly sales — batch {done_batches}/{max(total_batches, 1)}…",
                    done_batches,
                    max(total_batches, 1),
                )

    for key, lines in buckets.items():
        dom, asin = key
        prod = products_by_key.get(key)
        if not prod:
            for line, meta, _st, _ri in lines:
                avg_h = meta["avg_price_h"]
                bb_h = meta["buy_box_incl_ship_h"]
                take_h = meta["take_home_h"]
                roi_h = meta["roi_h"]
                msq_h = meta["monthly_sales_qty_h"]
                line[avg_h] = ""
                line[bb_h] = ""
                line[take_h] = ""
                line[roi_h] = ""
                line[msq_h] = ""
            continue
        for line, meta, _sheet_title, _ri in lines:
            avg_h = meta["avg_price_h"]
            bb_h = meta["buy_box_incl_ship_h"]
            msq_h = meta["monthly_sales_qty_h"]
            try:
                line[avg_h] = average_price_cell(prod, domain=dom, months=months)
            except Exception as e:
                logger.warning("average price cell failed asin=%s: %s", asin, e)
                line[avg_h] = f"error:{e}"
            try:
                line[bb_h] = buy_box_price_incl_shipping_cell(prod, domain=dom)
            except Exception as e:
                logger.warning("buy box price cell failed asin=%s: %s", asin, e)
                line[bb_h] = f"error:{e}"
            try:
                line[msq_h] = monthly_sold_quantity_cell(prod)
            except Exception as e:
                logger.warning("monthly sales cell failed asin=%s: %s", asin, e)
                line[msq_h] = f"error:{e}"
            try:
                _apply_take_home_roi(line, meta, prod, domain=dom)
            except Exception as e:
                logger.warning("take home / roi failed asin=%s: %s", asin, e)
                line[meta["take_home_h"]] = f"error:{e}"
                line[meta["roi_h"]] = f"error:{e}"

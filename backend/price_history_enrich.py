"""Fill the US price columns for HIGH/MEDIUM rows that survived US normalization.

By the time this runs, ``backend.us_asin_normalize.normalize_sections_to_us_marketplace``
has already converted every ASIN to an Amazon.com ASIN (or marked it
``Not found (Foreign)``) and stashed the corresponding Keepa product on
``line["__trasco_us_prod"]``. So this step is now small:

  - For each HIGH/MEDIUM row with a stashed US prod:
      * Average price (6 mo)        — Keepa NEW/Amazon csv lanes on .com
      * Buy Box Price (incl. ship)  — Keepa ``csv[18]`` + buy-box fields on .com
      * Monthly sales quantity      — Keepa ``monthlySold`` on .com
      * Take home profit (USD)      — landed US Buy Box minus vendor cost
      * ROI (%)                     — profit / cost

Vendor cost is interpreted in the resolver marketplace's currency (Keepa domain id on
``line["__trasco_domain"]``) and converted to USD via Frankfurter (free, ECB-sourced).
The FX rate used is appended to the trace column for auditability.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from backend.cache import Cache
from backend.errors import JobCancelled
from backend.fx import usd_per_currency
from backend.keepa_price_history import (
    average_price_cell,
    buy_box_landed_money,
    buy_box_price_incl_shipping_cell,
    domain_currency,
    monthly_sold_quantity_cell,
    parse_spreadsheet_unit_price,
)
from backend.lookup import KeepaThrottle, is_keepa_style_asin

logger = logging.getLogger(__name__)

ProgressCb = Optional[Callable[[str, str, int, int], None]]

_CONF_FILL = frozenset({"HIGH", "MEDIUM"})

# Output domain for all price columns. ASIN normalization has already enforced this.
_OUTPUT_DOMAIN = 1


def _append_trace(line: dict[str, Any], meta: dict[str, str], note: str) -> None:
    log_h = meta.get("log_h")
    if not log_h or not note:
        return
    prev = str(line.get(log_h) or "")
    line[log_h] = (prev + ("|" if prev else "") + note).strip()


def _apply_take_home_roi(
    line: dict[str, Any],
    meta: dict[str, str],
    prod: dict[str, Any],
    *,
    cache: Optional[Cache],
) -> None:
    """``Take home profit`` = landed US Buy Box minus vendor cost (FX'd to USD)."""
    take_h = meta["take_home_h"]
    roi_h = meta["roi_h"]
    cost_col = (meta.get("cost_source_h") or "").strip()
    landed = buy_box_landed_money(prod, domain=_OUTPUT_DOMAIN)
    cost_val = parse_spreadsheet_unit_price(line.get(cost_col)) if cost_col else None
    if not landed or cost_val is None:
        line[take_h] = ""
        line[roi_h] = ""
        return

    _bb_cur, land_amt_usd = landed
    src_dom = int(line.get("__trasco_domain") or 1)
    src_ccy = domain_currency(src_dom)

    if src_ccy == "USD":
        cost_usd: float = float(cost_val)
    else:
        rate = usd_per_currency(cache, src_ccy)
        if rate is None or rate <= 0:
            line[take_h] = ""
            line[roi_h] = ""
            _append_trace(line, meta, f"fx_unavailable={src_ccy}")
            return
        cost_usd = round(float(cost_val) * float(rate), 4)
        _append_trace(line, meta, f"fx_{src_ccy.lower()}_usd={rate:.4f}")

    profit_usd = round(land_amt_usd - cost_usd, 2)
    line[take_h] = f"USD {profit_usd:.2f}"
    if cost_usd > 0:
        line[roi_h] = f"{round((profit_usd / cost_usd) * 100, 2):.2f}%"
    else:
        line[roi_h] = ""


def _fill_us_price_columns(
    line: dict[str, Any],
    meta: dict[str, str],
    prod: dict[str, Any],
    *,
    asin: str,
    cache: Optional[Cache],
    months: float,
) -> None:
    avg_h = meta["avg_price_h"]
    bb_h = meta["buy_box_incl_ship_h"]
    msq_h = meta["monthly_sales_qty_h"]
    try:
        line[avg_h] = average_price_cell(prod, domain=_OUTPUT_DOMAIN, months=months)
    except Exception as e:
        logger.warning("average price cell failed asin=%s: %s", asin, e)
        line[avg_h] = f"error:{e}"
    try:
        line[bb_h] = buy_box_price_incl_shipping_cell(prod, domain=_OUTPUT_DOMAIN)
    except Exception as e:
        logger.warning("buy box price cell failed asin=%s: %s", asin, e)
        line[bb_h] = f"error:{e}"
    try:
        line[msq_h] = monthly_sold_quantity_cell(prod)
    except Exception as e:
        logger.warning("monthly sales cell failed asin=%s: %s", asin, e)
        line[msq_h] = f"error:{e}"
    try:
        _apply_take_home_roi(line, meta, prod, cache=cache)
    except Exception as e:
        logger.warning("take home / roi failed asin=%s: %s", asin, e)
        line[meta["take_home_h"]] = f"error:{e}"
        line[meta["roi_h"]] = f"error:{e}"


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
    """Fill US price columns for HIGH/MEDIUM rows using the US Keepa product the
    normalization step already cached on ``line["__trasco_us_prod"]``.
    """
    # Args kept for backward compatibility with the existing caller signature in
    # ``main.py``. Network fetches happen during ``normalize_sections_to_us_marketplace``;
    # this pass is now in-process only.
    _ = api_key, cache_ttl_seconds, throttle, keepa_parallel_max, tracker

    def _check() -> None:
        if should_cancel and should_cancel():
            raise JobCancelled()

    rows_to_fill: list[tuple[dict[str, Any], dict[str, str], str, dict[str, Any]]] = []
    for _sheet_title, _headers, out_rows, meta in sections:
        asin_h = meta["asin_h"]
        conf_h = meta["conf_h"]
        for line in out_rows:
            conf = str(line.get(conf_h) or "").strip().upper()
            if conf not in _CONF_FILL:
                continue
            asin = str(line.get(asin_h) or "").strip().upper()
            if not asin or not is_keepa_style_asin(asin):
                continue
            prod = line.get("__trasco_us_prod")
            if not isinstance(prod, dict):
                continue
            rows_to_fill.append((line, meta, asin, prod))

    if not rows_to_fill:
        return

    total = len(rows_to_fill)
    if progress:
        progress("keepa_price_hist", f"Filling US price columns — {total} row(s)…", 0, total)

    for idx, (line, meta, asin, prod) in enumerate(rows_to_fill, start=1):
        _check()
        _fill_us_price_columns(line, meta, prod, asin=asin, cache=cache, months=months)
        if progress and (idx == total or idx % 25 == 0):
            progress(
                "keepa_price_hist",
                f"Filling US price columns — {idx}/{total}…",
                idx,
                total,
            )

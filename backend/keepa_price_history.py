"""
Decode Keepa product ``csv`` price tracks and compute ~6 month statistics for export.

Keepa timestamps are minutes since the Keepa epoch (2011-01-01 UTC).
"""

from __future__ import annotations

import math
import re
import time
from typing import Any, Optional

# Keepa product.csv first dimension indices (subset).
CSV_AMAZON = 0
CSV_NEW = 1
CSV_BUY_BOX_SHIPPING = 18

# Minutes from Keepa epoch to Unix epoch (2011-01-01 UTC).
_KEEPA_UNIX_OFFSET_MIN = 21564000


def keepa_minute_now() -> int:
    return int(time.time() // 60) - _KEEPA_UNIX_OFFSET_MIN


def parse_keepa_csv_track(raw: Any) -> list[tuple[int, int]]:
    """
    Decode one csv lane: ``[t0, p0, dt1, p1, dt2, p2, ...]`` (Keepa minutes, price in smallest unit).
    Returns list of (keepa_minute, raw_price_int).
    """
    if not isinstance(raw, list) or len(raw) < 2:
        return []
    try:
        t = int(raw[0])
        p0 = int(raw[1])
    except (TypeError, ValueError):
        return []
    out: list[tuple[int, int]] = [(t, p0)]
    i = 2
    while i + 1 < len(raw):
        try:
            dt = int(raw[i])
            pr = int(raw[i + 1])
        except (TypeError, ValueError):
            break
        if dt != -1:
            t += dt
        out.append((t, pr))
        i += 2
    return out


def _price_to_float(raw: int, currency: str) -> Optional[float]:
    if raw < 0:
        return None
    _ = currency
    return round(raw / 100.0, 2)


def domain_currency(domain: int) -> str:
    """Public alias for ``_domain_currency``: returns ISO-4217 code for a Keepa domain id."""
    return _domain_currency(domain)


def _domain_currency(domain: int) -> str:
    m = {
        1: "USD",
        2: "GBP",
        3: "EUR",
        4: "EUR",
        5: "JPY",
        6: "CAD",
        8: "EUR",
        9: "EUR",
        10: "INR",
        11: "MXN",
        12: "BRL",
    }
    return m.get(int(domain), "USD")


def pick_price_track_csv(product: dict[str, Any]) -> tuple[str, Any]:
    """Return (track_name, csv_lane) preferring marketplace NEW then Amazon."""
    csv = product.get("csv")
    if not isinstance(csv, list):
        return "NONE", None
    if len(csv) > CSV_NEW and csv[CSV_NEW]:
        return "NEW", csv[CSV_NEW]
    if len(csv) > CSV_AMAZON and csv[CSV_AMAZON]:
        return "AMAZON", csv[CSV_AMAZON]
    return "NONE", None


def series_last_months(
    pairs: list[tuple[int, int]],
    *,
    months: float = 6.0,
) -> list[tuple[int, int]]:
    if not pairs:
        return []
    cutoff = keepa_minute_now() - int(months * 30.4375 * 24 * 60)
    return [(t, p) for t, p in pairs if t >= cutoff]


def average_price_cell(
    product: dict[str, Any],
    *,
    domain: int,
    months: float = 6.0,
) -> str:
    """
    One spreadsheet cell: mean price over ``months`` from NEW then Amazon history.
    Example: ``USD 24.99 — 6 mo avg (NEW)``. Empty string if no usable points.
    """
    currency = str(product.get("currency") or "").strip() or _domain_currency(domain)
    track, lane = pick_price_track_csv(product)
    raw_pairs = parse_keepa_csv_track(lane)
    window = series_last_months(raw_pairs, months=months)
    vals: list[float] = []
    for _km, raw_p in window:
        pf = _price_to_float(raw_p, currency)
        if pf is not None:
            vals.append(pf)
    if not vals:
        return ""
    avg = round(sum(vals) / len(vals), 2)
    mo = int(months) if float(months).is_integer() else months
    suffix = f" ({track})" if track != "NONE" else ""
    return f"{currency} {avg:.2f} — {mo} mo avg{suffix}"


def parse_buy_box_shipping_csv(raw: Any) -> list[tuple[int, int, int]]:
    """
    ``BUY_BOX_SHIPPING`` lane: ``[time, price, shipping, dt, price, shipping, …]``.
    Price and shipping are Keepa smallest-currency integers (-1 = n/a).
    """
    if not isinstance(raw, list) or len(raw) < 3:
        return []
    try:
        t = int(raw[0])
        p0 = int(raw[1])
        s0 = int(raw[2])
    except (TypeError, ValueError):
        return []
    out: list[tuple[int, int, int]] = [(t, p0, s0)]
    i = 3
    while i + 2 < len(raw):
        try:
            dt = int(raw[i])
            pr = int(raw[i + 1])
            sh = int(raw[i + 2])
        except (TypeError, ValueError):
            break
        if dt != -1:
            t += dt
        out.append((t, pr, sh))
        i += 3
    return out


def _buy_box_lane(product: dict[str, Any]) -> Any:
    csv = product.get("csv")
    if not isinstance(csv, list) or len(csv) <= CSV_BUY_BOX_SHIPPING:
        return None
    lane = csv[CSV_BUY_BOX_SHIPPING]
    return lane if lane else None


def _buy_box_total_raw_smallest_unit(product: dict[str, Any]) -> Optional[int]:
    """Landed Buy Box (item + shipping when known) in Keepa smallest-currency units, or ``None``."""
    total_raw: Optional[int] = None
    lane = _buy_box_lane(product)
    if lane is not None:
        triples = parse_buy_box_shipping_csv(lane)
        if triples:
            _t, p, s = triples[-1]
            if p >= 0:
                if s >= 0:
                    total_raw = p + s
                else:
                    total_raw = p

    if total_raw is None or total_raw < 0:
        bp = product.get("buyBoxPrice")
        bs = product.get("buyBoxShipping")
        p_raw: Optional[int] = None
        if bp is not None and not isinstance(bp, bool):
            try:
                p_raw = int(bp) if isinstance(bp, int) else int(round(float(bp)))
            except (TypeError, ValueError, OverflowError):
                p_raw = None
        if p_raw is not None and p_raw >= 0:
            ship_raw = 0
            if bs is not None and not isinstance(bs, bool):
                try:
                    s_int = int(bs) if isinstance(bs, int) else int(round(float(bs)))
                except (TypeError, ValueError, OverflowError):
                    s_int = -1
                if s_int >= 0:
                    ship_raw = s_int
            total_raw = p_raw + ship_raw

    if total_raw is None or total_raw < 0:
        return None
    return total_raw


def buy_box_landed_money(product: dict[str, Any], *, domain: int) -> Optional[tuple[str, float]]:
    """
    Current Buy Box landed amount as ``(currency, major_units)``, e.g. ``(\"USD\", 29.99)``.
    Same basis as :func:`buy_box_price_incl_shipping_cell` but without the FBA/FBM suffix.
    """
    currency = str(product.get("currency") or "").strip() or _domain_currency(domain)
    raw = _buy_box_total_raw_smallest_unit(product)
    if raw is None:
        return None
    amt = _price_to_float(raw, currency)
    if amt is None:
        return None
    return currency, amt


def parse_spreadsheet_unit_price(value: Any) -> Optional[float]:
    """
    Parse a vendor spreadsheet cost/price cell (number, or string with currency symbols / grouping).
    Returns ``None`` if empty or not parseable.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            x = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return x if math.isfinite(x) else None

    s = str(value).strip()
    if not s:
        return None
    # Keep digits, separators, minus; strip currency letters/symbols and spaces.
    s2 = re.sub(r"[^\d,.\-]", "", s.replace(" ", ""))
    if not s2 or s2 in "-.":
        return None
    if s2.count(",") == 1 and "." not in s2:
        s2 = s2.replace(",", ".")
    elif "," in s2 and "." in s2:
        s2 = s2.replace(",", "")
    else:
        s2 = s2.replace(",", "")
    try:
        x = float(s2)
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def buy_box_price_incl_shipping_cell(product: dict[str, Any], *, domain: int) -> str:
    """
    Current Buy Box landed price (item + shipping for FBM; shipping often 0 for FBA).

    Prefer ``csv[18]`` (``BUY_BOX_SHIPPING``) last triplet: ``price + max(0, shipping)``
    when shipping is non-negative; otherwise fall back to ``buyBoxPrice`` +
    ``buyBoxShipping`` when ``buybox=1`` was used on the product request.
    """
    landed = buy_box_landed_money(product, domain=domain)
    if not landed:
        return ""
    currency, amt = landed

    is_fba = product.get("buyBoxIsFBA")
    if is_fba is True:
        tag = "FBA"
    elif is_fba is False:
        tag = "FBM"
    else:
        tag = None
    suffix = f" ({tag})" if tag else ""
    return f"{currency} {amt:.2f}{suffix}"


def monthly_sold_quantity_cell(product: dict[str, Any]) -> str:
    """
    Keepa product ``monthlySold``: units bought in the past month when present.
    Empty when missing or non-positive (many ASINs have no value).
    """
    raw = product.get("monthlySold")
    if raw is None:
        return ""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return str(n)

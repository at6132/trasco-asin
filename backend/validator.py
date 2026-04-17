"""
Matching rules between spreadsheet rows and Keepa catalog fields.
Pure string similarity + ASIN / GTIN normalization — no I/O.
"""

from __future__ import annotations

import math
import re
import unicodedata
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Optional


_ASIN_RE = re.compile(r"\b(B0[A-Z0-9]{8})\b", re.IGNORECASE)
_ASIN_BROAD_RE = re.compile(r"\b([A-Z0-9]{10})\b", re.IGNORECASE)


def normalize_asin(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    s = str(value).strip().upper()
    if len(s) == 10 and re.fullmatch(r"[A-Z0-9]{10}", s):
        return s
    m = _ASIN_RE.search(str(value))
    if m:
        return m.group(1).upper()
    m = _ASIN_BROAD_RE.search(str(value))
    if m:
        candidate = m.group(1).upper()
        if not re.fullmatch(r"\d{10}", candidate):
            return candidate
    return None


def digits_only(value: Optional[str]) -> str:
    if value is None:
        return ""
    return re.sub(r"\D", "", str(value))


def normalize_gtin(value: Optional[str]) -> Optional[str]:
    """
    Extract a GTIN / EAN / UPC digit string suitable for Keepa's `code` parameter.
    Accepts common lengths; does not validate check digits.

    Excel often stores barcodes as numeric cells → Python ``float``; ``str(8720689036696.0)``
    would otherwise become the wrong digit string via ``digits_only`` (extra trailing ``0``).
    """
    if value is None or isinstance(value, bool):
        return None

    d: str
    if isinstance(value, int):
        d = str(abs(value))
    elif isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        if value.is_integer() and 1e5 <= abs(value) < 1e15:
            d = str(int(abs(value)))
        else:
            d = digits_only(str(value))
    elif isinstance(value, Decimal):
        try:
            iv = int(value)
        except Exception:
            iv = 0
        if value == iv and 1e5 <= abs(iv) < 1e15:
            d = str(abs(iv))
        else:
            d = digits_only(str(value))
    else:
        d = digits_only(value)

    if not d:
        return None
    if 8 <= len(d) <= 14:
        return d
    return None


def normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""
    s = unicodedata.normalize("NFKD", str(value))
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_MODEL_NUMBER_RE = re.compile(r"\b([A-Z]{1,4}[\-]?\d{2,}[A-Z0-9\-]*)\b", re.IGNORECASE)


def _extract_model_numbers(text: str) -> set[str]:
    return {m.group(1).upper().replace("-", "") for m in _MODEL_NUMBER_RE.finditer(text)}


def _token_overlap(a: str, b: str) -> float:
    ta = set(a.split())
    tb = set(b.split())
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    return len(inter) / min(len(ta), len(tb))


def title_similarity(a: Optional[str], b: Optional[str]) -> float:
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    seq_score = float(SequenceMatcher(None, na, nb).ratio())
    token_score = _token_overlap(na, nb)

    models_a = _extract_model_numbers(a or "")
    models_b = _extract_model_numbers(b or "")
    model_bonus = 0.0
    if models_a and models_b and (models_a & models_b):
        model_bonus = 0.15

    return min(1.0, max(seq_score, token_score * 0.85) + model_bonus)


def validate_title_match(
    sheet_title: Optional[str],
    keepa_title: Optional[str],
    *,
    min_similarity: float = 0.55,
) -> tuple[bool, float, str]:
    score = title_similarity(sheet_title, keepa_title)
    if score >= min_similarity:
        return True, score, "title_similarity_ok"
    return False, score, "title_similarity_low"


_PACK_PATTERNS = [
    (re.compile(r"\b(\d+)\s*[-]?\s*pack\b", re.I), lambda m: int(m.group(1))),
    (re.compile(r"\b(\d+)\s*er[-\s]*(pack|set|box)\b", re.I), lambda m: int(m.group(1))),
    (re.compile(r"\bpack\s+of\s+(\d+)\b", re.I), lambda m: int(m.group(1))),
    (re.compile(r"\b(\d+)\s*(?:ct|count)\b", re.I), lambda m: int(m.group(1))),
    (re.compile(r"\b(twin|double)\s+pack\b", re.I), lambda m: 2),
    (re.compile(r"\b(triple)\s+pack\b", re.I), lambda m: 3),
    (re.compile(r"\b(\d+)\s*[-]?\s*stück\b", re.I), lambda m: int(m.group(1))),
    (re.compile(r"\b(\d+)\s*pk\b", re.I), lambda m: int(m.group(1))),
    (re.compile(r"\b(\d+)\s*[-]?\s*piece\b", re.I), lambda m: int(m.group(1))),
    (re.compile(r"\bset\s+of\s+(\d+)\b", re.I), lambda m: int(m.group(1))),
]


def infer_pack_count(text: Optional[str]) -> Optional[int]:
    """Best-effort pack / multipack count from free text (supplier or Amazon title)."""
    if not text:
        return None
    s = str(text)
    for rx, fn in _PACK_PATTERNS:
        m = rx.search(s)
        if m:
            try:
                n = fn(m)
                return n if 1 < n < 100 else None
            except (ValueError, IndexError):
                continue
    return None


def pack_consistency(
    sheet_text: Optional[str],
    amazon_text: Optional[str],
) -> tuple[bool, Optional[int], Optional[int], str]:
    """
    Returns (ok, sheet_pack, amazon_pack, reason).
    ok=False when both sides imply a pack count and they disagree.
    """
    sp = infer_pack_count(sheet_text)
    ap = infer_pack_count(amazon_text)
    if sp is None or ap is None:
        return True, sp, ap, "pack_unknown_or_single"
    if sp == ap:
        return True, sp, ap, "pack_match"
    return False, sp, ap, "pack_mismatch"


def aggregate_confidence(
    *,
    status: str,
    title_match: bool,
    brand_match: bool,
    title_score: float,
    pack_ok: bool,
) -> str:
    if status in ("not_found", "no_identifier", "keepa_error", "missing"):
        return "NOT FOUND"
    if status == "pack_mismatch" or not pack_ok:
        return "LOW"
    if title_match and brand_match:
        return "HIGH"
    if title_match:
        return "MEDIUM"
    if title_score >= 0.55:
        return "MEDIUM"
    return "LOW"


def validate_brand_match(
    sheet_brand: Optional[str],
    keepa_brand: Optional[str],
    *,
    min_similarity: float = 0.65,
) -> tuple[bool, float, str]:
    sb = normalize_text(sheet_brand)
    kb = normalize_text(keepa_brand)
    if not sb or not kb:
        return False, 0.0, "brand_unknown"
    if sb == kb:
        return True, 1.0, "brand_exact"
    score = float(SequenceMatcher(None, sb, kb).ratio())
    if score >= min_similarity or sb in kb or kb in sb:
        return True, max(score, 0.7), "brand_fuzzy_ok"
    return False, score, "brand_mismatch"

"""
Tier-2/3 Keepa resolution: product_finder + batch product fetch, SKU→ASIN cache.
Uses Claude Haiku when ``ANTHROPIC_API_KEY`` is set, else local Ollama, to pick among finder hits
and to suggest escalation queries.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from backend.cache import Cache, get_cached_sku_resolve, set_cached_sku_resolve
from backend.lookup import (
    KEEPA_FINDER_SORT,
    KeepaError,
    fetch_keepa_products_batch,
    product_finder_asins,
)
from backend.ollama_asin_validate import (
    haiku_pick_asin_from_candidates,
    haiku_suggest_finder_escalations,
    ollama_pick_asin_from_candidates,
    ollama_suggest_finder_escalations,
    ollama_tags_reachable,
)
from backend.anthropic_usage import AnthropicUsageLedger
from backend.ollama_usage import OllamaTokenLedger
from backend.validator import title_similarity


def _norm_brand_key(brand: Optional[str]) -> str:
    if not brand:
        return ""
    return re.sub(r"\s+", " ", str(brand).strip().lower())[:80]


def _norm_sku_key(sku: Optional[str]) -> str:
    if not sku:
        return ""
    return str(sku).strip().lower()[:120]


def sku_resolve_storage_key(domain: int, row: dict[str, Any]) -> Optional[str]:
    sku = row.get("_sku") or row.get("_mpn")
    if not sku:
        return None
    return f"{int(domain)}|{_norm_brand_key(row.get('_sheet_brand'))}|{_norm_sku_key(sku)}"


def _pick_best_asin_heuristic(
    candidates: list[str],
    products: dict[str, dict[str, Any]],
    sheet_title: Optional[str],
) -> tuple[Optional[str], float, str]:
    """Highest title-similarity ASIN among loaded products (no hard reject — LLM handles ambiguity)."""
    best_a: Optional[str] = None
    best_s = -1.0
    for a in candidates:
        p = products.get(a)
        if not p:
            continue
        t = p.get("title")
        sc = title_similarity(sheet_title, t if isinstance(t, str) else None)
        if sc > best_s:
            best_s, best_a = sc, a
    if best_a is None:
        return None, 0.0, "no_candidate_products"
    return best_a, best_s, "best_title_similarity"


def _title_selection(tq: str) -> dict[str, Any]:
    return {
        "title": tq.strip()[:160],
        "title_flag": "0",
        "perPage": 50,
        "sort": KEEPA_FINDER_SORT,
    }


def _part_selection(part: str, brand: Optional[str]) -> dict[str, Any]:
    sel: dict[str, Any] = {
        "partNumber": part.strip().lower()[:120],
        "perPage": 50,
        "sort": KEEPA_FINDER_SORT,
    }
    if brand and str(brand).strip():
        sel["brand"] = str(brand).strip()[:80]
    return sel


def _dedupe_attempts(pairs: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    seen: set[str] = set()
    out: list[tuple[str, dict[str, Any]]] = []
    for lab, sel in pairs:
        key = json.dumps(sel, sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        out.append((lab, sel))
    return out


def _build_standard_attempts(
    *,
    part: str,
    brand: Optional[str],
    mpn_norm: str,
    sku_k: str,
    title_hint: Optional[str],
) -> list[tuple[str, dict[str, Any]]]:
    attempts: list[tuple[str, dict[str, Any]]] = []
    if part:
        if brand and str(brand).strip():
            attempts.append(("finder_partNumber_brand", _part_selection(part, brand)))
        attempts.append(("finder_partNumber", _part_selection(part, None)))
    if mpn_norm and mpn_norm != part:
        if brand and str(brand).strip():
            attempts.append(("finder_mpn_brand", _part_selection(mpn_norm, brand)))
        attempts.append(("finder_mpn", _part_selection(mpn_norm, None)))

    th = (title_hint or "").strip()
    if len(th) >= 3:
        attempts.append(("finder_title_full", _title_selection(th)))
    if len(th) > 90:
        attempts.append(("finder_title_short", _title_selection(th[:90])))
    words = th.split()
    if len(words) >= 4:
        kw = " ".join(words[:12])
        if len(kw) >= 8:
            attempts.append(("finder_title_keywords", _title_selection(kw)))

    return _dedupe_attempts(attempts)


def _run_one_finder_attempt(
    api_key: str,
    domain: int,
    cache: Cache,
    throttle: Any,
    cache_ttl_seconds: int,
    label: str,
    selection: dict[str, Any],
    *,
    title_hint: Optional[str],
    sku_display: str,
    source_file_hint: Optional[str],
    ollama_usage: Optional[OllamaTokenLedger],
    ollama_base_url: Optional[str],
    ollama_model: str,
    ollama_timeout_sec: float,
    use_llm_pick: bool,
    anthropic_api_key: Optional[str] = None,
    haiku_model: str = "",
    anthropic_usage: Optional[AnthropicUsageLedger] = None,
) -> Optional[tuple[dict[str, Any], str, float, str]]:
    """
    Returns (product, reason_suffix, finder_score, finder_how) or None.
    """
    asins, _raw = product_finder_asins(
        api_key,
        domain,
        selection,
        n_products=25,
        throttle=throttle,
    )
    if not asins:
        return None
    take = asins[:30]
    prods = fetch_keepa_products_batch(
        api_key,
        take,
        domain,
        cache=cache,
        cache_ttl_seconds=cache_ttl_seconds,
        throttle=throttle,
    )
    rows: list[tuple[str, str]] = []
    for a in take:
        p = prods.get(a)
        if not p:
            continue
        tt = p.get("title")
        rows.append((str(a).strip().upper(), str(tt) if isinstance(tt, str) else ""))
    if not rows:
        return None

    picked: Optional[str] = None
    if use_llm_pick and (anthropic_api_key or "").strip():
        picked = haiku_pick_asin_from_candidates(
            (anthropic_api_key or "").strip(),
            haiku_model or "claude-haiku-4-5-20251001",
            distributor_sku=sku_display,
            sheet_description=(title_hint or "")[:800],
            source_file_hint=source_file_hint,
            candidates=rows,
            timeout=min(ollama_timeout_sec, 120.0),
            anthropic_usage=anthropic_usage,
        )
        if picked and prods.get(picked):
            return prods[picked], f"{label}:haiku_pick", 1.0, "haiku_pick"
    elif use_llm_pick and ollama_base_url and ollama_tags_reachable(ollama_base_url):
        picked = ollama_pick_asin_from_candidates(
            ollama_base_url,
            ollama_model,
            distributor_sku=sku_display,
            sheet_description=(title_hint or "")[:800],
            source_file_hint=source_file_hint,
            candidates=rows,
            ollama_usage=ollama_usage,
            timeout=min(ollama_timeout_sec, 120.0),
        )
        if picked and prods.get(picked):
            return prods[picked], f"{label}:ollama_pick", 1.0, "ollama_pick"

    best, score, how = _pick_best_asin_heuristic(take, prods, title_hint)
    if best and prods.get(best):
        return prods[best], f"{label}:{how}", score, how
    return None


def resolve_via_product_finder(
    api_key: str,
    domain: int,
    cache: Cache,
    *,
    brand: Optional[str],
    sku: Optional[str],
    mpn: Optional[str],
    title_hint: Optional[str],
    cache_ttl_seconds: int,
    throttle: Any,
    anthropic_api_key: str = "",
    haiku_model: str = "",
    ollama_base_url: str = "",
    ollama_model: str = "gemma3:27b",
    ollama_timeout_sec: float = 120.0,
    use_ollama_resolver_gemma: bool = True,
    source_file_hint: Optional[str] = None,
    ollama_usage: Optional[OllamaTokenLedger] = None,
    anthropic_usage: Optional[AnthropicUsageLedger] = None,
) -> tuple[Optional[dict[str, Any]], str]:
    """
    Returns (product_dict or None, reason_code).
    Uses SKU resolve cache keyed by domain|brand|sku.
    """
    sku_k = _norm_sku_key(sku)
    mpn_n = _norm_sku_key(mpn)
    br_k = _norm_brand_key(brand)
    if not sku_k and not mpn_n:
        return None, "no_sku_or_mpn"

    cache_sku = sku_k or mpn_n
    cached = get_cached_sku_resolve(cache, domain, br_k, cache_sku)
    if isinstance(cached, dict):
        if cached.get("product") is not None:
            return cached["product"], str(cached.get("via", "cache"))
        if cached.get("via") == "not_found":
            return None, f"cache:{cached.get('reason', 'not_found')}"

    part = mpn_n or sku_k
    use_llm_pick = bool(
        use_ollama_resolver_gemma and (ollama_base_url or "").strip()
    ) or bool((anthropic_api_key or "").strip())
    obase = (ollama_base_url or "").strip() or None
    sku_display = (sku or mpn or "").strip()[:80]

    attempts = _build_standard_attempts(
        part=part,
        brand=brand,
        mpn_norm=mpn_n,
        sku_k=sku_k,
        title_hint=title_hint,
    )

    last_err = ""
    tried_labels: list[str] = []

    def _cache_hit(res: tuple[dict[str, Any], str, float, str]) -> tuple[dict[str, Any], str]:
        product, lab_suffix, _fs, _fh = res
        payload = {
            "product": product,
            "via": lab_suffix.split(":")[0],
            "finder_how": lab_suffix,
            "asin": product.get("asin"),
        }
        set_cached_sku_resolve(cache, domain, br_k, cache_sku, payload, ttl_seconds=cache_ttl_seconds)
        return product, lab_suffix

    for label, selection in attempts:
        tried_labels.append(label)
        try:
            hit = _run_one_finder_attempt(
                api_key,
                domain,
                cache,
                throttle,
                cache_ttl_seconds,
                label,
                selection,
                title_hint=title_hint,
                sku_display=sku_display,
                source_file_hint=source_file_hint,
                ollama_usage=ollama_usage,
                ollama_base_url=obase,
                ollama_model=ollama_model,
                ollama_timeout_sec=ollama_timeout_sec,
                use_llm_pick=use_llm_pick,
                anthropic_api_key=anthropic_api_key,
                haiku_model=haiku_model,
                anthropic_usage=anthropic_usage,
            )
            if hit:
                return _cache_hit(hit)
        except KeepaError as e:
            last_err = str(e)
        except Exception as e:
            last_err = f"{label}:{e}"

    if use_llm_pick and (anthropic_api_key or "").strip():
        trace = ",".join(tried_labels) + "|" + (last_err or "")
        extra_titles, extra_parts = haiku_suggest_finder_escalations(
            (anthropic_api_key or "").strip(),
            haiku_model or "claude-haiku-4-5-20251001",
            distributor_sku=sku_display,
            sheet_description=(title_hint or "")[:900],
            brand=brand,
            source_file_hint=source_file_hint,
            attempts_tried=trace[:500],
            timeout=min(ollama_timeout_sec, 120.0),
            anthropic_usage=anthropic_usage,
        )
    elif use_llm_pick and obase and ollama_tags_reachable(obase):
        trace = ",".join(tried_labels) + "|" + (last_err or "")
        extra_titles, extra_parts = ollama_suggest_finder_escalations(
            obase,
            ollama_model,
            distributor_sku=sku_display,
            sheet_description=(title_hint or "")[:900],
            brand=brand,
            source_file_hint=source_file_hint,
            attempts_tried=trace[:500],
            ollama_usage=ollama_usage,
            timeout=min(ollama_timeout_sec, 120.0),
        )
    else:
        extra_titles, extra_parts = [], []

    if extra_titles or extra_parts:
        extra_attempts: list[tuple[str, dict[str, Any]]] = []
        for i, tq in enumerate(extra_titles):
            if len(tq.strip()) >= 3:
                extra_attempts.append((f"finder_esc_title_{i}", _title_selection(tq)))
        for i, pn in enumerate(extra_parts):
            pn2 = _norm_sku_key(pn)
            if pn2:
                if brand and str(brand).strip():
                    extra_attempts.append(
                        (f"finder_esc_part_brand_{i}", _part_selection(pn2, brand)),
                    )
                extra_attempts.append((f"finder_esc_part_{i}", _part_selection(pn2, None)))

        for label, selection in _dedupe_attempts(extra_attempts):
            tried_labels.append(label)
            try:
                hit = _run_one_finder_attempt(
                    api_key,
                    domain,
                    cache,
                    throttle,
                    cache_ttl_seconds,
                    label,
                    selection,
                    title_hint=title_hint,
                    sku_display=sku_display,
                    source_file_hint=source_file_hint,
                    ollama_usage=ollama_usage,
                    ollama_base_url=obase,
                    ollama_model=ollama_model,
                    ollama_timeout_sec=ollama_timeout_sec,
                    use_llm_pick=use_llm_pick,
                    anthropic_api_key=anthropic_api_key,
                    haiku_model=haiku_model,
                    anthropic_usage=anthropic_usage,
                )
                if hit:
                    return _cache_hit(hit)
            except KeepaError as e:
                last_err = str(e)
            except Exception as e:
                last_err = f"{label}:{e}"

    neg_ttl = min(cache_ttl_seconds, 43200)
    neg_payload: dict[str, Any] = {"product": None, "via": "not_found", "reason": last_err or "finder_failed"}
    set_cached_sku_resolve(cache, domain, br_k, cache_sku, neg_payload, ttl_seconds=neg_ttl)
    return None, last_err or "finder_failed"

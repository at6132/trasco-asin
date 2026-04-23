"""
LLM helpers for Keepa workflows: Claude Haiku (Anthropic) when configured, else local Ollama.
Same prompts/JSON contracts for equivalent steps on either backend.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Literal, Optional

import httpx

from backend.anthropic_usage import AnthropicUsageLedger, record_anthropic_messages_response
from backend.http_pool import get_anthropic_client
from backend.ollama_usage import OllamaTokenLedger, record_chat_response

logger = logging.getLogger(__name__)

# Keepa marketplace ids (see https://keepa.com/#!api)
ALLOWED_KEEPA_DOMAINS: frozenset[int] = frozenset({1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12})

Verdict = Literal["accept", "reject", "error"]

# Anthropic org limits (e.g. 50 RPM) — transient; we retry after a computed wait.
_DEFAULT_ANTHROPIC_RATE_LIMIT_RETRIES = 12


def _parse_retry_after_header(resp: httpx.Response) -> Optional[float]:
    """Seconds to wait from ``Retry-After`` (seconds or HTTP-date), capped for safety."""
    raw = (resp.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        sec = float(raw)
        if sec >= 0:
            return min(120.0, sec)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delta = (dt - datetime.now(timezone.utc)).total_seconds()
            if delta > 0:
                return min(120.0, delta)
    except Exception:
        pass
    return None


def _backoff_after_rate_limit(attempt: int) -> float:
    """Exponential backoff with jitter when the API does not send Retry-After."""
    base = min(90.0, 2.0 * (1.55 ** max(0, attempt - 1)))
    return base + random.uniform(0.15, 1.1)


def ollama_tags_reachable(base_url: str, timeout: float = 3.0) -> bool:
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(base_url.rstrip("/") + "/api/tags")
            return r.status_code == 200
    except Exception:
        return False


def _anthropic_first_message_text(
    api_key: str,
    model: str,
    user_prompt: str,
    *,
    max_tokens: int,
    timeout: float,
    max_rate_limit_retries: int = _DEFAULT_ANTHROPIC_RATE_LIMIT_RETRIES,
    anthropic_usage: Optional[AnthropicUsageLedger] = None,
) -> str:
    """POST /v1/messages and concatenate assistant text blocks.

    On HTTP 429 (rate limit) or 529 (overloaded), waits and retries using ``Retry-After`` when
    present, otherwise exponential backoff, up to ``max_rate_limit_retries`` attempts.
    """
    url = "https://api.anthropic.com/v1/messages"
    body = {
        "model": model or "claude-haiku-4-5-20251001",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    headers = {
        "x-api-key": api_key.strip(),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    attempt = 0
    data: dict[str, Any] = {}
    while True:
        attempt += 1
        r = get_anthropic_client().post(url, json=body, headers=headers)
        if r.status_code < 400:
            data = r.json()
            record_anthropic_messages_response(anthropic_usage, data)
            break
        snippet = (r.text or "").strip().replace("\n", " ")[:400]
        transient = r.status_code == 429 or r.status_code == 529
        if transient and attempt <= max_rate_limit_retries:
            wait = _parse_retry_after_header(r)
            if wait is None:
                wait = _backoff_after_rate_limit(attempt)
            else:
                wait = max(0.5, wait)
            logger.warning(
                "Anthropic HTTP %s — waiting %.1fs before retry %s/%s: %s",
                r.status_code,
                wait,
                attempt,
                max_rate_limit_retries,
                snippet[:200],
            )
            time.sleep(wait)
            continue
        raise RuntimeError(f"anthropic_http_{r.status_code}:{snippet or r.reason_phrase}")
    text = ""
    for block in data.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text += block.get("text") or ""
    return text


def _parse_json_from_content(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if not text:
        return {}
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else {}
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            out = json.loads(text[start : end + 1])
            return out if isinstance(out, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _source_file_prompt_fragment(source_file_hint: Optional[str]) -> str:
    s = (source_file_hint or "").strip()[:240]
    if not s:
        return ""
    return (
        "\nSpreadsheet upload filename (no extension) — often reflects the vendor or catalog; "
        "use as a weak hint for brand or product family when the row text is short or ambiguous:\n"
        f"upload_filename_hint: {s}\n"
    )


def _asin_match_validation_prompt(
    *,
    sheet_description: str,
    distributor_sku: Optional[str],
    asin: str,
    amazon_title: Optional[str],
    amazon_brand: Optional[str],
    source_file_hint: Optional[str] = None,
) -> Optional[str]:
    """Shared LLM prompt; returns None if distributor description is empty."""
    desc = (sheet_description or "").strip()[:800]
    if not desc:
        return None
    at = (amazon_title or "").strip()[:500] or "(no title from Keepa)"
    ab = (amazon_brand or "").strip()[:120] or "(unknown brand)"
    sku = (distributor_sku or "").strip()[:80] or ""
    return (
        "You validate distributor spreadsheet rows against Amazon catalog data.\n"
        "Decide if the Amazon listing is the SAME physical product / same SKU as the line item.\n"
        "Allow minor wording differences, pack size wording, and regional naming.\n"
        "Reject only if they are clearly different products or incompatible models.\n"
        f"{_source_file_prompt_fragment(source_file_hint)}"
        f"\ndistributor_description: {desc}\n"
        f"distributor_sku: {sku or '(none)'}\n"
        f"amazon_asin: {asin}\n"
        f"amazon_listing_title: {at}\n"
        f"amazon_listing_brand: {ab}\n\n"
        'Reply with JSON only, no markdown: {"same_product": true}\n'
        'or {"same_product": false}\n'
    )


def _verdict_from_same_product_json(
    parsed: dict[str, Any], *, asin: str, content: str, log_label: str
) -> tuple[Verdict, str]:
    if "same_product" in parsed:
        sp = parsed["same_product"]
        if sp is True:
            return "accept", "llm_same_product"
        if sp is False:
            return "reject", "llm_not_same_product"
    if "match" in parsed:
        m = parsed["match"]
        if m is True:
            return "accept", "llm_match_alias"
        if m is False:
            return "reject", "llm_match_false"
    logger.warning("%s ASIN validate unparseable JSON (asin=%s): %r", log_label, asin, content[:300])
    return "error", "unparseable_llm_response"


def haiku_validate_asin_vs_description(
    api_key: str,
    model: str,
    *,
    sheet_description: str,
    distributor_sku: Optional[str],
    asin: str,
    amazon_title: Optional[str],
    amazon_brand: Optional[str],
    source_file_hint: Optional[str] = None,
    timeout: float = 120.0,
    anthropic_usage: Optional[AnthropicUsageLedger] = None,
) -> tuple[Verdict, str]:
    """
    Same contract as ``ollama_validate_asin_vs_description``: Claude Haiku decides same_product.
    """
    prompt = _asin_match_validation_prompt(
        sheet_description=sheet_description,
        distributor_sku=distributor_sku,
        asin=asin,
        amazon_title=amazon_title,
        amazon_brand=amazon_brand,
        source_file_hint=source_file_hint,
    )
    if not prompt:
        return "error", "empty_sheet_description"

    try:
        text = _anthropic_first_message_text(
            api_key.strip(),
            model or "claude-haiku-4-5-20251001",
            prompt,
            max_tokens=512,
            timeout=min(float(timeout), 120.0),
            anthropic_usage=anthropic_usage,
        )
    except Exception as e:
        logger.warning("Haiku ASIN validate request failed: %s", e)
        return "error", str(e)

    parsed = _parse_json_from_content(text)
    return _verdict_from_same_product_json(parsed, asin=asin, content=text, log_label="Haiku")


def ollama_validate_asin_vs_description(
    base_url: str,
    model: str,
    *,
    sheet_description: str,
    distributor_sku: Optional[str],
    asin: str,
    amazon_title: Optional[str],
    amazon_brand: Optional[str],
    source_file_hint: Optional[str] = None,
    ollama_usage: Optional[OllamaTokenLedger] = None,
    timeout: float = 120.0,
) -> tuple[Verdict, str]:
    """
    Local Ollama: whether the Amazon ASIN is the same product as the distributor line.

    Returns (verdict, note). ``reject`` only when the model explicitly answers not a match.
    """
    prompt = _asin_match_validation_prompt(
        sheet_description=sheet_description,
        distributor_sku=distributor_sku,
        asin=asin,
        amazon_title=amazon_title,
        amazon_brand=amazon_brand,
        source_file_hint=source_file_hint,
    )
    if not prompt:
        return "error", "empty_sheet_description"

    url = base_url.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
            record_chat_response(ollama_usage, data)
    except Exception as e:
        logger.warning("Ollama ASIN validate request failed: %s", e)
        return "error", str(e)

    content = (data.get("message") or {}).get("content") or ""
    parsed = _parse_json_from_content(content)
    return _verdict_from_same_product_json(parsed, asin=asin, content=content, log_label="Ollama")


def _finder_pick_asin_prompt_and_asins(
    *,
    distributor_sku: str,
    sheet_description: str,
    source_file_hint: Optional[str],
    candidates: list[tuple[str, str]],
) -> tuple[Optional[str], set[str]]:
    if not candidates:
        return None, set()
    body_lines: list[str] = []
    asin_set: set[str] = set()
    for i, (a, t) in enumerate(candidates[:22], start=1):
        aa = str(a).strip().upper()
        asin_set.add(aa)
        body_lines.append(f"{i}. {aa} | {(t or '')[:140]}")
    block = "\n".join(body_lines)
    sku = (distributor_sku or "").strip()[:80]
    desc = (sheet_description or "").strip()[:700]
    prompt = (
        "You match distributor catalog lines to Amazon listings (ASIN + title).\n"
        "Pick the ONE listing that is the same product / same SKU as the line item.\n"
        "If none clearly match, answer with null.\n"
        "Minor naming, pack wording, or region differences are OK.\n"
        f"{_source_file_prompt_fragment(source_file_hint)}"
        f"\ndistributor_sku: {sku or '(none)'}\n"
        f"distributor_description: {desc or '(none)'}\n\n"
        "Amazon candidates (from Keepa):\n"
        f"{block}\n\n"
        'Reply with JSON only: {"chosen_asin": "B0XXXXXXXXXX"} or {"chosen_asin": null}\n'
    )
    return prompt, asin_set


def _chosen_asin_from_llm_text(text: str, asin_set: set[str], log_label: str) -> Optional[str]:
    parsed = _parse_json_from_content(text)
    raw = parsed.get("chosen_asin")
    if raw is None:
        alt = parsed.get("asin")
        raw = alt
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    pick = str(raw).strip().upper()
    if pick in asin_set:
        return pick
    for a in asin_set:
        if pick.endswith(a) or a.endswith(pick):
            return a
    logger.debug("%s pick-asin returned unknown token: %r", log_label, raw)
    return None


def haiku_pick_asin_from_candidates(
    api_key: str,
    model: str,
    *,
    distributor_sku: str,
    sheet_description: str,
    source_file_hint: Optional[str] = None,
    candidates: list[tuple[str, str]],
    timeout: float = 90.0,
    anthropic_usage: Optional[AnthropicUsageLedger] = None,
) -> Optional[str]:
    """Claude Haiku: same JSON contract as ``ollama_pick_asin_from_candidates``."""
    prompt, asin_set = _finder_pick_asin_prompt_and_asins(
        distributor_sku=distributor_sku,
        sheet_description=sheet_description,
        source_file_hint=source_file_hint,
        candidates=candidates,
    )
    if not prompt or not asin_set:
        return None
    try:
        text = _anthropic_first_message_text(
            api_key,
            model,
            prompt,
            max_tokens=512,
            timeout=min(float(timeout), 120.0),
            anthropic_usage=anthropic_usage,
        )
    except Exception as e:
        logger.warning("Haiku pick-asin failed: %s", e)
        return None
    return _chosen_asin_from_llm_text(text, asin_set, "Haiku")


def ollama_pick_asin_from_candidates(
    base_url: str,
    model: str,
    *,
    distributor_sku: str,
    sheet_description: str,
    source_file_hint: Optional[str] = None,
    candidates: list[tuple[str, str]],
    ollama_usage: Optional[OllamaTokenLedger] = None,
    timeout: float = 90.0,
) -> Optional[str]:
    """
    Local Ollama: choose which Amazon ASIN (if any) matches the distributor line, given Keepa titles.
    ``candidates`` are (asin, title) for listings that already returned from Keepa.
    """
    prompt, asin_set = _finder_pick_asin_prompt_and_asins(
        distributor_sku=distributor_sku,
        sheet_description=sheet_description,
        source_file_hint=source_file_hint,
        candidates=candidates,
    )
    if not prompt or not asin_set:
        return None

    url = base_url.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
            record_chat_response(ollama_usage, data)
    except Exception as e:
        logger.warning("Ollama pick-asin failed: %s", e)
        return None

    content = (data.get("message") or {}).get("content") or ""
    return _chosen_asin_from_llm_text(content, asin_set, "Ollama")


def _finder_escalation_user_prompt(
    *,
    distributor_sku: str,
    sheet_description: str,
    brand: Optional[str],
    source_file_hint: Optional[str],
    attempts_tried: str,
) -> str:
    sku = (distributor_sku or "").strip()[:80]
    desc = (sheet_description or "").strip()[:900]
    br = (brand or "").strip()[:100]
    return (
        "Keepa product_finder on Amazon failed for this distributor line.\n"
        "Suggest NEW search strings we should try: short Amazon-style title queries and "
        "alternate manufacturer part numbers (drop dots, try core model codes, etc.).\n"
        "Do not invent ASINs. Return only JSON.\n"
        f"{_source_file_prompt_fragment(source_file_hint)}"
        f"\ndistributor_sku: {sku or '(none)'}\n"
        f"distributor_brand_hint: {br or '(none)'}\n"
        f"distributor_description: {desc or '(none)'}\n"
        f"already_tried_summary: {attempts_tried[:400]}\n\n"
        'JSON shape: {"title_queries": ["string", ...], "part_numbers": ["string", ...]}\n'
        "At most 5 title_queries (each under 120 chars) and 3 part_numbers.\n"
    )


def _parse_finder_escalation_json(content: str) -> tuple[list[str], list[str]]:
    parsed = _parse_json_from_content(content)
    tq = parsed.get("title_queries") or parsed.get("titles") or []
    pq = parsed.get("part_numbers") or []
    titles: list[str] = []
    parts: list[str] = []
    if isinstance(tq, list):
        for x in tq[:5]:
            if isinstance(x, str) and x.strip():
                titles.append(x.strip()[:120])
    if isinstance(pq, list):
        for x in pq[:3]:
            if isinstance(x, str) and x.strip():
                parts.append(_norm_part_hint(x))
    return titles, parts


def haiku_suggest_finder_escalations(
    api_key: str,
    model: str,
    *,
    distributor_sku: str,
    sheet_description: str,
    brand: Optional[str],
    source_file_hint: Optional[str] = None,
    attempts_tried: str,
    timeout: float = 90.0,
    anthropic_usage: Optional[AnthropicUsageLedger] = None,
) -> tuple[list[str], list[str]]:
    """Claude Haiku: same contract as ``ollama_suggest_finder_escalations``."""
    prompt = _finder_escalation_user_prompt(
        distributor_sku=distributor_sku,
        sheet_description=sheet_description,
        brand=brand,
        source_file_hint=source_file_hint,
        attempts_tried=attempts_tried,
    )
    try:
        text = _anthropic_first_message_text(
            api_key,
            model,
            prompt,
            max_tokens=1024,
            timeout=min(float(timeout), 120.0),
            anthropic_usage=anthropic_usage,
        )
    except Exception as e:
        logger.warning("Haiku finder-escalation suggest failed: %s", e)
        return [], []
    return _parse_finder_escalation_json(text)


def ollama_suggest_finder_escalations(
    base_url: str,
    model: str,
    *,
    distributor_sku: str,
    sheet_description: str,
    brand: Optional[str],
    source_file_hint: Optional[str] = None,
    attempts_tried: str,
    ollama_usage: Optional[OllamaTokenLedger] = None,
    timeout: float = 90.0,
) -> tuple[list[str], list[str]]:
    """
    After basic Keepa finder attempts fail, local Ollama proposes extra title strings and part numbers
    to try with Keepa product_finder (no web search — only strings we feed to Keepa).
    """
    prompt = _finder_escalation_user_prompt(
        distributor_sku=distributor_sku,
        sheet_description=sheet_description,
        brand=brand,
        source_file_hint=source_file_hint,
        attempts_tried=attempts_tried,
    )

    url = base_url.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
            record_chat_response(ollama_usage, data)
    except Exception as e:
        logger.warning("Ollama finder-escalation suggest failed: %s", e)
        return [], []

    content = (data.get("message") or {}).get("content") or ""
    return _parse_finder_escalation_json(content)


def _norm_part_hint(s: str) -> str:
    t = str(s).strip()
    return t[:120]


def _keepa_domain_user_prompt(sample_text: str) -> Optional[str]:
    blob = (sample_text or "").strip()[:6000]
    if not blob:
        return None
    return (
        "You classify B2B / distributor spreadsheet content to choose the best Amazon storefront "
        "for product lookups (Keepa API domain id).\n\n"
        "Allowed ids ONLY (pick exactly one integer):\n"
        "1 = Amazon.com (US), 2 = Amazon.co.uk, 3 = Amazon.de, 4 = Amazon.fr, 5 = Amazon.co.jp, "
        "6 = Amazon.ca, 8 = Amazon.it, 9 = Amazon.es, 10 = Amazon.in, 11 = Amazon.com.mx, 12 = Amazon.com.br\n\n"
        "Use product description/title language and regional hints (e.g. CHF prices → often DE/FR; "
        "German text → 3; UK English → 2; US English → 1; EU multilingual → pick the dominant retail locale).\n\n"
        "Sample rows (may include multiple lines):\n---\n"
        f"{blob}\n---\n"
        'Reply with JSON only, no markdown: {"keepa_domain": <int>}\n'
    )


def _parse_keepa_domain_int(parsed: dict[str, Any], *, default_domain: int, raw_log: Any) -> int:
    raw = parsed.get("keepa_domain")
    if raw is None:
        return default_domain
    try:
        dom = int(raw)
    except (TypeError, ValueError):
        return default_domain
    if dom not in ALLOWED_KEEPA_DOMAINS:
        logger.debug("LLM returned out-of-range keepa_domain=%r", raw_log)
        return default_domain
    return dom


def haiku_infer_keepa_domain(
    sample_text: str,
    api_key: str,
    model: str,
    *,
    default_domain: int,
    timeout: float = 75.0,
    anthropic_usage: Optional[AnthropicUsageLedger] = None,
) -> int:
    """Claude Haiku: same task as ``ollama_infer_keepa_domain``."""
    prompt = _keepa_domain_user_prompt(sample_text)
    if not prompt:
        return default_domain
    try:
        text = _anthropic_first_message_text(
            api_key,
            model,
            prompt,
            max_tokens=256,
            timeout=min(float(timeout), 120.0),
            anthropic_usage=anthropic_usage,
        )
    except Exception as e:
        logger.warning("Haiku keepa-domain inference failed: %s", e)
        return default_domain
    parsed = _parse_json_from_content(text)
    return _parse_keepa_domain_int(parsed, default_domain=default_domain, raw_log=parsed.get("keepa_domain"))


def ollama_infer_keepa_domain(
    sample_text: str,
    base_url: str,
    model: str,
    *,
    default_domain: int,
    ollama_usage: Optional[OllamaTokenLedger] = None,
    timeout: float = 75.0,
) -> int:
    """
    Local Ollama model: which Amazon marketplace (Keepa ``domain`` id) best matches the sheet language/region.
    """
    prompt = _keepa_domain_user_prompt(sample_text)
    if not prompt:
        return default_domain

    url = base_url.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
            record_chat_response(ollama_usage, data)
    except Exception as e:
        logger.warning("Ollama keepa-domain inference failed: %s", e)
        return default_domain

    content = (data.get("message") or {}).get("content") or ""
    parsed = _parse_json_from_content(content)
    return _parse_keepa_domain_int(parsed, default_domain=default_domain, raw_log=parsed.get("keepa_domain"))


def _sheet_text_blob_for_domain(rows: list[dict[str, Any]], max_rows: int = 30, max_chars: int = 5500) -> str:
    parts: list[str] = []
    n = 0
    for r in rows:
        if n >= max_rows:
            break
        t = str(r.get("_sheet_title_text") or "").strip()
        if len(t) < 4:
            continue
        parts.append(t[:900])
        n += 1
    blob = "\n---\n".join(parts)
    return blob[:max_chars]


def assign_keepa_domains_to_rows(
    rows: list[dict[str, Any]],
    *,
    default_domain: int,
    enabled: bool,
    anthropic_api_key: str = "",
    haiku_model: str = "",
    ollama_base_url: str = "",
    ollama_model: str = "",
    timeout: float,
    progress: Optional[Callable[[str, str, int, int], None]] = None,
    ollama_usage: Optional[OllamaTokenLedger] = None,
    anthropic_usage: Optional[AnthropicUsageLedger] = None,
) -> None:
    """Sets ``row['_keepa_domain']`` (int) for every row, grouped by ``_sheet_name``."""
    by_sheet: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_sheet[str(r.get("_sheet_name") or "Sheet")].append(r)

    if not enabled:
        for r in rows:
            r["_keepa_domain"] = int(default_domain)
        return

    use_haiku = bool((anthropic_api_key or "").strip())
    use_ollama = bool((ollama_base_url or "").strip() and ollama_tags_reachable(ollama_base_url))
    if not use_haiku and not use_ollama:
        for r in rows:
            r["_keepa_domain"] = int(default_domain)
        return

    names = list(by_sheet.keys())
    total = len(names) or 1
    label = "Claude Haiku" if use_haiku else "Ollama"
    for i, sn in enumerate(names, start=1):
        rs = by_sheet[sn]
        if progress:
            progress(
                "sheet_domain",
                f"{label} → Keepa domain for “{sn[:40]}”… ({i} of {total})",
                i,
                total,
            )
        blob = _sheet_text_blob_for_domain(rs)
        if not blob.strip():
            dom = int(default_domain)
        elif use_haiku:
            dom = haiku_infer_keepa_domain(
                blob,
                (anthropic_api_key or "").strip(),
                haiku_model or "claude-haiku-4-5-20251001",
                default_domain=int(default_domain),
                timeout=min(float(timeout), 120.0),
                anthropic_usage=anthropic_usage,
            )
        else:
            dom = ollama_infer_keepa_domain(
                blob,
                ollama_base_url,
                ollama_model,
                default_domain=int(default_domain),
                ollama_usage=ollama_usage,
                timeout=min(float(timeout), 120.0),
            )
        for r in rs:
            r["_keepa_domain"] = int(dom)

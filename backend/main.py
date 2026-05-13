"""
FastAPI entrypoint: health, parse preview, full process → Excel (ASIN / GTIN / SKU tiers).

Column / header mapping and downstream LLM steps prefer Claude Haiku when ANTHROPIC_API_KEY is set.
Optional local Ollama is used only when Anthropic is not configured or a step falls back.
HIGH/MEDIUM rows receive a second Keepa pass (``history=1``) for a rolling **average price**
column (~6 months, NEW then Amazon) and **Monthly sales quantity** (Keepa ``monthlySold``)
in the workbook.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import threading
import time
import uuid
from datetime import datetime, timezone
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Callable, Optional

from backend.errors import JobCancelled
from backend.pipeline_metrics import PipelineSlotTracker
from backend.price_history_enrich import enrich_sections_price_history
from backend.global_rate_gates import get_shared_keepa_reactive_limiter

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.cache import Cache
from backend.anthropic_usage import AnthropicUsageLedger
from backend.http_pool import close_pools
from backend.keepa_telemetry import get_keepa_telemetry
from backend.lookup import (
    KeepaError,
    KeepaThrottle,
    fetch_keepa_product,
    fetch_keepa_product_by_code,
    fetch_keepa_products_batch,
    first_product,
    best_product_by_title,
)
from backend.ollama_asin_validate import (
    assign_keepa_domains_to_rows,
    haiku_validate_asin_vs_description,
    ollama_tags_reachable,
    ollama_validate_asin_vs_description,
)
from backend.ollama_usage import OllamaTokenLedger
from backend.output import passthrough_headers, workbook_from_sheet_sections
from backend.parser import parse_uploaded_file
from backend.process_history import (
    append_manifest,
    history_result_path,
    list_history,
    save_result_xlsx,
)
from backend.resolution import resolve_via_product_finder, sku_resolve_storage_key
from backend.validator import (
    aggregate_confidence,
    normalize_text,
    pack_consistency,
    validate_brand_match,
    validate_title_match,
)

import re as _re

_KNOWN_BRANDS = {
    "3m", "adidas", "asus", "beko", "black+decker", "bosch", "braun", "brother",
    "canon", "casio", "colgate", "cuisinart", "de'longhi", "delonghi", "dewalt",
    "dyson", "electrolux", "emsa", "epson", "faber-castell", "fissler",
    "garmin", "gillette", "grohe", "grundig", "hama", "hasbro", "hp",
    "jbl", "karcher", "kenwood", "kitchenaid", "krups", "lacoste", "lamy",
    "lego", "lenovo", "leifheit", "lg", "liebherr", "logitech", "makita",
    "melitta", "metabo", "miele", "moulinex", "nespresso", "nike",
    "nivea", "nokia", "oral-b", "oral b", "osram", "panasonic", "pelikan",
    "philips", "puma", "reebok", "remington", "rowenta", "samsung", "sanyo",
    "sennheiser", "sharp", "siemens", "sony", "staedtler", "stihl",
    "tefal", "tesa", "tommy hilfiger", "toshiba", "tupperware", "victorinox",
    "villeroy & boch", "villeroy&boch", "weber", "wmf", "wusthof", "zwilling",
    "swiss military", "henckels", "emerson", "samsonite", "leatherman",
    "gerber", "buck", "benchmade", "spyderco", "kershaw",
}

_BRAND_WORD_RE = _re.compile(r"[A-Za-z][A-Za-z0-9&'+\-]{1,30}")


def _infer_brand_from_context(
    filename: Optional[str],
    sheet_names: list[str],
) -> Optional[str]:
    """Best-effort brand extraction from filename and sheet names."""
    sources = []
    if filename:
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        sources.append(stem)
    sources.extend(sheet_names)
    text = " ".join(sources).lower()
    for brand in sorted(_KNOWN_BRANDS, key=len, reverse=True):
        if brand in text:
            return brand.title()
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    keepa_api_key: str = ""
    keepa_domain: int = 1
    keepa_cache_ttl_seconds: int = 86_400
    # Max parallel threads issuing Keepa calls inside one job (SKU/GTIN/domain batches).
    # High default: actual pacing is reactive (server-signal driven), not proactive.
    keepa_parallel_max: int = 50

    # Max parallel Haiku ASIN-validation workers per job.  Anthropic 429 retry (12 attempts
    # with backoff) handles rate limits reactively — no proactive RPM cap needed.
    haiku_validate_max_workers: int = 50

    # After resolution: fetch Keepa ``history=1`` for HIGH/MEDIUM rows (~6 months NEW/Amazon).
    keepa_price_history_enabled: bool = True
    keepa_price_history_months: float = 6.0

    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "gemma3:27b"
    anthropic_api_key: str = ""
    # Snapshot id (Anthropic retired claude-3-5-haiku-20241022; Haiku 4.5 is current default).
    haiku_model: str = "claude-haiku-4-5-20251001"

    # LLM ASIN vs listing check when request allows (Haiku if ANTHROPIC_API_KEY, else Ollama if reachable).
    use_ollama_asin_validate: bool = True
    ollama_asin_validate_timeout_sec: float = 120.0
    # When true with Ollama URL: also allow Ollama for SKU finder pick/escalation (Haiku used if ANTHROPIC_API_KEY set).
    use_ollama_resolver_gemma: bool = False
    # When true: allow Ollama for per-sheet Keepa domain (Haiku runs automatically when ANTHROPIC_API_KEY is set).
    use_ollama_sheet_domain: bool = False

    # Comma-separated browser origins allowed to call this API (e.g. your Next.js URL on Railway).
    cors_allow_origins: str = ""

    # Optional absolute path to Keepa / resolver SQLite cache (default: repo `data/trasco_cache.sqlite3`).
    # On Railway, point at a volume path if you want cache to survive redeploys.
    trasco_cache_db: str = ""


settings = Settings()
logger = logging.getLogger(__name__)
_cache: Optional[Cache] = None


def get_cache() -> Cache:
    global _cache
    if _cache is None:
        raw = (settings.trasco_cache_db or "").strip()
        _cache = Cache(Path(raw).expanduser()) if raw else Cache()
    return _cache


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_cache()
    yield
    close_pools()
    global _cache
    if _cache is not None:
        _cache.close()
        _cache = None


app = FastAPI(title="Trasco ASIN API (Haiku + Keepa)", version="0.2.0", lifespan=lifespan)


def _normalize_cors_origin(origin: str) -> Optional[str]:
    """
    Return a single origin string (scheme + host + optional port, no path/query).
    Local dev may use http://localhost / 127.0.0.1; production should use https://.
    """
    u = (origin or "").strip().rstrip("/")
    if not u or u == "*":
        return None
    if "://" not in u:
        u = "https://" + u
    low = u.lower()
    if low.startswith(("http://localhost", "http://127.0.0.1")):
        return u
    if low.startswith("https://"):
        return u
    logger.warning("CORS origin rejected (use https in production): %s", origin[:80])
    return None


def _cors_allow_origins() -> list[str]:
    raw = (settings.cors_allow_origins or "").strip()
    out: list[str] = []
    if raw:
        for chunk in raw.split(","):
            norm = _normalize_cors_origin(chunk)
            if norm and norm not in out:
                out.append(norm)
    if out:
        return out
    return ["http://localhost:3000", "http://127.0.0.1:3000"]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "HEAD", "OPTIONS"],
    # Origins are the main guard; keep headers permissive so multipart / fetch preflights succeed.
    allow_headers=["*"],
    max_age=600,
)


def _allowed_upload(name: str) -> bool:
    n = (name or "").lower()
    return n.endswith((".xlsx", ".xlsm", ".csv"))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/parse")
async def parse_sheet(
    file: UploadFile = File(...),
    use_ollama: bool = False,
) -> JSONResponse:
    if not _allowed_upload(file.filename or ""):
        raise HTTPException(400, "Upload an .xlsx, .xlsm, or .csv file.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file.")
    try:
        result = parse_uploaded_file(
            data,
            file.filename or "upload.xlsx",
            ollama_base_url=settings.ollama_base_url,
            ollama_model=settings.ollama_model,
            use_ollama=use_ollama,
            anthropic_api_key=settings.anthropic_api_key,
            haiku_model=settings.haiku_model,
        )
    except Exception as e:
        raise HTTPException(400, f"Failed to parse file: {e}") from e

    preview = result.rows[:10]
    return JSONResponse(
        {
            "headers": result.headers,
            "header_row_1based": result.header_row_1based,
            "sheets_processed": result.sheets_processed,
            "mapping": result.mapping,
            "detection_method": result.detection_method,
            "row_count": len(result.rows),
            "rows_with_asin": sum(1 for r in result.rows if r.get("_asin")),
            "rows_with_gtin": sum(1 for r in result.rows if r.get("_gtin")),
            "rows_with_sku": sum(1 for r in result.rows if r.get("_sku")),
            "rows_with_mpn": sum(1 for r in result.rows if r.get("_mpn")),
            "rows_with_lookup": sum(
                1
                for r in result.rows
                if r.get("_asin") or r.get("_gtin") or r.get("_sku") or r.get("_mpn")
            ),
            "preview": preview,
        }
    )


def _direct_lookup_key(row: dict[str, Any]) -> Optional[tuple[str, str]]:
    if row.get("_asin"):
        return ("asin", row["_asin"])
    if row.get("_gtin"):
        return ("code", row["_gtin"])
    return None


def _trace_snip(s: str, max_len: int = 420) -> str:
    t = (s or "").replace("\n", " ").replace("\r", " ").strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 3] + "..."


def _append_trace(base: Optional[str], part: str, *, max_len: int = 950) -> str:
    p = _trace_snip(part, max_len=max_len)
    if not base:
        return p
    merged = f"{base}; {p}"
    return merged if len(merged) <= max_len else merged[: max_len - 3] + "..."


def _identifier_flags(row: dict[str, Any]) -> str:
    """Compact bitmask-style hint for which normalized identifiers exist on the row."""

    def has(k: str) -> str:
        v = row.get(k)
        if v is None or v is False:
            return "0"
        if isinstance(v, str) and not v.strip():
            return "0"
        return "1"

    return f"A{has('_asin')}G{has('_gtin')}S{has('_sku')}M{has('_mpn')}"


def _group_rows_by_sheet(rows: list[dict[str, Any]]) -> "OrderedDict[str, list[dict[str, Any]]]":
    groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in rows:
        name = str(row.get("_sheet_name") or "Sheet")
        groups.setdefault(name, []).append(row)
    return groups


def _resolved_asin_confidence_product(
    row: dict[str, Any],
    *,
    keepa_products: dict[tuple[int, str, str], dict[str, Any]],
    errors: dict[tuple[int, str, str], str],
    sku_results: dict[str, tuple[Optional[dict[str, Any]], str]],
    settings: Settings,
    debug: bool = False,
) -> tuple[str, str, Optional[dict[str, Any]], str, dict[str, Any]]:
    """Amazon ASIN, confidence tier, Keepa product dict when resolved, a compact trace string, and debug dict."""
    sheet_title = row.get("_sheet_title_text")
    sheet_brand = row.get("_sheet_brand")
    domain = int(row.get("_keepa_domain") or settings.keepa_domain)
    dkey = _direct_lookup_key(row)
    sk = sku_resolve_storage_key(domain, row)
    flags = _identifier_flags(row)

    dbg: dict[str, Any] = {}
    if debug:
        dbg["dbg_parsed_asin"] = row.get("_asin") or ""
        dbg["dbg_parsed_gtin"] = row.get("_gtin") or ""
        dbg["dbg_parsed_sku"] = row.get("_sku") or ""
        dbg["dbg_parsed_mpn"] = row.get("_mpn") or ""
        dbg["dbg_parsed_brand"] = sheet_brand or ""
        dbg["dbg_parsed_title"] = sheet_title or ""
        dbg["dbg_keepa_domain"] = domain
        dbg["dbg_resolution_path"] = ""
        dbg["dbg_winning_attempt"] = ""
        dbg["dbg_title_score"] = ""
        dbg["dbg_brand_score"] = ""
        dbg["dbg_brand_sheet"] = ""
        dbg["dbg_brand_keepa"] = ""
        dbg["dbg_pack_sheet"] = ""
        dbg["dbg_pack_keepa"] = ""
        dbg["dbg_keepa_title"] = ""
        dbg["dbg_llm_pick_method"] = ""
        dbg["dbg_llm_validate_verdict"] = ""
        dbg["dbg_llm_validate_reason"] = ""
        dbg["dbg_fallback_attempted"] = ""
        dbg["dbg_fallback_result"] = ""

    prod: Optional[dict[str, Any]] = None
    base_trace = ""
    gtin_fallback = bool(row.get("_gtin_fallback"))
    if dkey:
        dk = (domain, dkey[0], dkey[1])
        kind, raw_val = dkey[0], str(dkey[1])
        val_snip = _trace_snip(f"{kind}={raw_val}", 72)
        if debug:
            dbg["dbg_resolution_path"] = f"direct_{kind}"
        direct_failed = False
        if dk in errors:
            direct_failed = True
        else:
            prod = keepa_products.get(dk)
            if not prod:
                direct_failed = True
        if direct_failed and gtin_fallback and sk and sk in sku_results:
            prod, sku_reason = sku_results[sk]
            base_trace = f"path=gtin_fallback|{val_snip}|{_trace_snip(sku_reason, 250)}"
            if debug:
                dbg["dbg_resolution_path"] = "gtin_fallback"
                dbg["dbg_winning_attempt"] = sku_reason
                dbg["dbg_fallback_attempted"] = "yes"
            if not prod:
                if debug:
                    dbg["dbg_fallback_result"] = "finder_no_match"
                return "", "NOT FOUND", None, base_trace, dbg
            if debug:
                dbg["dbg_fallback_result"] = f"found:{prod.get('asin', '')}"
        elif direct_failed:
            err_detail = _trace_snip(errors.get(dk, "no_product"), 220)
            if debug and gtin_fallback:
                dbg["dbg_fallback_attempted"] = "no_text"
            return "", "NOT FOUND", None, f"path=direct|{val_snip}|keepa_err={err_detail}", dbg
        else:
            base_trace = f"path=direct|{val_snip}"
    elif sk and sk in sku_results:
        prod, sku_reason = sku_results[sk]
        base_trace = f"path=sku|{_trace_snip(sku_reason, 300)}"
        if debug:
            dbg["dbg_resolution_path"] = "sku_finder"
            dbg["dbg_winning_attempt"] = sku_reason
        if not prod:
            return "", "NOT FOUND", None, base_trace, dbg
    else:
        if debug:
            dbg["dbg_resolution_path"] = "no_keys" if not sk else "sku_key_missing"
        if sk:
            return (
                "",
                "NOT FOUND",
                None,
                f"path=sku|resolver_key_missing_from_batch|id={flags}",
                dbg,
            )
        return "", "NOT FOUND", None, f"path=no_keys|id={flags}", dbg

    keepa_title = prod.get("title")
    keepa_brand = None
    for k in ("brand", "manufacturer"):
        v = prod.get(k)
        if isinstance(v, str) and v.strip():
            keepa_brand = v.strip()
            break

    ok_title, title_score, title_why = validate_title_match(sheet_title, keepa_title)
    ok_brand, brand_sc, brand_why = validate_brand_match(sheet_brand, keepa_brand)
    pack_ok, _sp, _ap, pack_why = pack_consistency(sheet_title, keepa_title)

    if debug:
        dbg["dbg_title_score"] = round(title_score, 4)
        dbg["dbg_brand_score"] = round(brand_sc, 4)
        dbg["dbg_brand_sheet"] = normalize_text(sheet_brand)
        dbg["dbg_brand_keepa"] = normalize_text(keepa_brand)
        dbg["dbg_pack_sheet"] = _sp if _sp is not None else ""
        dbg["dbg_pack_keepa"] = _ap if _ap is not None else ""
        dbg["dbg_keepa_title"] = str(keepa_title or "")[:500]

    if not pack_ok:
        row_status = "pack_mismatch"
    elif not ok_title:
        row_status = "ok_with_warnings"
    else:
        row_status = "ok"

    conf = aggregate_confidence(
        status=row_status,
        title_match=ok_title,
        brand_match=ok_brand,
        title_score=title_score,
        pack_ok=pack_ok,
    )
    ra = prod.get("asin")
    match_tail = (
        f"match|t_ok={int(ok_title)}|t_sc={title_score:.2f}|t={title_why}"
        f"|b_ok={int(ok_brand)}|b_sc={brand_sc:.2f}|b={brand_why}"
        f"|pk_ok={int(pack_ok)}|pk={pack_why}|conf={conf}"
    )
    full_trace = f"{base_trace}|{match_tail}"
    return (str(ra) if ra else ""), conf, prod, _trace_snip(full_trace, 950), dbg


ProgressCb = Optional[Callable[[str, str, int, int], None]]
"""phase, message, current (1-based step), total (0 = indeterminate bar)."""


RowCountCb = Optional[Callable[[int], None]]


def run_process_pipeline(
    data: bytes,
    filename: str,
    *,
    use_ollama: bool,
    max_rows: int,
    progress: ProgressCb,
    use_ollama_asin_validate: bool = True,
    on_row_count: RowCountCb = None,
    anthropic_usage: Optional[AnthropicUsageLedger] = None,
    ollama_usage: Optional[OllamaTokenLedger] = None,
    debug: bool = False,
    worker_metrics: Optional[PipelineSlotTracker] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> tuple[BytesIO, str, dict[str, Any]]:
    def p(phase: str, message: str, current: int = 0, total: int = 0) -> None:
        if should_cancel and should_cancel():
            raise JobCancelled()
        if progress:
            progress(phase, message, current, total)

    def _check_cancel() -> None:
        if should_cancel and should_cancel():
            raise JobCancelled()

    t0 = time.perf_counter()
    _check_cancel()
    ollama_ledger = ollama_usage if ollama_usage is not None else OllamaTokenLedger()
    anthropic_ledger = anthropic_usage if anthropic_usage is not None else AnthropicUsageLedger()
    tracker = worker_metrics if worker_metrics is not None else PipelineSlotTracker()

    validate_queue: list[
        tuple[dict[str, Any], dict[str, Any], str, str, dict[str, Any], str, str]
    ] = []
    do_llm_asin = bool(settings.use_ollama_asin_validate and use_ollama_asin_validate)

    p("parse", "Parsing spreadsheet…", 0, 0)
    try:
        parsed = parse_uploaded_file(
            data,
            filename or "upload.xlsx",
            ollama_base_url=settings.ollama_base_url,
            ollama_model=settings.ollama_model,
            use_ollama=use_ollama,
            anthropic_api_key=settings.anthropic_api_key,
            haiku_model=settings.haiku_model,
            ollama_usage=ollama_ledger,
            anthropic_usage=anthropic_ledger,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to parse file: {e}") from e

    rows_in = parsed.rows[: max(1, min(max_rows, 10_000))]
    if on_row_count:
        on_row_count(len(rows_in))
    _upload_stem = (filename or "upload.xlsx").rsplit(".", 1)[0].strip()[:240]
    for _r in rows_in:
        _r["_source_file_hint"] = _upload_stem

    any_brand = any(r.get("_sheet_brand") for r in rows_in)
    if not any_brand:
        inferred = _infer_brand_from_context(filename, parsed.sheets_processed)
        if inferred:
            logger.info("No brand column detected — inferred brand '%s' from file/sheet context", inferred)
            for _r in rows_in:
                _r["_sheet_brand"] = inferred
                _r["_brand_inferred"] = True

    p("parse", f"Parsed {len(rows_in)} row(s) (max {max_rows}).", 1, 1)

    domain_llm_enabled = bool(settings.use_ollama_sheet_domain or settings.anthropic_api_key.strip())
    assign_keepa_domains_to_rows(
        rows_in,
        default_domain=int(settings.keepa_domain),
        enabled=domain_llm_enabled,
        anthropic_api_key=settings.anthropic_api_key,
        haiku_model=settings.haiku_model,
        ollama_base_url=settings.ollama_base_url,
        ollama_model=settings.ollama_model,
        timeout=float(settings.ollama_asin_validate_timeout_sec),
        progress=p,
        ollama_usage=ollama_ledger,
        anthropic_usage=anthropic_ledger,
    )

    cache = get_cache()
    throttle = KeepaThrottle(reactive_limiter=get_shared_keepa_reactive_limiter())
    kpx = max(1, int(settings.keepa_parallel_max))

    keepa_products: dict[tuple[int, str, str], dict[str, Any]] = {}
    errors: dict[tuple[int, str, str], str] = {}

    unique_direct: list[tuple[int, str, str]] = []
    seen_direct: set[tuple[int, str, str]] = set()
    for row in rows_in:
        key = _direct_lookup_key(row)
        if not key:
            continue
        dom = int(row.get("_keepa_domain") or settings.keepa_domain)
        dk = (dom, key[0], key[1])
        if dk not in seen_direct:
            seen_direct.add(dk)
            unique_direct.append(dk)

    title_hint_for_key: dict[tuple[int, str, str], Optional[str]] = {}
    for row in rows_in:
        key = _direct_lookup_key(row)
        if not key:
            continue
        dom = int(row.get("_keepa_domain") or settings.keepa_domain)
        dk = (dom, key[0], key[1])
        if dk not in title_hint_for_key:
            title_hint_for_key[dk] = row.get("_sheet_title_text")

    asin_direct_by_domain: dict[int, list[str]] = {}
    code_direct: list[tuple[int, str, str]] = []
    for dk in unique_direct:
        dom, kind, val = dk
        if kind == "asin":
            asin_direct_by_domain.setdefault(dom, []).append(val)
        else:
            code_direct.append(dk)

    n_direct = len(unique_direct)
    done_direct = 0

    def _asin_domain_job(dom: int, asin_list: list[str]) -> tuple[dict[tuple[int, str, str], dict[str, Any]], dict[tuple[int, str, str], str], int]:
        local_p: dict[tuple[int, str, str], dict[str, Any]] = {}
        local_e: dict[tuple[int, str, str], str] = {}
        n_ok = 0
        with tracker.keepa_slot():
            try:
                batch_result = fetch_keepa_products_batch(
                    settings.keepa_api_key,
                    asin_list,
                    dom,
                    cache=cache,
                    cache_ttl_seconds=settings.keepa_cache_ttl_seconds,
                    history=0,
                    throttle=throttle,
                )
                for asin_val in asin_list:
                    dk2 = (dom, "asin", asin_val)
                    prod = batch_result.get(asin_val.strip().upper())
                    if prod:
                        local_p[dk2] = prod
                    else:
                        local_e[dk2] = "not_found"
                    n_ok += 1
            except KeepaError as e:
                for asin_val in asin_list:
                    local_e[(dom, "asin", asin_val)] = str(e)
                    n_ok += 1
            except Exception as e:
                for asin_val in asin_list:
                    local_e[(dom, "asin", asin_val)] = f"lookup_error:{e}"
                    n_ok += 1
        return local_p, local_e, n_ok

    dom_items = list(asin_direct_by_domain.items())
    if len(dom_items) <= 1:
        for dom, asin_list in dom_items:
            p(
                "keepa_direct",
                f"Keepa ASIN batch domain {dom} — {len(asin_list)} ASINs…",
                done_direct + 1,
                max(n_direct, 1),
            )
            lp, le, _ = _asin_domain_job(dom, asin_list)
            keepa_products.update(lp)
            errors.update(le)
            done_direct += sum(1 for _ in asin_list)
    else:
        pool_n = min(kpx, len(dom_items))
        tracker.configure_keepa_pool(pool_n)
        p(
            "keepa_direct",
            f"Keepa ASIN batches — {len(dom_items)} domains ({pool_n} workers)…",
            1,
            max(n_direct, 1),
        )
        with ThreadPoolExecutor(max_workers=pool_n) as pool:
            futs = {pool.submit(_asin_domain_job, dom, al): (dom, al) for dom, al in dom_items}
            for fut in as_completed(futs):
                lp, le, n_add = fut.result()
                _check_cancel()
                keepa_products.update(lp)
                errors.update(le)
                done_direct += n_add
                p(
                    "keepa_direct",
                    f"Keepa ASIN batches — merged domain batch ({done_direct} of {n_direct})…",
                    min(done_direct, n_direct),
                    max(n_direct, 1),
                )

    if code_direct:
        n_code = len(code_direct)
        pool_c = min(kpx, max(1, n_code))
        tracker.configure_keepa_pool(pool_c)
        done_lock = threading.Lock()

        def _one_code(dk: tuple[int, str, str]) -> None:
            nonlocal done_direct
            dom, _kind, val = dk
            with tracker.keepa_slot():
                try:
                    payload = fetch_keepa_product_by_code(
                        settings.keepa_api_key,
                        val,
                        dom,
                        cache=cache,
                        cache_ttl_seconds=settings.keepa_cache_ttl_seconds,
                        history=0,
                        throttle=throttle,
                    )
                    prod = best_product_by_title(payload, title_hint_for_key.get(dk))
                    if not prod:
                        errors[dk] = "not_found"
                    else:
                        keepa_products[dk] = prod
                except KeepaError as e:
                    errors[dk] = str(e)
                except Exception as e:
                    errors[dk] = f"lookup_error:{e}"
            with done_lock:
                done_direct += 1
                d = done_direct
            p(
                "keepa_direct",
                f"Keepa (GTIN/EAN) domain {dom} — {d} of {n_direct}…",
                min(d, n_direct),
                max(n_direct, 1),
            )

        with ThreadPoolExecutor(max_workers=pool_c) as pool:
            list(pool.map(_one_code, code_direct))

    failed_gtin_keys: set[tuple[int, str, str]] = set()
    for dk in code_direct:
        if dk in errors:
            failed_gtin_keys.add(dk)

    sku_keys: list[str] = []
    sku_row_for: dict[str, dict[str, Any]] = {}
    seen_sku: set[str] = set()
    for row in rows_in:
        _check_cancel()
        dkey = _direct_lookup_key(row)
        if dkey:
            dom = int(row.get("_keepa_domain") or settings.keepa_domain)
            dk = (dom, dkey[0], dkey[1])
            if dk not in failed_gtin_keys:
                continue
            fallback_mpn = row.get("_mpn_from_title")
            has_text = bool(
                fallback_mpn
                or (row.get("_sheet_title_text") or "").strip()
            )
            if not has_text:
                continue
            if fallback_mpn and not row.get("_mpn"):
                row["_mpn"] = fallback_mpn
            if not row.get("_sku") and not row.get("_mpn"):
                continue
            row["_gtin_fallback"] = True
        dom = int(row.get("_keepa_domain") or settings.keepa_domain)
        sk = sku_resolve_storage_key(dom, row)
        if sk and sk not in seen_sku:
            seen_sku.add(sk)
            sku_keys.append(sk)
            sku_row_for[sk] = row

    sku_results: dict[str, tuple[Optional[dict[str, Any]], str]] = {}
    n_sku = len(sku_keys)
    if n_sku:
        pool_s = min(kpx, max(1, n_sku))
        tracker.configure_keepa_pool(pool_s)
        sku_lock = threading.Lock()

        def _one_sku(sk: str) -> None:
            row0 = sku_row_for.get(sk) or {}
            row_dom = int(row0.get("_keepa_domain") or settings.keepa_domain)
            with tracker.keepa_slot():
                try:
                    prod, reason = resolve_via_product_finder(
                        settings.keepa_api_key,
                        row_dom,
                        cache,
                        brand=row0.get("_sheet_brand"),
                        sku=row0.get("_sku"),
                        mpn=row0.get("_mpn"),
                        title_hint=row0.get("_sheet_title_text"),
                        cache_ttl_seconds=settings.keepa_cache_ttl_seconds,
                        throttle=throttle,
                        anthropic_api_key=settings.anthropic_api_key,
                        haiku_model=settings.haiku_model,
                        ollama_base_url=settings.ollama_base_url,
                        ollama_model=settings.ollama_model,
                        ollama_timeout_sec=float(settings.ollama_asin_validate_timeout_sec),
                        use_ollama_resolver_gemma=settings.use_ollama_resolver_gemma,
                        source_file_hint=str(row0.get("_source_file_hint") or "").strip() or None,
                        ollama_usage=ollama_ledger,
                        anthropic_usage=anthropic_ledger,
                    )
                    with sku_lock:
                        sku_results[sk] = (prod, reason)
                except Exception as e:
                    with sku_lock:
                        sku_results[sk] = (None, str(e))
            with sku_lock:
                done_i = len(sku_results)
            p(
                "keepa_sku",
                f"Keepa product finder (SKU/MPN) {done_i} of {n_sku}…",
                done_i,
                max(n_sku, 1),
            )

        with ThreadPoolExecutor(max_workers=pool_s) as pool:
            list(pool.map(_one_sku, sku_keys))

    _DEBUG_HEADERS = [
        "dbg_parsed_asin",
        "dbg_parsed_gtin",
        "dbg_parsed_sku",
        "dbg_parsed_mpn",
        "dbg_parsed_brand",
        "dbg_parsed_title",
        "dbg_keepa_domain",
        "dbg_resolution_path",
        "dbg_winning_attempt",
        "dbg_title_score",
        "dbg_brand_score",
        "dbg_brand_sheet",
        "dbg_brand_keepa",
        "dbg_pack_sheet",
        "dbg_pack_keepa",
        "dbg_keepa_title",
        "dbg_llm_pick_method",
        "dbg_llm_validate_verdict",
        "dbg_llm_validate_reason",
        "dbg_fallback_attempted",
        "dbg_fallback_result",
    ]

    sections: list[tuple[str, list[str], list[dict[str, Any]], dict[str, str]]] = []
    grouped = _group_rows_by_sheet(rows_in)
    n_sheets = len(grouped) or 1
    for si, (sheet_name, sheet_rows) in enumerate(grouped.items(), start=1):
        p(
            "assemble",
            f"Building rows — sheet {si} of {n_sheets} ({sheet_name})…",
            si,
            n_sheets,
        )
        col_order = list(sheet_rows[0].get("_column_order") or [])
        if not col_order:
            col_order = [
                k for k in sheet_rows[0].keys() if isinstance(k, str) and not k.startswith("_")
            ]
        headers, asin_h, conf_h, avg_price_h, buy_box_incl_ship_h, take_home_h, roi_h, monthly_sales_qty_h, log_h, reject_asin_h = (
            passthrough_headers(col_order)
        )
        if debug:
            headers = headers + _DEBUG_HEADERS
        src_map = sheet_rows[0].get("_mapping")
        cost_col = ""
        if isinstance(src_map, dict):
            raw_cost = src_map.get("cost")
            if raw_cost is not None:
                cost_col = str(raw_cost).strip()
        meta = {
            "asin_h": asin_h,
            "conf_h": conf_h,
            "avg_price_h": avg_price_h,
            "buy_box_incl_ship_h": buy_box_incl_ship_h,
            "take_home_h": take_home_h,
            "roi_h": roi_h,
            "monthly_sales_qty_h": monthly_sales_qty_h,
            "cost_source_h": cost_col,
        }
        out_rows: list[dict[str, Any]] = []
        total_r = len(sheet_rows)
        for ri, row in enumerate(sheet_rows, start=1):
            _check_cancel()
            if ri == 1 or ri == total_r or ri % 25 == 0:
                p(
                    "assemble",
                    f"Resolving output rows — {ri} of {total_r} in {sheet_name}…",
                    ri,
                    max(total_r, 1),
                )
            ra, conf, prod, trace, dbg = _resolved_asin_confidence_product(
                row,
                keepa_products=keepa_products,
                errors=errors,
                sku_results=sku_results,
                settings=settings,
                debug=debug,
            )
            line = {h: row.get(h) for h in col_order}
            line[asin_h] = ra
            line[conf_h] = conf
            line[avg_price_h] = ""
            line[buy_box_incl_ship_h] = ""
            line[take_home_h] = ""
            line[roi_h] = ""
            line[monthly_sales_qty_h] = ""
            line[log_h] = trace
            line[reject_asin_h] = ""
            line["__trasco_domain"] = int(row.get("_keepa_domain") or settings.keepa_domain)
            if debug:
                for dh in _DEBUG_HEADERS:
                    line[dh] = dbg.get(dh, "")
            out_rows.append(line)
            if (
                do_llm_asin
                and ra
                and prod
                and str((row.get("_sheet_title_text") or "")).strip()
            ):
                validate_queue.append(
                    (line, row, asin_h, conf_h, prod, log_h, reject_asin_h)
                )
        sections.append((sheet_name, headers, out_rows, meta))

    if validate_queue and do_llm_asin:
        n_val = len(validate_queue)
        tmo = float(settings.ollama_asin_validate_timeout_sec)
        use_haiku_asin = bool(settings.anthropic_api_key.strip())
        ollama_ok = bool(
            (not use_haiku_asin)
            and (settings.ollama_base_url or "").strip()
            and ollama_tags_reachable(settings.ollama_base_url)
        )
        if not use_haiku_asin and not ollama_ok:
            logger.warning(
                "Skipping LLM ASIN validation: set ANTHROPIC_API_KEY (Haiku) or run Ollama (%s rows)",
                n_val,
            )
        else:
            label = "Haiku" if use_haiku_asin else "Ollama"
            wmax = max(1, int(settings.haiku_validate_max_workers))
            if use_haiku_asin:
                n_workers = min(wmax, n_val)
            else:
                n_workers = 1
            tracker.configure_llm_pool(n_workers)
            val_counter = [0]
            val_lock = threading.Lock()

            def _validate_one(
                idx: int,
                line: dict[str, Any],
                row: dict[str, Any],
                asin_h: str,
                conf_h: str,
                prod: dict[str, Any],
                log_h: str,
                reject_asin_h: str,
            ) -> None:
                with tracker.llm_slot():
                    desc = str((row.get("_sheet_title_text") or "")).strip()
                    ra0 = str(line.get(asin_h) or "").strip()
                    if not ra0:
                        return
                    sku_raw = row.get("_sku")
                    sku_s = (
                        str(sku_raw).strip()
                        if sku_raw is not None and str(sku_raw).strip()
                        else None
                    )
                    kt = prod.get("title") if isinstance(prod.get("title"), str) else None
                    kb = None
                    for k in ("brand", "manufacturer"):
                        v = prod.get(k)
                        if isinstance(v, str) and v.strip():
                            kb = v.strip()
                            break

                    if use_haiku_asin:
                        verdict, note = haiku_validate_asin_vs_description(
                            settings.anthropic_api_key.strip(),
                            settings.haiku_model,
                            sheet_description=desc,
                            distributor_sku=sku_s,
                            asin=ra0,
                            amazon_title=kt,
                            amazon_brand=kb,
                            source_file_hint=str(row.get("_source_file_hint") or "").strip() or None,
                            timeout=tmo,
                            anthropic_usage=anthropic_ledger,
                        )
                    else:
                        verdict, note = ollama_validate_asin_vs_description(
                            settings.ollama_base_url,
                            settings.ollama_model,
                            sheet_description=desc,
                            distributor_sku=sku_s,
                            asin=ra0,
                            amazon_title=kt,
                            amazon_brand=kb,
                            source_file_hint=str(row.get("_source_file_hint") or "").strip() or None,
                            ollama_usage=ollama_ledger,
                            timeout=tmo,
                        )
                    if debug:
                        line["dbg_llm_pick_method"] = label
                        line["dbg_llm_validate_verdict"] = verdict
                        line["dbg_llm_validate_reason"] = _trace_snip(note or "", 500)
                    if verdict == "reject":
                        line[reject_asin_h] = ra0
                        line[asin_h] = ""
                        line[conf_h] = "NOT FOUND (LLM)"
                        line[log_h] = _append_trace(
                            str(line.get(log_h) or ""),
                            f"llm_reject({label})={_trace_snip(note or '', 320)}",
                        )
                    elif verdict == "error":
                        line[log_h] = _append_trace(
                            str(line.get(log_h) or ""),
                            f"llm_inconclusive({label})={_trace_snip(note or '', 320)}",
                        )
                        logger.debug(
                            "%s ASIN validate inconclusive asin=%s note=%s",
                            label,
                            ra0,
                            note,
                        )
                    with val_lock:
                        val_counter[0] += 1
                        done_n = val_counter[0]
                    p(
                        "ollama_asin",
                        f"{label} validates ASIN vs listing ({done_n} of {n_val})…",
                        done_n,
                        n_val,
                    )

            llm_rejected: list[tuple[dict[str, Any], dict[str, Any], str, str, str, str]] = []
            reject_lock = threading.Lock()

            def _validate_one_and_collect(
                idx: int,
                line: dict[str, Any],
                row: dict[str, Any],
                asin_h: str,
                conf_h: str,
                prod: dict[str, Any],
                log_h: str,
                reject_asin_h: str,
            ) -> None:
                _validate_one(idx, line, row, asin_h, conf_h, prod, log_h, reject_asin_h)
                if line.get(conf_h) == "NOT FOUND (LLM)":
                    with reject_lock:
                        llm_rejected.append((line, row, asin_h, conf_h, log_h, reject_asin_h))

            p(
                "ollama_asin",
                f"{label} validating {n_val} ASINs ({n_workers} workers)…",
                0,
                n_val,
            )
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                futs = []
                for i, (line, row, ah, ch, prod, lh, rh) in enumerate(validate_queue):
                    futs.append(pool.submit(_validate_one_and_collect, i, line, row, ah, ch, prod, lh, rh))
                for fut in as_completed(futs):
                    exc = fut.exception()
                    if exc:
                        logger.warning("LLM ASIN validate worker error: %s", exc)

            retry_candidates = [
                (line, row, ah, ch, lh, rah)
                for line, row, ah, ch, lh, rah in llm_rejected
                if (row.get("_mpn_from_title") or row.get("_mpn") or row.get("_sku")
                    or (row.get("_sheet_title_text") or "").strip())
            ]
            if retry_candidates:
                n_retry = len(retry_candidates)
                p("keepa_sku", f"Finder retry for {n_retry} LLM-rejected row(s)…", 0, n_retry)
                retry_pool_n = min(kpx, max(1, n_retry))
                tracker.configure_keepa_pool(retry_pool_n)
                retry_counter = [0]
                retry_count_lock = threading.Lock()

                def _retry_one(
                    item: tuple[dict[str, Any], dict[str, Any], str, str, str, str],
                ) -> None:
                    line, row, asin_h_l, conf_h_l, log_h_l, rej_h_l = item
                    rejected_asin = str(line.get(rej_h_l) or "").strip().upper()
                    excl = {rejected_asin} if rejected_asin else set()
                    row_dom = int(row.get("_keepa_domain") or settings.keepa_domain)
                    mpn_fb = row.get("_mpn_from_title") or row.get("_mpn")
                    sku_fb = row.get("_sku")
                    with tracker.keepa_slot():
                        try:
                            prod2, reason2 = resolve_via_product_finder(
                                settings.keepa_api_key,
                                row_dom,
                                cache,
                                brand=row.get("_sheet_brand"),
                                sku=sku_fb,
                                mpn=mpn_fb,
                                title_hint=row.get("_sheet_title_text"),
                                cache_ttl_seconds=settings.keepa_cache_ttl_seconds,
                                throttle=throttle,
                                anthropic_api_key=settings.anthropic_api_key,
                                haiku_model=settings.haiku_model,
                                ollama_base_url=settings.ollama_base_url,
                                ollama_model=settings.ollama_model,
                                ollama_timeout_sec=float(settings.ollama_asin_validate_timeout_sec),
                                use_ollama_resolver_gemma=settings.use_ollama_resolver_gemma,
                                source_file_hint=str(row.get("_source_file_hint") or "").strip() or None,
                                ollama_usage=ollama_ledger,
                                anthropic_usage=anthropic_ledger,
                                exclude_asins=excl,
                            )
                        except Exception as e:
                            prod2, reason2 = None, str(e)
                    if prod2:
                        new_asin = prod2.get("asin", "")
                        ok_t, t_sc, t_why = validate_title_match(
                            row.get("_sheet_title_text"), prod2.get("title"),
                        )
                        ok_b, b_sc, b_why = validate_brand_match(
                            row.get("_sheet_brand"),
                            next((prod2.get(k) for k in ("brand", "manufacturer")
                                  if isinstance(prod2.get(k), str) and prod2.get(k, "").strip()), None),
                        )
                        pk_ok, _sp, _ap, pk_why = pack_consistency(
                            row.get("_sheet_title_text"), prod2.get("title"),
                        )
                        st = "pack_mismatch" if not pk_ok else ("ok" if ok_t else "ok_with_warnings")
                        conf2 = aggregate_confidence(
                            status=st, title_match=ok_t, brand_match=ok_b,
                            title_score=t_sc, pack_ok=pk_ok,
                        )
                        line[asin_h_l] = str(new_asin)
                        line[conf_h_l] = conf2
                        line[log_h_l] = _append_trace(
                            str(line.get(log_h_l) or ""),
                            f"llm_reject_retry|finder={_trace_snip(reason2, 200)}"
                            f"|t_sc={t_sc:.2f}|b_sc={b_sc:.2f}|conf={conf2}",
                        )
                        if debug:
                            line["dbg_fallback_attempted"] = "yes_llm_retry"
                            line["dbg_fallback_result"] = f"found:{new_asin}"
                    else:
                        line[log_h_l] = _append_trace(
                            str(line.get(log_h_l) or ""),
                            f"llm_reject_retry_failed|{_trace_snip(reason2, 200)}",
                        )
                        if debug:
                            line["dbg_fallback_attempted"] = "yes_llm_retry"
                            line["dbg_fallback_result"] = "finder_no_match"
                    with retry_count_lock:
                        retry_counter[0] += 1
                        done_r = retry_counter[0]
                    p("keepa_sku", f"Finder retry {done_r} of {n_retry}…", done_r, n_retry)

                with ThreadPoolExecutor(max_workers=retry_pool_n) as pool:
                    list(pool.map(_retry_one, retry_candidates))

    if settings.keepa_price_history_enabled:
        p(
            "keepa_price_hist",
            "Fetching 6-month average prices & monthly sales (HIGH/MEDIUM)…",
            0,
            0,
        )
        try:
            enrich_sections_price_history(
                sections,
                api_key=settings.keepa_api_key,
                cache=cache,
                cache_ttl_seconds=settings.keepa_cache_ttl_seconds,
                throttle=throttle,
                keepa_parallel_max=kpx,
                tracker=tracker,
                progress=p,
                should_cancel=should_cancel,
                months=float(settings.keepa_price_history_months),
            )
        except JobCancelled:
            raise

    p("workbook", "Writing Excel workbook…", 0, 0)
    buf = workbook_from_sheet_sections(sections)
    base_fn = (filename or "results").rsplit(".", 1)[0]
    download_name = base_fn + "_trasco_results.xlsx"
    p("done", "Done.", 1, 1)
    stats: dict[str, Any] = dict(ollama_ledger.to_stats_dict())
    stats.update(anthropic_ledger.to_stats_dict())
    stats["duration_sec"] = round(time.perf_counter() - t0, 3)
    return buf, download_name, stats


@dataclass
class ProcessJob:
    status: str = "queued"  # queued | running | complete | error | cancelled
    phase: str = "queued"
    message: str = ""
    current: int = 0
    total: int = 0
    error: Optional[str] = None
    result_path: Optional[str] = None
    download_name: Optional[str] = None
    duration_sec: Optional[float] = None
    ollama_prompt_tokens: int = 0
    ollama_completion_tokens: int = 0
    ollama_total_tokens: int = 0
    ollama_requests: int = 0
    anthropic_input_tokens: int = 0
    anthropic_output_tokens: int = 0
    anthropic_total_tokens: int = 0
    anthropic_requests: int = 0
    pipeline_keepa_workers_active: int = 0
    pipeline_keepa_workers_cap: int = 0
    pipeline_llm_workers_active: int = 0
    pipeline_llm_workers_cap: int = 0
    source_filename: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    row_count: int = 0
    completed_at: Optional[float] = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    cancel_event: threading.Event = field(default_factory=threading.Event)


_process_jobs: dict[str, ProcessJob] = {}
_COMPLETED_JOB_TTL_SEC = 7200.0  # 2 hours


def _purge_stale_jobs() -> None:
    """Remove completed/errored jobs older than TTL from in-memory dict."""
    now = time.time()
    stale = [
        jid
        for jid, j in _process_jobs.items()
        if j.completed_at is not None and (now - j.completed_at) > _COMPLETED_JOB_TTL_SEC
    ]
    for jid in stale:
        _process_jobs.pop(jid, None)


def _process_queue_stats() -> dict[str, Any]:
    """Counts jobs still held in memory (for UI: shared Keepa / API load)."""
    queued = running = complete = error = 0
    for j in _process_jobs.values():
        with j.lock:
            st = j.status
        if st == "queued":
            queued += 1
        elif st == "running":
            running += 1
        elif st == "complete":
            complete += 1
        elif st == "error":
            error += 1
        # cancelled: not active (completed)
    active = queued + running
    return {
        "jobs_in_memory": len(_process_jobs),
        "active": active,
        "queued": queued,
        "running": running,
        "complete": complete,
        "error": error,
    }


def _job_snapshot(job: ProcessJob) -> dict[str, Any]:
    with job.lock:
        elapsed = round(time.time() - job.started_at, 1)
        snap: dict[str, Any] = {
            "status": job.status,
            "phase": job.phase,
            "message": job.message,
            "current": job.current,
            "total": job.total,
            "error": job.error,
            "row_count": job.row_count,
            "elapsed_sec": elapsed,
        }
        snap["anthropic_input_tokens"] = job.anthropic_input_tokens
        snap["anthropic_output_tokens"] = job.anthropic_output_tokens
        snap["anthropic_total_tokens"] = job.anthropic_total_tokens
        snap["anthropic_requests"] = job.anthropic_requests
        snap["ollama_prompt_tokens"] = job.ollama_prompt_tokens
        snap["ollama_completion_tokens"] = job.ollama_completion_tokens
        snap["ollama_total_tokens"] = job.ollama_total_tokens
        snap["ollama_requests"] = job.ollama_requests
        snap["pipeline_keepa_workers_active"] = job.pipeline_keepa_workers_active
        snap["pipeline_keepa_workers_cap"] = job.pipeline_keepa_workers_cap
        snap["pipeline_llm_workers_active"] = job.pipeline_llm_workers_active
        snap["pipeline_llm_workers_cap"] = job.pipeline_llm_workers_cap
        if job.status == "complete":
            snap["duration_sec"] = job.duration_sec
            snap["source_filename"] = job.source_filename
            snap["download_name"] = job.download_name
        return snap


async def _run_process_job(
    job_id: str,
    data: bytes,
    filename: str,
    use_ollama: bool,
    max_rows: int,
    use_ollama_asin_validate: bool,
    debug: bool = False,
) -> None:
    job = _process_jobs.get(job_id)
    if job is None:
        return

    with job.lock:
        if job.cancel_event.is_set():
            job.status = "cancelled"
            job.phase = "cancelled"
            job.message = "Cancelled by user."
            job.error = None
            job.completed_at = time.time()
            return

    ollama_ledger = OllamaTokenLedger()
    anthropic_ledger = AnthropicUsageLedger()
    slot_tracker = PipelineSlotTracker()

    def progress(phase: str, message: str, current: int, total: int) -> None:
        au = anthropic_ledger.to_stats_dict()
        ou = ollama_ledger.to_stats_dict()
        slots = slot_tracker.snapshot()
        with job.lock:
            job.status = "running"
            job.phase = phase
            job.message = message
            job.current = current
            job.total = total
            job.anthropic_input_tokens = int(au.get("anthropic_input_tokens") or 0)
            job.anthropic_output_tokens = int(au.get("anthropic_output_tokens") or 0)
            job.anthropic_total_tokens = int(au.get("anthropic_total_tokens") or 0)
            job.anthropic_requests = int(au.get("anthropic_requests") or 0)
            job.ollama_prompt_tokens = int(ou.get("ollama_prompt_tokens") or 0)
            job.ollama_completion_tokens = int(ou.get("ollama_completion_tokens") or 0)
            job.ollama_total_tokens = int(ou.get("ollama_total_tokens") or 0)
            job.ollama_requests = int(ou.get("ollama_requests") or 0)
            job.pipeline_keepa_workers_active = int(
                slots.get("pipeline_keepa_workers_active") or 0
            )
            job.pipeline_keepa_workers_cap = int(slots.get("pipeline_keepa_workers_cap") or 0)
            job.pipeline_llm_workers_active = int(
                slots.get("pipeline_llm_workers_active") or 0
            )
            job.pipeline_llm_workers_cap = int(slots.get("pipeline_llm_workers_cap") or 0)

    def set_row_count(n: int) -> None:
        with job.lock:
            job.row_count = n

    try:
        buf, download_name, stats = await asyncio.to_thread(
            run_process_pipeline,
            data,
            filename,
            use_ollama=use_ollama,
            max_rows=max_rows,
            progress=progress,
            use_ollama_asin_validate=use_ollama_asin_validate,
            on_row_count=set_row_count,
            anthropic_usage=anthropic_ledger,
            ollama_usage=ollama_ledger,
            debug=debug,
            worker_metrics=slot_tracker,
            should_cancel=job.cancel_event.is_set,
        )
        xlsx_bytes = buf.getvalue()
        path = save_result_xlsx(job_id, xlsx_bytes)
        append_manifest(
            {
                "job_id": job_id,
                "source_filename": filename,
                "download_name": download_name,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "duration_sec": float(stats.get("duration_sec") or 0),
                "ollama_prompt_tokens": int(stats.get("ollama_prompt_tokens") or 0),
                "ollama_completion_tokens": int(stats.get("ollama_completion_tokens") or 0),
                "ollama_total_tokens": int(stats.get("ollama_total_tokens") or 0),
                "ollama_requests": int(stats.get("ollama_requests") or 0),
                "anthropic_input_tokens": int(stats.get("anthropic_input_tokens") or 0),
                "anthropic_output_tokens": int(stats.get("anthropic_output_tokens") or 0),
                "anthropic_total_tokens": int(stats.get("anthropic_total_tokens") or 0),
                "anthropic_requests": int(stats.get("anthropic_requests") or 0),
            }
        )
        with job.lock:
            job.status = "complete"
            job.phase = "done"
            job.message = "Ready to download."
            job.current = 1
            job.total = 1
            job.result_path = path
            job.download_name = download_name
            job.source_filename = filename
            job.completed_at = time.time()
            job.duration_sec = float(stats.get("duration_sec") or 0)
            job.ollama_prompt_tokens = int(stats.get("ollama_prompt_tokens") or 0)
            job.ollama_completion_tokens = int(stats.get("ollama_completion_tokens") or 0)
            job.ollama_total_tokens = int(stats.get("ollama_total_tokens") or 0)
            job.ollama_requests = int(stats.get("ollama_requests") or 0)
            job.anthropic_input_tokens = int(stats.get("anthropic_input_tokens") or 0)
            job.anthropic_output_tokens = int(stats.get("anthropic_output_tokens") or 0)
            job.anthropic_total_tokens = int(stats.get("anthropic_total_tokens") or 0)
            job.anthropic_requests = int(stats.get("anthropic_requests") or 0)
    except JobCancelled:
        with job.lock:
            job.status = "cancelled"
            job.phase = "cancelled"
            job.message = "Cancelled by user."
            job.error = None
            job.completed_at = time.time()
    except Exception as e:
        with job.lock:
            job.status = "error"
            job.phase = "error"
            job.message = str(e)
            job.error = str(e)
            job.completed_at = time.time()


@app.post("/api/v1/process/cancel/{job_id}")
def process_cancel(job_id: str) -> dict[str, str]:
    """Request cooperative cancellation of a queued or running job."""
    _purge_stale_jobs()
    job = _process_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job_id.")
    with job.lock:
        st = job.status
    if st == "complete":
        raise HTTPException(409, "Job already finished.")
    if st in ("error", "cancelled"):
        return {"ok": "true"}
    job.cancel_event.set()
    return {"ok": "true"}


@app.post("/api/v1/process")
async def process_sheet(
    file: UploadFile = File(...),
    use_ollama: bool = False,
    use_ollama_asin_validate: bool = False,
    max_rows: int = 10_000,
    debug: bool = False,
) -> StreamingResponse:
    if not settings.keepa_api_key.strip():
        raise HTTPException(500, "KEEPA_API_KEY is not configured in .env")
    if not _allowed_upload(file.filename or ""):
        raise HTTPException(400, "Upload an .xlsx, .xlsm, or .csv file.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file.")
    try:
        buf, download_name, stats = await asyncio.to_thread(
            run_process_pipeline,
            data,
            file.filename or "upload.xlsx",
            use_ollama=use_ollama,
            max_rows=max_rows,
            progress=None,
            use_ollama_asin_validate=use_ollama_asin_validate,
            debug=debug,
        )
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
    hdrs = {
        "Content-Disposition": f'attachment; filename="{download_name}"',
        "X-Trasco-Duration-Sec": str(stats.get("duration_sec", "")),
        "X-Trasco-Ollama-Total-Tokens": str(stats.get("ollama_total_tokens", "")),
        "X-Trasco-Ollama-Requests": str(stats.get("ollama_requests", "")),
    }
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=hdrs,
    )


@app.post("/api/v1/process/start")
async def process_start(
    file: UploadFile = File(...),
    use_ollama: bool = False,
    use_ollama_asin_validate: bool = False,
    max_rows: int = 10_000,
    debug: bool = False,
) -> dict[str, str]:
    if not settings.keepa_api_key.strip():
        raise HTTPException(500, "KEEPA_API_KEY is not configured in .env")
    if not _allowed_upload(file.filename or ""):
        raise HTTPException(400, "Upload an .xlsx, .xlsm, or .csv file.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file.")
    _purge_stale_jobs()
    job_id = str(uuid.uuid4())
    job = ProcessJob(
        status="queued",
        phase="queued",
        message="Starting…",
        current=0,
        total=0,
    )
    _process_jobs[job_id] = job
    asyncio.create_task(
        _run_process_job(
            job_id,
            data,
            file.filename or "upload.xlsx",
            use_ollama,
            max_rows,
            use_ollama_asin_validate,
            debug,
        )
    )
    return {"job_id": job_id}


@app.get("/api/v1/process/queue-stats")
def process_queue_stats() -> dict[str, Any]:
    """How many process jobs are in memory; active = queued + running (shared server load)."""
    _purge_stale_jobs()
    out = _process_queue_stats()
    out.update(get_keepa_telemetry())
    return out


@app.get("/api/v1/process/status/{job_id}")
def process_status(job_id: str) -> dict[str, Any]:
    job = _process_jobs.get(job_id)
    if job is not None:
        return _job_snapshot(job)
    path = history_result_path(job_id)
    if path is not None:
        for entry in list_history(200):
            if str(entry.get("job_id")) == job_id:
                return {
                    "status": "complete",
                    "phase": "done",
                    "message": "Ready to download.",
                    "current": 1,
                    "total": 1,
                    "error": None,
                    "row_count": 0,
                    "elapsed_sec": 0,
                    "duration_sec": float(entry.get("duration_sec") or 0),
                    "source_filename": entry.get("source_filename"),
                    "download_name": entry.get("download_name"),
                    "anthropic_input_tokens": int(entry.get("anthropic_input_tokens") or 0),
                    "anthropic_output_tokens": int(entry.get("anthropic_output_tokens") or 0),
                    "anthropic_total_tokens": int(entry.get("anthropic_total_tokens") or 0),
                    "anthropic_requests": int(entry.get("anthropic_requests") or 0),
                    "ollama_prompt_tokens": int(entry.get("ollama_prompt_tokens") or 0),
                    "ollama_completion_tokens": int(entry.get("ollama_completion_tokens") or 0),
                    "ollama_total_tokens": int(entry.get("ollama_total_tokens") or 0),
                    "ollama_requests": int(entry.get("ollama_requests") or 0),
                }
    raise HTTPException(404, "Unknown job_id.")


@app.get("/api/v1/process/result/{job_id}")
def process_result(job_id: str) -> FileResponse:
    job = _process_jobs.get(job_id)
    if job is not None:
        with job.lock:
            if job.status == "error":
                raise HTTPException(400, job.error or "Job failed.")
            if job.status != "complete" or not job.result_path:
                raise HTTPException(409, "Job not finished yet.")
            path = job.result_path
            name = job.download_name or "trasco_results.xlsx"
        return FileResponse(
            path,
            filename=name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    hist_path = history_result_path(job_id)
    if hist_path is not None:
        name = "trasco_results.xlsx"
        for entry in list_history(200):
            if str(entry.get("job_id")) == job_id:
                dn = entry.get("download_name")
                if isinstance(dn, str) and dn.strip():
                    name = dn.strip()
                break
        return FileResponse(
            str(hist_path),
            filename=name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    raise HTTPException(404, "Unknown job_id.")


@app.get("/api/v1/process/history")
def process_history_list() -> list[dict[str, Any]]:
    return list_history(100)


@app.get("/api/v1/process/history/{job_id}/result")
def process_history_result(job_id: str) -> FileResponse:
    path = history_result_path(job_id)
    if path is None:
        raise HTTPException(404, "History entry or file not found.")
    entries = list_history(200)
    name = "trasco_results.xlsx"
    for e in entries:
        if isinstance(e, dict) and str(e.get("job_id")) == job_id:
            dn = e.get("download_name")
            if isinstance(dn, str) and dn.strip():
                name = dn.strip()
            break
    return FileResponse(
        str(path),
        filename=name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "trasco-asin",
        "docs": "/docs",
        "health": "/health",
        "parse": "POST /api/v1/parse",
        "process": "POST /api/v1/process",
        "process_start": "POST /api/v1/process/start",
        "process_cancel": "POST /api/v1/process/cancel/{job_id}",
        "process_status": "GET /api/v1/process/status/{job_id}",
        "process_result": "GET /api/v1/process/result/{job_id}",
        "process_history": "GET /api/v1/process/history",
        "process_history_result": "GET /api/v1/process/history/{job_id}/result",
        "process_queue_stats": "GET /api/v1/process/queue-stats",
    }

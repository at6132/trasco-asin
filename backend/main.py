"""
FastAPI entrypoint: health, parse preview, full process → Excel (ASIN / GTIN / SKU tiers).

Column / header mapping and downstream LLM steps prefer Claude Haiku when ANTHROPIC_API_KEY is set.
Optional local Ollama is used only when Anthropic is not configured or a step falls back.
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

from backend.rate_limiter import RpmLimiter

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.cache import Cache
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
from backend.process_history import append_manifest, history_result_path, list_history, save_result_xlsx
from backend.resolution import resolve_via_product_finder, sku_resolve_storage_key
from backend.validator import (
    aggregate_confidence,
    pack_consistency,
    validate_brand_match,
    validate_title_match,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    keepa_api_key: str = ""
    keepa_domain: int = 1
    keepa_cache_ttl_seconds: int = 86_400
    keepa_min_request_interval_sec: float = 1.05

    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "gemma3:27b"
    anthropic_api_key: str = ""
    # Snapshot id (Anthropic retired claude-3-5-haiku-20241022; Haiku 4.5 is current default).
    haiku_model: str = "claude-haiku-4-5-20251001"

    # LLM ASIN vs listing check when request allows (Haiku if ANTHROPIC_API_KEY, else Ollama if reachable).
    use_ollama_asin_validate: bool = True
    ollama_asin_validate_timeout_sec: float = 120.0
    # Space out Haiku ASIN checks (~50 RPM org limits). Set 0 to rely only on 429 retries in ollama_asin_validate.
    anthropic_asin_validate_min_interval_sec: float = 1.25
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
) -> tuple[str, str, Optional[dict[str, Any]], str]:
    """Amazon ASIN, confidence tier, Keepa product dict when resolved, and a compact trace string."""
    sheet_title = row.get("_sheet_title_text")
    sheet_brand = row.get("_sheet_brand")
    domain = int(row.get("_keepa_domain") or settings.keepa_domain)
    dkey = _direct_lookup_key(row)
    sk = sku_resolve_storage_key(domain, row)
    flags = _identifier_flags(row)

    prod: Optional[dict[str, Any]] = None
    base_trace = ""
    if dkey:
        dk = (domain, dkey[0], dkey[1])
        kind, raw_val = dkey[0], str(dkey[1])
        val_snip = _trace_snip(f"{kind}={raw_val}", 72)
        if dk in errors:
            err = _trace_snip(errors[dk], 220)
            return "", "NOT FOUND", None, f"path=direct|{val_snip}|keepa_err={err}"
        prod = keepa_products.get(dk)
        if not prod:
            return "", "NOT FOUND", None, f"path=direct|{val_snip}|no_product"
        base_trace = f"path=direct|{val_snip}"
    elif sk and sk in sku_results:
        prod, sku_reason = sku_results[sk]
        base_trace = f"path=sku|{_trace_snip(sku_reason, 300)}"
        if not prod:
            return "", "NOT FOUND", None, base_trace
    else:
        if sk:
            return (
                "",
                "NOT FOUND",
                None,
                f"path=sku|resolver_key_missing_from_batch|id={flags}",
            )
        return "", "NOT FOUND", None, f"path=no_keys|id={flags}"

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
    return (str(ra) if ra else ""), conf, prod, _trace_snip(full_trace, 950)


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
) -> tuple[BytesIO, str, dict[str, Any]]:
    def p(phase: str, message: str, current: int = 0, total: int = 0) -> None:
        if progress:
            progress(phase, message, current, total)

    t0 = time.perf_counter()
    ollama_usage = OllamaTokenLedger()

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
            ollama_usage=ollama_usage,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to parse file: {e}") from e

    rows_in = parsed.rows[: max(1, min(max_rows, 10_000))]
    if on_row_count:
        on_row_count(len(rows_in))
    _upload_stem = (filename or "upload.xlsx").rsplit(".", 1)[0].strip()[:240]
    for _r in rows_in:
        _r["_source_file_hint"] = _upload_stem
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
        ollama_usage=ollama_usage,
    )

    cache = get_cache()
    throttle = KeepaThrottle(settings.keepa_min_request_interval_sec)

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

    for dom, asin_list in asin_direct_by_domain.items():
        p(
            "keepa_direct",
            f"Keepa ASIN batch domain {dom} — {len(asin_list)} ASINs…",
            done_direct + 1,
            max(n_direct, 1),
        )
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
                dk = (dom, "asin", asin_val)
                prod = batch_result.get(asin_val.strip().upper())
                if prod:
                    keepa_products[dk] = prod
                else:
                    errors[dk] = "not_found"
                done_direct += 1
        except KeepaError as e:
            for asin_val in asin_list:
                dk = (dom, "asin", asin_val)
                errors[dk] = str(e)
                done_direct += 1
        except Exception as e:
            for asin_val in asin_list:
                dk = (dom, "asin", asin_val)
                errors[dk] = f"lookup_error:{e}"
                done_direct += 1

    for dk in code_direct:
        dom, kind, val = dk
        done_direct += 1
        p(
            "keepa_direct",
            f"Keepa (GTIN/EAN) domain {dom} — {done_direct} of {n_direct}…",
            done_direct,
            max(n_direct, 1),
        )
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
                continue
            keepa_products[dk] = prod
        except KeepaError as e:
            errors[dk] = str(e)
        except Exception as e:
            errors[dk] = f"lookup_error:{e}"

    sku_keys: list[str] = []
    sku_row_for: dict[str, dict[str, Any]] = {}
    seen_sku: set[str] = set()
    for row in rows_in:
        if _direct_lookup_key(row):
            continue
        dom = int(row.get("_keepa_domain") or settings.keepa_domain)
        sk = sku_resolve_storage_key(dom, row)
        if sk and sk not in seen_sku:
            seen_sku.add(sk)
            sku_keys.append(sk)
            sku_row_for[sk] = row

    sku_results: dict[str, tuple[Optional[dict[str, Any]], str]] = {}
    n_sku = len(sku_keys)
    for i, sk in enumerate(sku_keys, start=1):
        p(
            "keepa_sku",
            f"Keepa product finder (SKU/MPN) {i} of {n_sku}…",
            i,
            max(n_sku, 1),
        )
        row0 = sku_row_for.get(sk) or {}
        row_dom = int(row0.get("_keepa_domain") or settings.keepa_domain)
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
                ollama_usage=ollama_usage,
            )
            sku_results[sk] = (prod, reason)
        except Exception as e:
            sku_results[sk] = (None, str(e))

    sections: list[tuple[str, list[str], list[dict[str, Any]]]] = []
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
        headers, asin_h, conf_h, log_h, reject_asin_h = passthrough_headers(col_order)
        out_rows: list[dict[str, Any]] = []
        total_r = len(sheet_rows)
        for ri, row in enumerate(sheet_rows, start=1):
            if ri == 1 or ri == total_r or ri % 25 == 0:
                p(
                    "assemble",
                    f"Resolving output rows — {ri} of {total_r} in {sheet_name}…",
                    ri,
                    max(total_r, 1),
                )
            ra, conf, prod, trace = _resolved_asin_confidence_product(
                row,
                keepa_products=keepa_products,
                errors=errors,
                sku_results=sku_results,
                settings=settings,
            )
            line = {h: row.get(h) for h in col_order}
            line[asin_h] = ra
            line[conf_h] = conf
            line[log_h] = trace
            line[reject_asin_h] = ""
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
        sections.append((sheet_name, headers, out_rows))

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
            haiku_rpm = RpmLimiter(requests_per_minute=45)
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

                haiku_rpm.acquire()

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
                        ollama_usage=ollama_usage,
                        timeout=tmo,
                    )
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

            n_workers = 4 if use_haiku_asin else 1
            p("ollama_asin", f"{label} validating {n_val} ASINs ({n_workers} workers)…", 0, n_val)
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                futs = []
                for i, (line, row, ah, ch, prod, lh, rh) in enumerate(validate_queue):
                    futs.append(pool.submit(_validate_one, i, line, row, ah, ch, prod, lh, rh))
                for fut in as_completed(futs):
                    exc = fut.exception()
                    if exc:
                        logger.warning("LLM ASIN validate worker error: %s", exc)

    p("workbook", "Writing Excel workbook…", 0, 0)
    buf = workbook_from_sheet_sections(sections)
    base_fn = (filename or "results").rsplit(".", 1)[0]
    download_name = base_fn + "_trasco_results.xlsx"
    p("done", "Done.", 1, 1)
    stats: dict[str, Any] = dict(ollama_usage.to_stats_dict())
    stats["duration_sec"] = round(time.perf_counter() - t0, 3)
    return buf, download_name, stats


@dataclass
class ProcessJob:
    status: str = "queued"  # queued | running | complete | error
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
    source_filename: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    row_count: int = 0
    completed_at: Optional[float] = None
    lock: threading.Lock = field(default_factory=threading.Lock)


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
        if job.status == "complete":
            snap["duration_sec"] = job.duration_sec
            snap["ollama_prompt_tokens"] = job.ollama_prompt_tokens
            snap["ollama_completion_tokens"] = job.ollama_completion_tokens
            snap["ollama_total_tokens"] = job.ollama_total_tokens
            snap["ollama_requests"] = job.ollama_requests
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
) -> None:
    job = _process_jobs.get(job_id)
    if job is None:
        return

    def progress(phase: str, message: str, current: int, total: int) -> None:
        with job.lock:
            job.status = "running"
            job.phase = phase
            job.message = message
            job.current = current
            job.total = total

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
    except Exception as e:
        with job.lock:
            job.status = "error"
            job.phase = "error"
            job.message = str(e)
            job.error = str(e)
            job.completed_at = time.time()


@app.post("/api/v1/process")
async def process_sheet(
    file: UploadFile = File(...),
    use_ollama: bool = False,
    use_ollama_asin_validate: bool = False,
    max_rows: int = 10_000,
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
        )
    )
    return {"job_id": job_id}


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
        "process_status": "GET /api/v1/process/status/{job_id}",
        "process_result": "GET /api/v1/process/result/{job_id}",
        "process_history": "GET /api/v1/process/history",
        "process_history_result": "GET /api/v1/process/history/{job_id}/result",
    }

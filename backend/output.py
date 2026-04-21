"""
Excel output: preserve source columns + appended ASIN, confidence, and optional trace column
(optional multi-sheet).
"""

from __future__ import annotations

from io import BytesIO
from typing import Any, Mapping, Sequence

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter


def _flatten_for_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _autosize_columns(ws: Any, col_count: int, max_width: int = 60) -> None:
    for i in range(1, col_count + 1):
        letter = get_column_letter(i)
        best = 10
        for cell in ws[letter]:
            if cell.value is None:
                continue
            best = min(max_width, max(best, len(str(cell.value)) + 2))
        ws.column_dimensions[letter].width = best


def passthrough_headers(col_order: list[str]) -> tuple[list[str], str, str, str, str]:
    """
    Original column order unchanged; return
    (full_row_headers, asin_header, confidence_header, trace_header, rejected_asin_header).
    If the sheet already uses those names, pick non-colliding append names.
    ``rejected_asin_header`` is filled when LLM ASIN validation rejects (else empty).
    """
    lower = {str(h).strip().lower() for h in col_order if h}
    asin_h = "ASIN"
    if asin_h.lower() in lower:
        asin_h = "Resolved ASIN"
    conf_h = "confidence"
    if conf_h.lower() in lower:
        conf_h = "Trasco confidence"
    log_h = "Trasco trace"
    if log_h.lower() in lower:
        log_h = "Trasco diagnostics"
    if log_h.lower() in lower:
        log_h = "_trasco_trace"
    rej_h = "Rejected ASIN (LLM)"
    if rej_h.lower() in lower:
        rej_h = "Trasco rejected ASIN"
    if rej_h.lower() in lower:
        rej_h = "_trasco_llm_rejected_asin"
    full = list(col_order) + [asin_h, conf_h, log_h, rej_h]
    return full, asin_h, conf_h, log_h, rej_h


def workbook_from_sheet_sections(
    sections: list[tuple[str, list[str], list[Mapping[str, Any]]]],
) -> BytesIO:
    """
    Each section: (worksheet_title, headers_in_order, row_dicts).
    Row dicts must contain exactly the keys in headers_in_order.
    """
    wb = Workbook()
    if not sections:
        ws = wb.active
        ws.title = "results"
        ws.append(["(no rows)"])
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        return buf

    header_font = Font(bold=True)
    fills = {
        "HIGH": PatternFill("solid", fgColor="C6EFCE"),
        "MEDIUM": PatternFill("solid", fgColor="FFEB9C"),
        "LOW": PatternFill("solid", fgColor="FFC7CE"),
        "NOT FOUND": PatternFill("solid", fgColor="D9D9D9"),
        "NOT FOUND (LLM)": PatternFill("solid", fgColor="D9D9D9"),
    }
    used_titles: set[str] = set()

    def _unique_sheet_title(raw: str) -> str:
        base = (raw or "Sheet")[:31] or "Sheet"
        t = base
        n = 1
        while t in used_titles:
            n += 1
            suffix = f"_{n}"
            t = (base[: 31 - len(suffix)] + suffix)[:31]
        used_titles.add(t)
        return t

    for idx, (title, headers, rows) in enumerate(sections):
        safe_title = _unique_sheet_title(title)
        if idx == 0:
            ws = wb.active
            ws.title = safe_title
        else:
            ws = wb.create_sheet(title=safe_title)

        if not headers:
            ws.append(["(empty)"])
            continue

        ws.append(headers)
        for col_i, _h in enumerate(headers, start=1):
            ws.cell(row=1, column=col_i).font = header_font

        conf_headers = {"confidence", "Trasco confidence"}
        conf_col_idx = None
        for i, h in enumerate(headers, start=1):
            if h in conf_headers:
                conf_col_idx = i
                break

        for row in rows:
            ws.append([_flatten_for_cell(row.get(h)) for h in headers])

        if conf_col_idx:
            letter = get_column_letter(conf_col_idx)
            for r in range(2, ws.max_row + 1):
                val = ws[f"{letter}{r}"].value
                if val in fills:
                    ws[f"{letter}{r}"].fill = fills[str(val)]

        ws.freeze_panes = "A2"
        _autosize_columns(ws, len(headers))

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def rows_to_workbook(rows: Sequence[Mapping[str, Any]]) -> BytesIO:
    """Backward-compatible single-sheet writer when rows already include final headers."""
    if not rows:
        return workbook_from_sheet_sections([("results", [], [])])
    headers = list(rows[0].keys())
    return workbook_from_sheet_sections([("results", headers, list(rows))])

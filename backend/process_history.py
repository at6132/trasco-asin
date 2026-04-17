"""Persist completed process jobs for re-download and usage history."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional

_lock = threading.Lock()
_JOB_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def history_root() -> Path:
    raw = (os.environ.get("TRASCO_PROCESS_HISTORY_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path(__file__).resolve().parent.parent / ".trasco_process_history").resolve()


def save_result_xlsx(job_id: str, content: bytes) -> str:
    if not _JOB_ID_RE.match(job_id):
        raise ValueError("invalid job_id")
    root = history_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{job_id}.xlsx"
    path.write_bytes(content)
    return str(path)


def append_manifest(entry: dict[str, Any]) -> None:
    root = history_root()
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "manifest.json"
    with _lock:
        entries: list[Any] = []
        if manifest.exists():
            try:
                entries = json.loads(manifest.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                entries = []
        if not isinstance(entries, list):
            entries = []
        entries.insert(0, entry)
        entries = entries[:200]
        manifest.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def list_history(max_n: int = 100) -> list[dict[str, Any]]:
    manifest = history_root() / "manifest.json"
    if not manifest.exists():
        return []
    try:
        entries = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(entries, list):
        return []
    out: list[dict[str, Any]] = []
    for e in entries:
        if isinstance(e, dict):
            jid = str(e.get("job_id") or "")
            if _JOB_ID_RE.match(jid):
                p = history_root() / f"{jid}.xlsx"
                e2 = dict(e)
                e2["file_available"] = p.is_file()
                out.append(e2)
        if len(out) >= max_n:
            break
    return out


def history_result_path(job_id: str) -> Optional[Path]:
    if not _JOB_ID_RE.match(job_id):
        return None
    p = history_root() / f"{job_id}.xlsx"
    return p if p.is_file() else None

"""
Run /api/v1/process on the four EXAMPLE_SPRAEDSHEETS workbooks and write example_outputs/*.xlsx
Usage (from trasco-asin/):  .venv\Scripts\python scripts\export_four_examples.py
Requires KEEPA_API_KEY in .env (and network).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from fastapi.testclient import TestClient

from backend.main import app


def main() -> None:
    exdir = Path(r"c:\Users\Avi\Trasco LLC\EXAMPLE_SPRAEDSHEETS")
    if not exdir.is_dir():
        print("Example folder not found:", exdir)
        sys.exit(1)

    outdir = ROOT / "example_outputs"
    outdir.mkdir(parents=True, exist_ok=True)

    with TestClient(app) as client:
        for p in sorted(exdir.glob("*.xlsx")):
            with open(p, "rb") as fp:
                r = client.post(
                    "/api/v1/process",
                    params={"use_ollama": "false", "max_rows": 500},
                    files={
                        "file": (
                            p.name,
                            fp,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
                    },
                    timeout=900.0,
                )
            dest = outdir / f"{p.stem}_trasco_results.xlsx"
            if r.status_code == 200:
                dest.write_bytes(r.content)
                print("Wrote", dest, f"({len(r.content)} bytes)")
            else:
                print("FAILED", p.name, r.status_code, r.text[:800])


if __name__ == "__main__":
    main()

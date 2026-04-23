"""Per-run counters for parallel Keepa / LLM worker slots (UI + tuning)."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator


class PipelineSlotTracker:
    """Thread-safe in-flight counts and pool caps for one ``run_process_pipeline`` invocation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.keepa_active = 0
        self.llm_active = 0
        self.keepa_cap = 0
        self.llm_cap = 0

    def configure_keepa_pool(self, cap: int) -> None:
        with self._lock:
            self.keepa_cap = max(0, int(cap))

    def configure_llm_pool(self, cap: int) -> None:
        with self._lock:
            self.llm_cap = max(0, int(cap))

    @contextmanager
    def keepa_slot(self) -> Iterator[None]:
        with self._lock:
            self.keepa_active += 1
        try:
            yield
        finally:
            with self._lock:
                self.keepa_active = max(0, self.keepa_active - 1)

    @contextmanager
    def llm_slot(self) -> Iterator[None]:
        with self._lock:
            self.llm_active += 1
        try:
            yield
        finally:
            with self._lock:
                self.llm_active = max(0, self.llm_active - 1)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "pipeline_keepa_workers_active": self.keepa_active,
                "pipeline_keepa_workers_cap": self.keepa_cap,
                "pipeline_llm_workers_active": self.llm_active,
                "pipeline_llm_workers_cap": self.llm_cap,
            }

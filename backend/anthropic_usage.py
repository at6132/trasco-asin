"""Thread-safe Anthropic Messages API usage totals for one process run."""

from __future__ import annotations

import threading
from typing import Any, Optional


class AnthropicUsageLedger:
    """Accumulates input/output tokens and request count from successful /v1/messages responses."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.requests: int = 0

    def record_messages_response(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        usage = data.get("usage")
        inp = out = 0
        if isinstance(usage, dict):
            inp = int(usage.get("input_tokens") or 0)
            out = int(usage.get("output_tokens") or 0)
            ccr = usage.get("cache_creation_input_tokens")
            cra = usage.get("cache_read_input_tokens")
            if isinstance(ccr, (int, float)):
                inp += int(ccr)
            if isinstance(cra, (int, float)):
                inp += int(cra)
        with self._lock:
            self.requests += 1
            self.input_tokens += max(0, inp)
            self.output_tokens += max(0, out)

    def to_stats_dict(self) -> dict[str, Any]:
        with self._lock:
            it = self.input_tokens
            ot = self.output_tokens
            rq = self.requests
        return {
            "anthropic_input_tokens": it,
            "anthropic_output_tokens": ot,
            "anthropic_total_tokens": it + ot,
            "anthropic_requests": rq,
        }


def record_anthropic_messages_response(
    ledger: Optional[AnthropicUsageLedger], data: Any
) -> None:
    if ledger is not None and isinstance(data, dict):
        ledger.record_messages_response(data)

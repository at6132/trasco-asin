"""Accumulate Ollama /api/chat token usage from JSON responses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class OllamaTokenLedger:
    """Sums ``prompt_eval_count`` and ``eval_count`` from Ollama chat completions."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    requests: int = 0

    def add_chat_response(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        pe = data.get("prompt_eval_count")
        ec = data.get("eval_count")
        if isinstance(pe, int) and pe >= 0:
            self.prompt_tokens += pe
        if isinstance(ec, int) and ec >= 0:
            self.completion_tokens += ec
        self.requests += 1

    def to_stats_dict(self) -> dict[str, Any]:
        return {
            "ollama_prompt_tokens": self.prompt_tokens,
            "ollama_completion_tokens": self.completion_tokens,
            "ollama_total_tokens": self.prompt_tokens + self.completion_tokens,
            "ollama_requests": self.requests,
        }


def record_chat_response(ledger: Optional[OllamaTokenLedger], data: Any) -> None:
    if ledger is not None:
        ledger.add_chat_response(data)

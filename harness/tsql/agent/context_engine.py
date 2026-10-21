"""The context engine contract.

Context management is pluggable: the loop asks the engine whether to compress
and, if so, lets it do the work. The built-in engine is a lossy summarizer
(:mod:`tsql.agent.context_compressor`); a plugin could provide a lossless one.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CompressionDecision:
    should_compress: bool = False
    reason: str = ""
    estimated_tokens: int = 0
    threshold_tokens: int = 0
    context_length: int = 0

    def to_dict(self) -> dict:
        return {
            "should_compress": self.should_compress,
            "reason": self.reason,
            "estimated_tokens": self.estimated_tokens,
            "threshold_tokens": self.threshold_tokens,
            "context_length": self.context_length,
        }


@dataclass
class CompressResult:
    messages: list[dict] = field(default_factory=list)
    messages_before: int = 0
    messages_after: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    method: str = "none"
    summary: str = ""
    pruned_tool_results: int = 0

    def to_dict(self) -> dict:
        return {
            "messages_before": self.messages_before,
            "messages_after": self.messages_after,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "method": self.method,
            "pruned_tool_results": self.pruned_tool_results,
        }


class ContextEngine(ABC):
    """Decides when to compact and performs the compaction."""

    @abstractmethod
    def should_compress(
        self, messages: list[dict], anchor: Any = None, context_length: int = 0
    ) -> CompressionDecision:
        """Should the transcript be compacted before the next request?"""

    @abstractmethod
    def compress(self, messages: list[dict], focus: str | None = None) -> CompressResult:
        """Return a compacted transcript."""

    def update_model(self, context_length: int) -> None:
        """Called when the model (and therefore the context window) changes."""

"""Provider abstraction.

The harness speaks one internal message format (OpenAI-style) regardless of the
upstream API. Adapters translate at the boundary.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolSpec:
    """A model-facing tool definition."""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_anthropic(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ProviderRequest:
    messages: list[dict[str, Any]]
    tools: list[ToolSpec] = field(default_factory=list)
    model: str = ""
    max_tokens: int = 4096
    temperature: float = 0.0
    #: Optional per-request override of the provider's context window.
    context_length: int | None = None


@dataclass
class ProviderResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""
    reasoning: str | None = None
    raw: Any = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class Provider(ABC):
    """Base class for model providers."""

    name: str = "provider"

    @abstractmethod
    def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Run one completion. Raises :class:`~tsql.errors.ProviderError` on failure."""

    @property
    @abstractmethod
    def context_length(self) -> int:
        """Context window in tokens for the configured model."""

    def describe(self) -> dict:
        return {"provider": self.name, "context_length": self.context_length}


def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """Parse a provider's tool-call argument payload, tolerating malformed JSON.

    Providers occasionally emit truncated or non-JSON argument blobs. Returning an
    ``__invalid_arguments__`` marker lets the model see the problem and retry.
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"__invalid_arguments__": str(raw)[:2000]}
    if not isinstance(parsed, dict):
        return {"__invalid_arguments__": str(parsed)[:2000]}
    return parsed

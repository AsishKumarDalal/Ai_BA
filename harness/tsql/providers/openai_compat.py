"""OpenAI-compatible provider.

Covers OpenAI, OpenRouter, Groq, Together, Fireworks, DeepSeek, MiniMax, Nous
Portal, Ollama (``/v1``), vLLM, LM Studio and any other ``/chat/completions``
compatible gateway.
"""

from __future__ import annotations

from typing import Any

from ..config import ModelConfig, guess_context_length
from ..errors import ProviderError
from .base import Provider, ProviderRequest, ProviderResponse, ToolCall, Usage, parse_tool_arguments


class OpenAICompatProvider(Provider):
    name = "openai_compat"

    def __init__(self, config: ModelConfig, client: Any | None = None):
        self.config = config
        self._client = client
        self._context_length = config.context_length or guess_context_length(config.model)

    # ------------------------------------------------------------------ setup
    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise ProviderError(
                "the 'openai' package is required for OpenAI-compatible providers",
                code="missing_dependency",
            ) from exc
        kwargs: dict[str, Any] = {
            "api_key": self.config.api_key or "not-required",
            "timeout": self.config.timeout_seconds,
        }
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        if self.config.extra_headers:
            kwargs["default_headers"] = dict(self.config.extra_headers)
        self._client = OpenAI(**kwargs)
        return self._client

    @property
    def context_length(self) -> int:
        return self._context_length

    # ------------------------------------------------------------------- call
    def complete(self, request: ProviderRequest) -> ProviderResponse:
        client = self._ensure_client()
        payload: dict[str, Any] = {
            "model": request.model or self.config.model,
            "messages": request.messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.tools:
            payload["tools"] = [t.to_openai() for t in request.tools]

        try:
            response = client.chat.completions.create(**payload)
        except Exception as exc:  # noqa: BLE001 - normalized into ProviderError
            raise _normalize_error(exc) from exc

        return self._parse(response)

    # ----------------------------------------------------------------- parsing
    def _parse(self, response: Any) -> ProviderResponse:
        choices = getattr(response, "choices", None) or []
        if not choices:
            return ProviderResponse(
                text="",
                finish_reason="stop",
                usage=_parse_usage(getattr(response, "usage", None)),
                raw=response,
            )
        choice = choices[0]
        message = getattr(choice, "message", None)
        finish_reason = getattr(choice, "finish_reason", "") or "stop"

        if message is None:
            return ProviderResponse(finish_reason=finish_reason, raw=response)

        text = _extract_text(message)
        reasoning = _extract_reasoning(message)
        tool_calls: list[ToolCall] = []
        for raw_call in getattr(message, "tool_calls", None) or []:
            function = getattr(raw_call, "function", None)
            tool_calls.append(
                ToolCall(
                    id=getattr(raw_call, "id", "") or "",
                    name=getattr(function, "name", "") or "",
                    arguments=parse_tool_arguments(getattr(function, "arguments", None)),
                )
            )
        return ProviderResponse(
            text=text,
            tool_calls=tool_calls,
            usage=_parse_usage(getattr(response, "usage", None)),
            finish_reason=finish_reason,
            reasoning=reasoning,
            raw=response,
        )


def _extract_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in (None, "text") and part.get("text"):
                    parts.append(str(part["text"]))
            else:
                text = getattr(part, "text", None)
                if text:
                    parts.append(str(text))
        return "".join(parts)
    return "" or ""


def _extract_reasoning(message: Any) -> str | None:
    """Pull extended-thinking content out of the several shapes providers use."""
    for attribute in ("reasoning_content", "reasoning", "thinking"):
        value = getattr(message, attribute, None)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list) and value:
            chunks = []
            for part in value:
                if isinstance(part, dict):
                    chunks.append(str(part.get("text") or part.get("thinking") or ""))
                else:
                    chunks.append(str(getattr(part, "text", "") or ""))
            joined = "".join(chunks).strip()
            if joined:
                return joined
    return None


def _parse_usage(raw: Any) -> Usage:
    if raw is None:
        return Usage()
    details = getattr(raw, "prompt_details", None)
    cached = 0
    if details is not None:
        cached = int(getattr(details, "cached_tokens", 0) or 0)
    completion_details = getattr(raw, "completion_details", None)
    reasoning = 0
    if completion_details is not None:
        reasoning = int(getattr(completion_details, "reasoning_tokens", 0) or 0)
    return Usage(
        input_tokens=int(getattr(raw, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(raw, "completion_tokens", 0) or 0),
        cache_read_tokens=cached,
        reasoning_tokens=reasoning,
    )


def _normalize_error(exc: Exception) -> ProviderError:
    """Map a provider SDK exception onto a retryable/non-retryable ProviderError."""
    status = getattr(exc, "status_code", None)
    message = str(exc)
    lowered = message.lower()
    if status in (408, 409, 425, 429) or "rate limit" in lowered or "overloaded" in lowered:
        return ProviderError(message, code="rate_limited", retryable=True, status_code=status)
    if status is not None and 500 <= int(status) < 600:
        return ProviderError(message, code="server_error", retryable=True, status_code=status)
    if status in (401, 403):
        return ProviderError(message, code="auth_error", retryable=False, status_code=status)
    if "context length" in lowered or "maximum context" in lowered or "too many tokens" in lowered:
        return ProviderError(message, code="context_overflow", retryable=False, status_code=status)
    if isinstance(exc, (TimeoutError,)) or "timeout" in lowered:
        return ProviderError(message, code="timeout", retryable=True, status_code=status)
    return ProviderError(message, code="provider_error", retryable=False, status_code=status)

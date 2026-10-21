"""Native Anthropic Messages API provider.

Implemented on the standard library (``urllib.request``) so the harness does not
depend on the ``anthropic`` SDK.

Translates the harness's OpenAI-style internal message format to and from the
Anthropic Messages API, including ``tool_use`` / ``tool_result`` blocks and
prompt-caching breakpoints.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..config import ModelConfig, guess_context_length
from ..errors import ProviderError
from .base import Provider, ProviderRequest, ProviderResponse, ToolCall, Usage

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"

#: Anthropic allows at most 4 cache breakpoints per request.
MAX_CACHE_BREAKPOINTS = 4


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, config: ModelConfig):
        self.config = config
        self.base_url = (config.base_url or DEFAULT_BASE_URL).rstrip("/")
        self._context_length = config.context_length or guess_context_length(config.model)

    @property
    def context_length(self) -> int:
        return self._context_length

    # ------------------------------------------------------------------- call
    def complete(self, request: ProviderRequest) -> ProviderResponse:
        system_blocks, messages = convert_messages(request.messages)
        payload: dict[str, Any] = {
            "model": request.model or self.config.model,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if system_blocks:
            payload["system"] = system_blocks
        if request.tools:
            payload["tools"] = [t.to_anthropic() for t in request.tools]

        response = self._post(payload)
        return self._parse(response)

    def _post(self, payload: dict[str, Any]) -> dict:
        if not self.config.api_key:
            raise ProviderError(
                "ANTHROPIC_API_KEY (or model.api_key) is required for the Anthropic provider",
                code="missing_credentials",
            )
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "x-api-key": self.config.api_key,
                "anthropic-version": API_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise _normalize_http_error(exc.code, detail) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"network error contacting Anthropic: {exc}",
                                code="network_error", retryable=True) from exc
        except TimeoutError as exc:
            raise ProviderError("request to Anthropic timed out",
                                code="timeout", retryable=True) from exc

    # ----------------------------------------------------------------- parsing
    def _parse(self, raw: dict) -> ProviderResponse:
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in raw.get("content") or []:
            kind = block.get("type")
            if kind == "text":
                text_parts.append(block.get("text") or "")
            elif kind == "thinking":
                thinking_parts.append(block.get("thinking") or "")
            elif kind == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.get("id") or "",
                        name=block.get("name") or "",
                        arguments=block.get("input") if isinstance(block.get("input"), dict) else {},
                    )
                )
        usage_raw = raw.get("usage") or {}
        usage = Usage(
            input_tokens=int(usage_raw.get("input_tokens", 0) or 0),
            output_tokens=int(usage_raw.get("output_tokens", 0) or 0),
            cache_read_tokens=int(usage_raw.get("cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(usage_raw.get("cache_creation_input_tokens", 0) or 0),
        )
        return ProviderResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=raw.get("stop_reason") or "stop",
            reasoning="".join(thinking_parts) or None,
            raw=raw,
        )


def convert_messages(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Convert OpenAI-style messages into (system_blocks, anthropic_messages)."""
    system_blocks: list[dict] = []
    converted: list[dict] = []

    for message in messages:
        role = message.get("role")
        if role == "system":
            content = message.get("content")
            if isinstance(content, str):
                if content:
                    system_blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        system_blocks.append({"type": "text", "text": part.get("text") or ""})
            continue

        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id") or "",
                "content": _stringify(message.get("content")),
            }
            # Anthropic requires tool_result blocks to live in a user turn.
            if converted and converted[-1]["role"] == "user" and _is_tool_result_turn(converted[-1]):
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
            continue

        if role == "assistant":
            blocks: list[dict] = []
            content = message.get("content")
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        blocks.append({"type": "text", "text": part.get("text") or ""})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id") or "",
                        "name": function.get("name") or "",
                        "input": arguments if isinstance(arguments, dict) else {},
                    }
                )
            if blocks:
                converted.append({"role": "assistant", "content": blocks})
            continue

        # user / anything else
        content = message.get("content")
        if isinstance(content, str):
            converted.append({"role": "user", "content": [{"type": "text", "text": content}]})
        elif isinstance(content, list):
            parts = [
                {"type": "text", "text": p.get("text") or ""}
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            if parts:
                converted.append({"role": "user", "content": parts})
        else:
            converted.append({"role": "user", "content": [{"type": "text", "text": ""}]})

    _merge_adjacent_user_turns(converted)
    return system_blocks, converted


def _is_tool_result_turn(turn: dict) -> bool:
    content = turn.get("content")
    return bool(content) and all(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )


def _merge_adjacent_user_turns(messages: list[dict]) -> None:
    merged: list[dict] = []
    for message in messages:
        if (
            merged
            and merged[-1]["role"] == "user"
            and message["role"] == "user"
            and not _is_tool_result_turn(merged[-1])
            and not _is_tool_result_turn(message)
        ):
            merged[-1]["content"].extend(message["content"])
            continue
        merged.append(message)
    messages[:] = merged


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def _normalize_http_error(status: int, detail: str) -> ProviderError:
    try:
        parsed = json.loads(detail)
        message = parsed.get("error", {}).get("message") or detail
    except (json.JSONDecodeError, AttributeError):
        message = detail
    lowered = str(message).lower()
    if status == 429:
        return ProviderError(str(message), code="rate_limited", retryable=True, status_code=status)
    if status in (408, 409, 425, 529):
        return ProviderError(str(message), code="server_error", retryable=True, status_code=status)
    if status in (401, 403):
        return ProviderError(str(message), code="auth_error", retryable=False, status_code=status)
    if "prompt is too long" in lowered or "context" in lowered and "length" in lowered:
        return ProviderError(str(message), code="context_overflow", retryable=False, status_code=status)
    return ProviderError(str(message), code="provider_error", retryable=False, status_code=status)

"""Message-list manipulation.

Providers validate role sequences and reject malformed histories, so the
harness normalizes before every request:

* ``system`` appears once, at position 0.
* ``user`` and ``assistant`` alternate.
* ``tool`` messages may repeat (parallel tool results).
* Every assistant ``tool_calls`` entry has a matching tool result.

The rules are repaired rather than enforced destructively: a violation is
merged, not dropped, so no information is lost.
"""

from __future__ import annotations

from typing import Any

from ..providers.base import ProviderResponse


def enforce_alternation(messages: list[dict]) -> list[dict]:
    """Return a copy of ``messages`` with a legal role sequence."""
    out: list[dict] = []
    for message in messages:
        role = message.get("role")

        if role == "system":
            if not out:
                out.append(dict(message))
            else:
                # Fold a late system message into the head system message.
                head = out[0]
                head["content"] = f"{head.get('content') or ''}\n\n{message.get('content') or ''}"
            continue

        if not out or out[-1].get("role") == "system":
            if role != "user":
                out.append({"role": "user", "content": ""})
            out.append(dict(message))
            continue

        last = out[-1]
        if last.get("role") == role and role in ("user", "assistant"):
            if role == "user":
                last["content"] = f"{last.get('content') or ''}\n\n{message.get('content') or ''}"
            else:
                last["content"] = f"{last.get('content') or ''}{message.get('content') or ''}"
                if message.get("tool_calls"):
                    last["tool_calls"] = (last.get("tool_calls") or []) + message["tool_calls"]
                if message.get("reasoning"):
                    last["reasoning"] = message["reasoning"]
            continue

        # Tool results may repeat (parallel calls) and must all be kept.
        out.append(dict(message))
    return out


def sanitize_tool_pairs(messages: list[dict]) -> list[dict]:
    """Drop orphan tool results and stub assistant tool calls with no result."""
    kept: list[dict] = []
    answered: set[str] = set()
    for message in messages:
        role = message.get("role")
        if role == "tool":
            call_id = message.get("tool_call_id") or ""
            kept.append(dict(message))
            answered.add(call_id)
            continue
        kept.append(dict(message))

    # An assistant tool call whose result vanished (removed by compression)
    # must still be answered, or the provider rejects the turn.
    out: list[dict] = []
    for message in kept:
        out.append(message)
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        for call in message["tool_calls"]:
            call_id = call.get("id") or ""
            if call_id and call_id not in answered:
                out.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": '{"status": "unavailable", "note": "tool result was not retained"}',
                })
    return out


def prepare_api_messages(messages: list[dict]) -> list[dict]:
    """Full normalization pass: alternate roles, then pair tool calls."""
    return sanitize_tool_pairs(enforce_alternation(messages))


def assistant_message_from_response(response: ProviderResponse) -> dict:
    """Build the assistant message that goes into the transcript."""
    message: dict[str, Any] = {"role": "assistant", "content": response.text or ""}
    if response.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": _dump_arguments(call.arguments)},
            }
            for call in response.tool_calls
        ]
    if response.reasoning:
        message["reasoning"] = response.reasoning
    if response.finish_reason:
        message["finish_reason"] = response.finish_reason
    return message


def tool_message(tool_call_id: str, name: str, content: str) -> dict:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "tool_name": name,
        "content": content,
    }


def _dump_arguments(arguments: Any) -> str:
    import json

    if isinstance(arguments, str):
        return arguments
    try:
        return json.dumps(arguments, default=str)
    except (TypeError, ValueError):
        return "{}"


def find_system_prompt(messages: list[dict]) -> str:
    for message in messages:
        if message.get("role") == "system":
            return message.get("content") or ""
    return ""

"""Small shared helpers: id minting, token estimation, JSON canonicalization."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any

# --------------------------------------------------------------------------- ids


def new_id(prefix: str, width: int = 12) -> str:
    """Mint a short, sortable-ish, human-quotable id such as ``tbl_9f3a1c02b7de``."""
    return f"{prefix}_{uuid.uuid4().hex[:width]}"


def new_task_id() -> str:
    return uuid.uuid4().hex


def now() -> float:
    return time.time()


# ------------------------------------------------------------------ token counts

# Rough token estimate. Used only where the provider did not report real usage.
# 4 characters per token is the standard heuristic for English + SQL.
_CHARS_PER_TOKEN = 4.0


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


def estimate_message_tokens(message: dict) -> int:
    """Estimate the token cost of one OpenAI-format message."""
    total = estimate_tokens(message.get("content"))
    if isinstance(message.get("content"), list):
        total = 0
        for part in message["content"]:
            if isinstance(part, dict):
                total += estimate_tokens(part.get("text"))
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        total += estimate_tokens(fn.get("name"))
        total += estimate_tokens(fn.get("arguments"))
    if message.get("reasoning"):
        total += estimate_tokens(message["reasoning"])
    return total


def estimate_messages_tokens(messages: list[dict]) -> int:
    return sum(estimate_message_tokens(m) for m in messages)


# -------------------------------------------------------------------- json / hash


def canonical_json(value: Any) -> str:
    """Stable JSON serialization used for hashing and fingerprinting."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(message: dict) -> str:
    """Content fingerprint of a message, used by the usage anchor to detect drift."""
    return sha256_hex(canonical_json(message))[:16]


# ----------------------------------------------------------------------- strings

_WHITESPACE = re.compile(r"\s+")


def collapse_whitespace(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix


def truncate_cell(value: Any, limit: int = 512) -> Any:
    """Truncate a single cell value without breaking its type."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    if isinstance(value, (bytes, bytearray)) and len(value) > limit:
        return f"<{limit} bytes elided>"
    return value

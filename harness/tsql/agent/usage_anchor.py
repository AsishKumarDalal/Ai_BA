"""Usage anchor — trustworthy token accounting.

Provider-reported tokens are the only number worth trusting. The anchor records
the last provider-reported prompt/completion counts plus a *fingerprint* of the
message they were measured against.

Fingerprinting (rather than counting positions) matters because the gateway
pattern reloads the transcript from SQLite on every turn, so positional
bookkeeping would drift. A content fingerprint survives the reload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..util import estimate_message_tokens, fingerprint


@dataclass
class UsageAnchor:
    """Provider-reported usage for a known point in the transcript."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    #: Fingerprint of the last message included in the provider's count.
    last_fingerprint: str = ""
    #: Estimated tokens appended since the provider's count.
    delta_estimate: int = 0
    context_length: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ update
    def update(self, usage: Any, messages: list[dict], context_length: int = 0) -> None:
        """Re-anchor on a fresh provider usage report."""
        self.prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        self.completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        self.cache_read_tokens = int(getattr(usage, "cache_read_tokens", 0) or 0)
        self.reasoning_tokens = int(getattr(usage, "reasoning_tokens", 0) or 0)
        self.context_length = context_length or self.context_length
        self.delta_estimate = 0
        self.last_fingerprint = fingerprint(messages[-1]) if messages else ""

    # ----------------------------------------------------------------- queries
    @property
    def has_anchor(self) -> bool:
        return self.prompt_tokens > 0 or self.completion_tokens > 0

    def estimate_current(self, messages: list[dict]) -> int:
        """Best available estimate of the current prompt size.

        Anchored: provider count + a rough estimate of only the messages
        appended since. Unanchored: a whole-context rough estimate.
        """
        if not self.has_anchor:
            return estimate_message_tokens_all(messages)
        appended = count_appended(messages, self.last_fingerprint)
        return self.prompt_tokens + appended

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "last_fingerprint": self.last_fingerprint,
            "delta_estimate": self.delta_estimate,
            "context_length": self.context_length,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "UsageAnchor":
        if not data:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def count_appended(messages: list[dict], last_fingerprint: str) -> int:
    """Estimate the tokens of messages appended after the anchored message."""
    if not messages:
        return 0
    if not last_fingerprint:
        return estimate_message_tokens_all(messages)
    for index in range(len(messages) - 1, -1, -1):
        if fingerprint(messages[index]) == last_fingerprint:
            return estimate_message_tokens_all(messages[index + 1:])
    # The anchored message is gone (compressed or replaced): fall back to the
    # whole transcript, which over-estimates rather than under-estimates.
    return estimate_message_tokens_all(messages)


def estimate_message_tokens_all(messages: list[dict]) -> int:
    return sum(estimate_message_tokens(m) for m in messages)

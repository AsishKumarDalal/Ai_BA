"""The context compressor.

A "build me a sales report" request means 40 tool calls, several thousand
rows of results, and a lot of chart specs. A naive ReAct loop dies on the
context window. Four phases, in order:

1. **Prune old tool results** (no LLM call). A big query result is replaced
   by a stub that keeps the *handle* and the *shape*::

       [tool result cleared - result_ref=tbl_a91f rows=5000 cols=[region,revenue]]

   The model can still chart that result without re-querying it. That single
   detail is the difference between a session that survives a twelve-panel
   report and one that does not.

2. **Determine boundaries.** The head is protected, the tail is protected by a
   token budget, and the middle is what gets summarized. Boundaries are
   aligned so an assistant's tool calls and their results are never split.

3. **Generate a structured summary** with one auxiliary LLM call. On
   re-compression the previous summary is passed in and *updated*, so
   information survives repeated compressions.

4. **Assemble.** Head + summary + tail, with orphaned tool pairs repaired.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from ..config import CompressionConfig
from ..util import estimate_message_tokens, estimate_messages_tokens
from .context_engine import CompressResult, CompressionDecision, ContextEngine

SUMMARY_PREFIX = "[CONTEXT COMPACTION]"

SUMMARY_TEMPLATE = """\
## Goal
## Constraints & Preferences
## Progress
### Done
### In Progress
### Blocked
## Key Decisions
## Artifacts
## Next Steps
## Critical Context"""

SUMMARIZER_PROMPT = """\
Summarize the conversation so far so another agent can continue without seeing \
it. Another agent will pick up the work, so keep every identifier exactly as \
it appears: table names, column names, value names, ids, file paths and error \
strings. Never paraphrase an identifier.

Also list the artifacts that were produced, with their ids, so the next agent \
can still reach them:
- tbl_xxxx  a query result (what it answers, row count, column names)
- cht_xxxx  a chart
- dash_xxxx a dashboard

Use exactly this structure:

## Goal
## Constraints & Preferences
## Progress
### Done
### In Progress
### Blocked
## Key Decisions
## Artifacts
## Next Steps
## Critical Context
"""

#: A summarizer takes the messages to summarize and returns the summary text.
Summarizer = Callable[[list[dict], str | None], str]


class ContextCompressor(ContextEngine):
    """The built-in lossy compressor."""

    def __init__(
        self,
        config: CompressionConfig | None = None,
        summarizer: Summarizer | None = None,
        model: str = "",
        context_length: int = 0,
    ):
        self.config = config or CompressionConfig()
        self._summarizer = summarizer
        self._model = model
        self._context_length = context_length
        self._previous_summary = ""

    # ------------------------------------------------------------------ setup
    def update_model(self, context_length: int) -> None:
        self._context_length = context_length

    def set_summarizer(self, summarizer: Summarizer | None) -> None:
        self._summarizer = summarizer

    # ---------------------------------------------------------------- decision
    def should_compress(
        self, messages: list[dict], anchor: Any = None, context_length: int = 0
    ) -> CompressionDecision:
        if not self.config.enabled:
            return CompressionDecision(reason="compression disabled")
        if len(messages) < 6:
            return CompressionDecision(reason="transcript is still short")

        context_length = context_length or self._context_length
        if context_length <= 0:
            return CompressionDecision(reason="no context window known")

        threshold = self.config.threshold_for(self._model)
        threshold_tokens = int(context_length * threshold)
        if anchor is not None and hasattr(anchor, "estimate_current"):
            estimated = anchor.estimate_current(messages)
        else:
            estimated = estimate_messages_tokens(messages)

        should = estimated >= threshold_tokens
        return CompressionDecision(
            should_compress=should,
            reason=(
                f"estimated {estimated:,} tokens against a {threshold_tokens:,} threshold "
                f"({threshold:.0%} of {context_length:,})"
            ),
            estimated_tokens=estimated,
            threshold_tokens=threshold_tokens,
            context_length=context_length,
        )

    # -------------------------------------------------------------- compression
    def compress(self, messages: list[dict], focus: str | None = None) -> CompressResult:
        before_tokens = estimate_messages_tokens(messages)
        before_count = len(messages)

        head_end, tail_start = self._boundaries(messages)
        head = messages[:head_end]
        tail = messages[tail_start:]

        # Phase 1 — prune the tool results in the summarized middle. The
        # artifacts they produced are extracted first, because the summary is
        # the only place those handles survive.
        middle: list[dict] = []
        artifacts: list[str] = []
        errors: list[str] = []
        pruned_count = 0
        for message in messages[head_end:tail_start]:
            if message.get("role") == "tool":
                record = self._artifact_record(message.get("content") or "")
                if record:
                    artifacts.append(record)
                error = self._error_record(message.get("content") or "")
                if error:
                    errors.append(error)
                stub = self._stub_tool_result(message.get("content") or "")
                if stub != message.get("content"):
                    pruned_count += 1
                middle.append({**message, "content": stub})
            else:
                middle.append(message)

        # The tail is kept, but only the most recent tool results are kept
        # verbatim. Older ones become stubs that still carry their handle, so
        # the model can chart a result it ran twenty turns ago.
        tail = self._prune_tail(tail, pruned_count)

        # Phase 3 — the summary.
        summary, method = self._summarize(middle, focus, artifacts, messages, errors)

        # Phase 4 — assemble.
        # The summary's role is chosen so the alternation stays legal: it must
        # differ from the message that follows it.
        summary_role = "assistant" if tail and tail[0].get("role") == "user" else "user"
        summary_message = {
            "role": summary_role,
            "content": f"{SUMMARY_PREFIX} Earlier turns were compacted.\n\n{summary}",
        }
        assembled = head + [summary_message] + tail
        from .messages import prepare_api_messages  # local import avoids a cycle

        assembled = prepare_api_messages(assembled)

        after_tokens = estimate_messages_tokens(assembled)
        return CompressResult(
            messages=assembled,
            messages_before=before_count,
            messages_after=len(assembled),
            tokens_before=before_tokens,
            tokens_after=after_tokens,
            method=method,
            summary=summary,
            pruned_tool_results=pruned_count,
        )

    # -------------------------------------------------------------- boundaries
    def _boundaries(self, messages: list[dict]) -> tuple[int, int]:
        """Return ``(head_end, tail_start)``.

        The head is always protected. The tail is protected by a token budget
        *and* by a minimum message count, and always contains the last real
        user message. Both boundaries are then aligned so an assistant's tool
        calls stay with their results.
        """
        protect_first = max(1, self.config.protect_first_n)
        head_end = min(protect_first, len(messages))
        head_end = self._align_head(messages, head_end)

        budget = self._tail_budget()
        tail: list[dict] = []
        tokens = 0
        tail_start = len(messages)

        for index in range(len(messages) - 1, head_end - 1, -1):
            cost = estimate_message_tokens(messages[index])
            if tail and tokens + cost > budget:
                break
            tail.insert(0, messages[index])
            tokens += cost
            tail_start = index
            if len(tail) >= self.config.protect_last_n and self._has_user_message(tail):
                break

        # Guarantee the last real user message survives verbatim.
        if not self._has_user_message(tail):
            for index in range(tail_start - 1, head_end - 1, -1):
                if messages[index].get("role") == "user":
                    tail.insert(0, messages[index])
                    tail_start = index
                    break

        tail_start = self._align_tail(messages, head_end, tail_start)
        return head_end, tail_start

    @staticmethod
    def _align_head(messages: list[dict], head_end: int) -> int:
        """Extend the head so an assistant's tool calls keep their results.

        Without this, the head could end on an assistant whose tool results
        fall in the summarized middle — leaving an unanswered tool call.
        """
        changed = True
        while changed and head_end < len(messages):
            changed = False
            previous = messages[head_end - 1]
            if previous.get("role") == "assistant" and previous.get("tool_calls"):
                index = head_end
                while index < len(messages) and messages[index].get("role") == "tool":
                    index += 1
                if index > head_end:
                    head_end = index
                    changed = True
        return head_end

    @staticmethod
    def _align_tail(messages: list[dict], head_end: int, tail_start: int) -> int:
        """Move the boundary back so a tool result in the tail keeps its assistant.

        A tool result whose owning assistant fell in the summarized middle
        would be an orphan, and providers reject a transcript with one.
        """
        for _ in range(8):
            if tail_start <= head_end:
                break
            message = messages[tail_start]
            if message.get("role") != "tool":
                break
            call_id = message.get("tool_call_id")
            owner = None
            for index in range(tail_start - 1, head_end - 1, -1):
                candidate = messages[index]
                if candidate.get("role") != "assistant":
                    continue
                if any(call.get("id") == call_id for call in (candidate.get("tool_calls") or [])):
                    owner = index
                    break
            if owner is None:
                break
            tail_start = owner
        return tail_start

    def _tail_budget(self) -> int:
        window = self._context_length or 0
        if window <= 0:
            return self.config.tail_min_tokens
        budget = int(window * self.config.tail_ratio)
        return max(self.config.tail_min_tokens, min(self.config.tail_max_tokens, budget))

    @staticmethod
    def _has_user_message(messages: list[dict]) -> bool:
        return any(m.get("role") == "user" for m in messages)

    @staticmethod
    def _align_boundary(messages: list[dict], head_end: int, tail_start: int) -> int:
        """Move the boundary backwards past an assistant that owns tool calls."""
        index = tail_start
        while index > head_end:
            message = messages[index]
            if message.get("role") == "tool":
                # Find the assistant that issued this call.
                index -= 1
                while index > head_end and messages[index].get("role") != "assistant":
                    index -= 1
                if index > head_end:
                    return index
                break
            index -= 1
        return tail_start

    # --------------------------------------------------------------- tool stub
    def _prune_tail(self, tail: list[dict], pruned_count: int) -> list[dict]:
        """Stub old tool results in the tail, keeping the most recent ones.

        The most recent tool results are what the model is still working with,
        so they stay verbatim. Older ones become stubs that keep their handle.
        """
        tool_indexes = [i for i, m in enumerate(tail) if m.get("role") == "tool"]
        keep = set(tool_indexes[-self.config.keep_recent_tool_results:])
        result: list[dict] = []
        for index, message in enumerate(tail):
            if index in keep or message.get("role") != "tool":
                result.append(message)
                continue
            stub = self._stub_tool_result(message.get("content") or "")
            if stub != message.get("content"):
                pruned_count += 1
            result.append({**message, "content": stub})
        return result

    def _stub_tool_result(self, content: str) -> str:
        """Replace a big tool result with a stub that keeps the handle."""
        if len(content) <= 200:
            return content
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return "[old tool output cleared to save context space]"
        if not isinstance(payload, dict):
            return "[old tool output cleared to save context space]"

        if payload.get("result_ref"):
            columns = payload.get("columns") or []
            names = ", ".join(
                str(c.get("name", "")) if isinstance(c, dict) else str(c) for c in columns
            )
            return (
                f"[tool result cleared - result_ref={payload['result_ref']} "
                f"rows={payload.get('row_count', '?')} cols=[{names}]. "
                "The full result is still available at that handle.]"
            )
        if payload.get("status") == "blocked":
            errors = "; ".join(payload.get("errors") or [])[:200]
            return f"[tool result cleared - the query was blocked: {errors}]"
        if payload.get("status") == "error":
            return f"[tool result cleared - error: {str(payload.get('error'))[:200]}]"
        return "[old tool output cleared to save context space]"

    # ---------------------------------------------------------------- summary
    def _error_record(self, content: str) -> str | None:
        """Capture a blocked or failed query's reason, before it is stubbed."""
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("status") in ("blocked", "error"):
            reason = "; ".join(payload.get("errors") or []) or str(payload.get("error") or "")
            if reason:
                return reason[:300]
        return None

    def _summarize(
        self,
        middle: list[dict],
        focus: str | None,
        artifacts: list[str] | None = None,
        whole: list[dict] | None = None,
        errors: list[str] | None = None,
    ) -> tuple[str, str]:
        if not middle:
            return "(nothing to summarize)", "none"
        if self._summarizer is not None:
            try:
                summary = self._summarizer(middle, focus)
                if summary and summary.strip():
                    self._previous_summary = summary
                    return summary, "llm_summary"
            except Exception:  # noqa: BLE001 - fall through to the deterministic summary
                pass
        summary = self._deterministic_summary(middle, artifacts or [], whole or [], errors or [])
        self._previous_summary = summary
        return summary, "deterministic_fallback"

    def _artifact_record(self, content: str) -> str | None:
        """Describe a tool result compactly, for the summary's artifact list."""
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        if not payload.get("result_ref"):
            return None
        columns = payload.get("columns") or []
        names = ", ".join(
            str(c.get("name", "")) if isinstance(c, dict) else str(c) for c in columns
        )
        purpose = payload.get("purpose") or ""
        rows = payload.get("row_count", "?")
        return f"- {payload['result_ref']}  {purpose}  ({rows} rows, cols: {names})".strip()

    def _deterministic_summary(
        self,
        middle: list[dict],
        artifacts: list[str],
        whole: list[dict],
        errors: list[str] | None = None,
    ) -> str:
        """A summary that needs no model call.

        It is lossy but it is never wrong: it quotes the user's goals and
        lists the artifacts and tool calls that were made.
        """
        goals: list[str] = []
        tool_counts: dict[str, int] = {}
        last_error = ""

        # The first request is the goal, even when it sits in the protected
        # head — the summary is the only thing that survives into the next
        # compression round.
        for message in whole:
            if message.get("role") == "user" and (message.get("content") or "").strip():
                goals.append(_one_line(message["content"]))
                break

        for message in middle:
            role = message.get("role")
            content = message.get("content") or ""
            if role == "user" and content:
                goals.append(_one_line(content))
            if role == "tool":
                name = message.get("tool_name") or "tool"
                tool_counts[name] = tool_counts.get(name, 0) + 1

        if errors:
            last_error = errors[-1]

        lines = ["## Goal"]
        if goals:
            lines.extend(f"- {g}" for g in goals[-5:])
        else:
            lines.append("- (not recorded)")

        lines.append("\n## Artifacts")
        if artifacts:
            lines.extend(dict.fromkeys(artifacts))
        else:
            lines.append("- (none)")

        lines.append("\n## Progress")
        for name, count in sorted(tool_counts.items()):
            lines.append(f"- {name}: {count} call(s)")

        if last_error:
            lines.append("\n## Critical Context")
            lines.append(f"- Last error: {last_error}")

        if self._previous_summary:
            lines.append("\n## Earlier summary")
            lines.append(self._previous_summary)

        return "\n".join(lines)


def _one_line(text: str, limit: int = 200) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1] + "…"
    return collapsed


class ProviderSummarizer:
    """Summarizes a transcript segment with the model provider.

    Kept separate from the compressor so a failed summary never breaks a turn:
    the compressor catches the exception and falls back to the deterministic
    summary.
    """

    def __init__(
        self,
        provider: Any,
        model: str = "",
        max_tokens: int = 4_000,
        temperature: float = 0.0,
    ):
        self._provider = provider
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature

    def __call__(self, messages: list[dict], focus: str | None = None) -> str:
        from ..providers.base import ProviderRequest
        from .messages import prepare_api_messages

        prompt = SUMMARIZER_PROMPT
        if focus:
            prompt += f"\n\nPay particular attention to: {focus}\n"

        conversation = [{"role": "system", "content": prompt}]
        conversation.extend(prepare_api_messages(messages))

        response = self._provider.complete(
            ProviderRequest(
                messages=conversation,
                tools=[],
                model=self._model,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )
        )
        return response.text or ""

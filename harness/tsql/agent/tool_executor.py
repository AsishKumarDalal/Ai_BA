"""Tool execution.

Responsibilities:

* Route a tool call to the registry, or to an agent-level interceptor for tools
  that need per-session agent state (``todo_list``, ``memory``).
* Execute independent calls concurrently while preserving result order.
* Emit observable events around every call.
* Guarantee the model always receives a well-formed JSON string.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from ..providers.base import ToolCall
from ..tools.registry import ToolRegistry, registry as default_registry
from ..util import truncate
from .events import EventEmitter, EventType


@dataclass
class ToolContext:
    """Per-request state handed to tool handlers via ``**kwargs``."""

    task_id: str
    session_id: str | None = None
    state: Any = None        # tsql.state.SessionDB
    db: Any = None           # tsql.db.connection.Database
    policy: Any = None       # tsql.safety.policy.Policy
    approval: Any = None     # tsql.tools.approval.ApprovalManager
    agent: Any = None        # the Agent itself, for intercepted tools
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False
    elapsed_ms: int = 0

    def to_message(self) -> dict:
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "tool_name": self.name,
            "content": self.content,
        }


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry | None = None,
        on_event: Callable[[Any], None] | None = None,
        parallel: bool = True,
        max_workers: int = 4,
        intercepted: dict[str, Callable[[dict, ToolContext], str]] | None = None,
    ):
        self._registry = registry or default_registry
        self._emitter = EventEmitter([on_event] if on_event else [])
        self._parallel = parallel
        self._max_workers = max(1, max_workers)
        self._intercepted = dict(intercepted or {})

    def register_interceptor(
        self, name: str, handler: Callable[[dict, ToolContext], str]
    ) -> None:
        self._intercepted[name] = handler

    # ------------------------------------------------------------------ batch
    def execute(self, tool_calls: list[ToolCall], ctx: ToolContext) -> list[ToolResult]:
        """Execute a batch of tool calls and return results in the original order.

        Read-only tools run concurrently. A tool that mutates shared state
        (``add_panel`` writing to the session's dashboard) runs on its own, in
        the order the model asked for it, so two copies of it cannot race.
        """
        if not tool_calls:
            return []
        if len(tool_calls) == 1 or not self._parallel:
            return [self.execute_one(call, ctx) for call in tool_calls]

        concurrent: list[tuple[int, ToolCall]] = []
        serial: list[tuple[int, ToolCall]] = []
        for index, call in enumerate(tool_calls):
            if self._is_parallel_safe(call):
                concurrent.append((index, call))
            else:
                serial.append((index, call))

        results: dict[int, ToolResult] = {}
        pool: ThreadPoolExecutor | None = None
        futures: dict[int, Any] = {}
        if concurrent:
            pool = ThreadPoolExecutor(
                max_workers=min(len(concurrent), self._max_workers)
            )
            futures = {
                index: pool.submit(self.execute_one, call, ctx)
                for index, call in concurrent
            }
        try:
            # The mutating calls run here, while the read-only ones work.
            for index, call in serial:
                results[index] = self.execute_one(call, ctx)
            for index, future in futures.items():
                results[index] = future.result()
        finally:
            if pool is not None:
                pool.shutdown(wait=True)

        return [results[index] for index in sorted(results)]

    def _is_parallel_safe(self, call: ToolCall) -> bool:
        """A call is safe to run alongside others unless its tool says otherwise."""
        if call.name in self._intercepted:
            # Intercepted tools keep per-session state on the agent.
            return False
        entry = self._registry.get(call.name)
        if entry is None:
            return True
        return entry.parallel_safe

    # ------------------------------------------------------------------ single
    def execute_one(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        started = time.monotonic()
        self._emitter.emit(
            EventType.TOOL_START,
            tool_call_id=call.id,
            name=call.name,
            args=_preview_args(call.arguments),
        )
        try:
            content, is_error = self._invoke(call, ctx)
        except Exception as exc:  # noqa: BLE001 - never let a tool kill the turn
            content = json.dumps({
                "error": f"Tool execution failed: {type(exc).__name__}: {exc}",
                "tool": call.name,
            })
            is_error = True
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._emitter.emit(
            EventType.TOOL_ERROR if is_error else EventType.TOOL_END,
            tool_call_id=call.id,
            name=call.name,
            elapsed_ms=elapsed_ms,
            is_error=is_error,
            preview=truncate(content, 400),
        )
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            content=content,
            is_error=is_error,
            elapsed_ms=elapsed_ms,
        )

    def _invoke(self, call: ToolCall, ctx: ToolContext) -> tuple[str, bool]:
        interceptor = self._intercepted.get(call.name)
        if interceptor is not None:
            raw = interceptor(call.arguments, ctx)
            return _as_json_string(raw), _is_error(raw)

        if self._registry.get(call.name) is None:
            return json.dumps({
                "error": f"unknown tool: {call.name}",
                "available_tools": self._registry.names(),
            }), True

        raw = self._registry.dispatch(call.name, call.arguments, ctx=ctx, task_id=ctx.task_id)
        return raw, _is_error(raw)


def _as_json_string(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, default=str)
    except (TypeError, ValueError):
        return json.dumps({"error": "tool returned a non-serializable value"})


def _is_error(raw: Any) -> bool:
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return False
        return isinstance(parsed, dict) and "error" in parsed
    return isinstance(raw, dict) and "error" in raw


def _preview_args(arguments: dict) -> dict:
    """Truncate long argument values so events stay small and log-safe."""
    preview: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if isinstance(value, str) and len(value) > 300:
            preview[key] = truncate(value, 300)
        else:
            preview[key] = value
    return preview

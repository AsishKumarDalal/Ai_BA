"""Typed events emitted by the agent loop.

One callback, one shape, no platform branching inside the loop. The CLI renders
them; the Phase 2 server streams them as SSE; tests assert on them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


class EventType:
    AGENT_START = "agent_start"
    AGENT_END = "agent_end"
    ASSISTANT_TEXT = "assistant_text"
    REASONING = "reasoning"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    TOOL_ERROR = "tool_error"
    NUDGE = "nudge"
    CONTEXT_COMPRESSION = "context_compression"
    ARTIFACT_CREATED = "artifact_created"
    APPROVAL_REQUIRED = "approval_required"
    ERROR = "error"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass
class AgentEvent:
    type: str
    ts: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"type": self.type, "ts": self.ts, "data": self.data}


class EventEmitter:
    """Fan-out to zero or more listeners. A failing listener never breaks the loop."""

    def __init__(self, listeners: list[Callable[[AgentEvent], None]] | None = None):
        self._listeners: list[Callable[[AgentEvent], None]] = list(listeners or [])

    def add_listener(self, listener: Callable[[AgentEvent], None]) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[AgentEvent], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def emit(self, event_type: str, **data: Any) -> AgentEvent:
        event = AgentEvent(type=event_type, data=data)
        self.dispatch(event)
        return event

    def dispatch(self, event: AgentEvent) -> None:
        """Deliver an already-built event to every listener.

        Subsystems that emit events of their own (the tool executor) receive
        ``dispatch`` rather than ``emit``, so their events are not re-wrapped.
        """
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - a bad listener must not kill the loop
                continue

"""The agent runtime: loop, events, budget, prompt assembly, context engine."""

from .budget import IterationBudget
from .events import AgentEvent, EventEmitter, EventType
from .loop import Agent, AgentResult
from .messages import (
    enforce_alternation,
    prepare_api_messages,
    sanitize_tool_pairs,
)
from .prompt_builder import PromptBuilder, PromptContext
from .tool_executor import ToolContext, ToolExecutor, ToolResult
from .usage_anchor import UsageAnchor

__all__ = [
    "Agent",
    "AgentEvent",
    "AgentResult",
    "EventEmitter",
    "EventType",
    "IterationBudget",
    "PromptBuilder",
    "PromptContext",
    "ToolContext",
    "ToolExecutor",
    "ToolResult",
    "UsageAnchor",
    "enforce_alternation",
    "prepare_api_messages",
    "sanitize_tool_pairs",
]

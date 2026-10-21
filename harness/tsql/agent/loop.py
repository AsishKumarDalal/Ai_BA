"""The agentic loop.

    prompt → model → tool calls → execute → feed back → model → … → final answer

Design constraints inherited from Hermes, and why they are here:

* **One internal message format.** Providers differ; the loop does not.
* **Strict role alternation, repaired not rejected.** A malformed history is
  merged into a legal one before the request, so a provider never 400s on a
  sequence the harness itself created.
* **Interruptible calls.** The HTTP request runs on a worker thread while the
  main thread watches for cancellation, so a user can abort mid-flight and the
  abandoned response is never injected into the transcript.
* **A per-request iteration budget.** Runaway loops stop.
* **Observable execution.** Every tool call is visible through the event stream.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Config
from ..errors import BudgetExhausted, ProviderError
from ..providers.base import Provider, ProviderRequest, Usage
from ..tools.registry import ToolRegistry, registry as default_registry
from ..tools.toolsets import get_tool_definitions
from ..util import new_task_id
from .budget import IterationBudget
from .events import EventEmitter, EventType
from .messages import assistant_message_from_response, prepare_api_messages, tool_message
from .prompt_builder import PromptBuilder, PromptContext
from .tool_executor import ToolContext, ToolExecutor
from .usage_anchor import UsageAnchor


@dataclass
class AgentResult:
    """The outcome of one conversational turn."""

    final_response: str = ""
    messages: list[dict] = field(default_factory=list)
    iterations: int = 0
    usage: Usage = field(default_factory=Usage)
    tool_calls: list[dict] = field(default_factory=list)
    stop_reason: str = "completed"
    task_id: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "final_response": self.final_response,
            "iterations": self.iterations,
            "usage": self.usage.to_dict(),
            "tool_calls": self.tool_calls,
            "stop_reason": self.stop_reason,
            "task_id": self.task_id,
            "error": self.error,
        }


#: An assistant message that opens with one of these reads as a promise to act
#: ("Let me take a look…") rather than an answer. If the model returned it with
#: no tool calls, the turn stalled: it described the work instead of doing it.
#: "Let me know" is an offer to the user, not a promise, and is excluded.
_PROMISE_RE = re.compile(
    r"^(?:#{1,6}\s*)?(?:[-*]\s*)?"
    r"(?:"
    r"let me(?!\s+know\b)"
    r"|i'?ll\b"
    r"|i will\b"
    r"|i'?m going to\b"
    r"|i am going to\b"
    r"|let'?s\b"
    r"|first,? (?:i'?ll|let me)"
    r"|now,? (?:i'?ll|let me)"
    r"|next,? (?:i'?ll|let me)"
    r"|to (?:do|answer) (?:that|this),? (?:i'?ll|let me)"
    r")",
    re.IGNORECASE,
)

#: The correction sent back when the model stalls. It has to be blunt: the
#: failure mode is a polite sentence that sounds like progress.
_NUDGE = (
    "You stopped without calling a tool or answering the question. Do not say "
    "what you are about to do — do it. Call the tool now, then give the answer."
)


class Agent:
    """The analyst agent."""

    def __init__(
        self,
        config: Config,
        provider: Provider,
        registry: ToolRegistry | None = None,
        state: Any = None,
        db: Any = None,
        policy: Any = None,
        approval: Any = None,
        on_event: Callable[[Any], None] | None = None,
        session_id: str | None = None,
        prompt_context: PromptContext | None = None,
        enabled_toolsets: list[str] | None = None,
        disabled_toolsets: list[str] | None = None,
        context_engine: Any = None,
        catalog: Any = None,
    ):
        self.config = config
        self.provider = provider
        self.registry = registry or default_registry
        self.state = state
        self.db = db
        self.policy = policy
        self.approval = approval
        self.session_id = session_id
        self.context_engine = context_engine
        self._catalog = catalog

        self.emitter = EventEmitter([on_event] if on_event else [])
        self._executor = ToolExecutor(
            registry=self.registry,
            on_event=self.emitter.dispatch,
            parallel=config.agent.parallel_tools,
            max_workers=config.agent.max_parallel_tools,
        )
        self._prompt_builder = PromptBuilder(config, config.agent.system_prompt_extra)
        self._prompt_context = prompt_context or PromptContext()
        self._enabled_toolsets = (
            enabled_toolsets if enabled_toolsets is not None else config.agent.enabled_toolsets
        )
        self._disabled_toolsets = (
            disabled_toolsets if disabled_toolsets is not None else config.agent.disabled_toolsets
        )

        self._messages: list[dict] = []
        self._anchor = UsageAnchor()
        self._usage = Usage()
        self._budget = IterationBudget(config.agent.max_iterations)
        self._interrupt = threading.Event()
        self._tool_call_log: list[dict] = []
        self._nudges = 0

    # ------------------------------------------------------------ public API
    def chat(self, message: str) -> str:
        """Convenience wrapper returning only the final response text."""
        return self.run_conversation(message).final_response

    def run_conversation(
        self,
        user_message: str,
        conversation_history: list[dict] | None = None,
        task_id: str | None = None,
    ) -> AgentResult:
        task_id = task_id or new_task_id()
        self._budget.reset()
        self._tool_call_log = []
        self._nudges = 0
        self._interrupt.clear()

        self.emitter.emit(
            EventType.AGENT_START,
            task_id=task_id,
            session_id=self.session_id,
            message=user_message,
        )

        history = self._prepare_history(conversation_history)
        history.append({"role": "user", "content": user_message})
        system = self._prompt_builder.build(
            self._prompt_context, session_id=self.session_id, iteration=1
        )
        self._messages = [{"role": "system", "content": system}] + history
        self._persist("user", content=user_message)

        stop_reason = "completed"
        error: str | None = None

        try:
            while True:
                self._budget.consume()
                iteration = self._budget.used

                self._maybe_compress()

                api_messages = prepare_api_messages(self._messages)
                request = ProviderRequest(
                    messages=api_messages,
                    tools=self._tool_specs(),
                    model=self.config.model.model,
                    max_tokens=self.config.model.max_tokens,
                    temperature=self.config.model.temperature,
                )

                try:
                    response = self._call_provider(request)
                except ProviderError as exc:
                    error = f"{exc.code}: {exc.message}"
                    self.emitter.emit(EventType.ERROR, message=error, code=exc.code)
                    stop_reason = "provider_error"
                    break

                self._anchor.update(
                    response.usage, api_messages, self.provider.context_length
                )
                self._record_usage(response.usage)

                if response.reasoning:
                    self.emitter.emit(EventType.REASONING, text=response.reasoning)

                if response.tool_calls:
                    assistant = assistant_message_from_response(response)
                    self._messages.append(assistant)
                    self._persist_assistant(assistant)

                    ctx = ToolContext(
                        task_id=task_id,
                        session_id=self.session_id,
                        state=self.state,
                        db=self.db,
                        policy=self.policy,
                        approval=self.approval,
                        agent=self,
                    )
                    if self._catalog is not None:
                        ctx.extra["catalog"] = self._catalog
                    results = self._executor.execute(response.tool_calls, ctx)

                    for result in results:
                        self._messages.append(result.to_message())
                        self._persist_tool(result)
                        self._tool_call_log.append({
                            "name": result.name,
                            "tool_call_id": result.tool_call_id,
                            "is_error": result.is_error,
                            "elapsed_ms": result.elapsed_ms,
                        })
                    continue

                # No tool calls. This is either the answer, or a turn that
                # stopped early.
                final_text = (response.text or "").strip()

                if response.finish_reason == "length":
                    # The reply was cut off mid-message. Never pass a fragment
                    # off as the answer: a reasoning model can spend its budget
                    # thinking and leave only a preamble, which reads exactly
                    # like an answer and ends the turn with nothing done.
                    message = (
                        "the model ran out of output budget before it finished. "
                        "Raise `max_tokens`, or set `reasoning_effort` to "
                        '"none" if the endpoint supports it.'
                    )
                    self.emitter.emit(
                        EventType.ERROR, message=message, code="truncated_response"
                    )
                    return AgentResult(
                        final_response="",
                        messages=list(self._messages),
                        iterations=self._budget.used,
                        usage=self._usage,
                        tool_calls=list(self._tool_call_log),
                        stop_reason="truncated_response",
                        task_id=task_id,
                        error=message,
                    )

                if not final_text:
                    message = "the model returned an empty response."
                    self.emitter.emit(EventType.ERROR, message=message,
                                      code="empty_response")
                    return AgentResult(
                        final_response="",
                        messages=list(self._messages),
                        iterations=self._budget.used,
                        usage=self._usage,
                        tool_calls=list(self._tool_call_log),
                        stop_reason="empty_response",
                        task_id=task_id,
                        error=message,
                    )

                if not self._tool_call_log and self._is_promise(final_text):
                    # The model said it would look, and then stopped. Put the
                    # promise back to it as an instruction to act.
                    if self._nudges >= self.config.agent.max_nudges:
                        message = (
                            "the model kept describing what it was about to do "
                            "instead of calling a tool. Try asking again, or ask "
                            "for something narrower."
                        )
                        self.emitter.emit(
                            EventType.ERROR, message=message, code="stalled_turn"
                        )
                        return AgentResult(
                            final_response="",
                            messages=list(self._messages),
                            iterations=self._budget.used,
                            usage=self._usage,
                            tool_calls=list(self._tool_call_log),
                            stop_reason="stalled_turn",
                            task_id=task_id,
                            error=message,
                        )
                    self._nudges += 1
                    self.emitter.emit(
                        EventType.NUDGE, attempt=self._nudges, text=final_text
                    )
                    self._messages.append({"role": "user", "content": _NUDGE})
                    continue

                assistant = assistant_message_from_response(response)
                self._messages.append(assistant)
                self._persist_assistant(assistant)

                self.emitter.emit(EventType.ASSISTANT_TEXT, text=final_text)
                return AgentResult(
                    final_response=final_text,
                    messages=list(self._messages),
                    iterations=self._budget.used,
                    usage=self._usage,
                    tool_calls=list(self._tool_call_log),
                    stop_reason=stop_reason,
                    task_id=task_id,
                )

        except BudgetExhausted as exc:
            stop_reason = "budget_exhausted"
            error = exc.message
            self.emitter.emit(EventType.BUDGET_EXHAUSTED, message=exc.message)
        except KeyboardInterrupt:
            stop_reason = "interrupted"
            error = "interrupted by user"
            self.emitter.emit(EventType.ERROR, message=error, code="interrupted")

        return AgentResult(
            final_response="",
            messages=list(self._messages),
            iterations=self._budget.used,
            usage=self._usage,
            tool_calls=list(self._tool_call_log),
            stop_reason=stop_reason,
            task_id=task_id,
            error=error,
        )

    def interrupt(self) -> None:
        """Ask the in-flight provider call to be abandoned."""
        self._interrupt.set()

    # -------------------------------------------------------------- internals
    def _is_promise(self, text: str) -> bool:
        """True when the message reads as a promise to act rather than an answer.

        "Let me take a look at the data" is a stall when nothing was requested
        and nothing came back. It is not a stall when it is the opening line of
        a real answer, so the message has to be short.
        """
        candidate = text.strip()
        if not candidate or len(candidate) > 300 or len(candidate.split()) > 45:
            return False
        first_line = next(
            (line.strip() for line in candidate.splitlines() if line.strip()), ""
        )
        if _PROMISE_RE.match(first_line):
            return True
        # "Here is what I found:" with nothing after it is the same stall in a
        # different costume.
        return candidate.endswith(":") and len(candidate) < 160

    def _tool_specs(self) -> list:
        # The context is passed so a tool can declare a runtime requirement
        # ("I need a live database connection") and be hidden when it is absent.
        ctx = ToolContext(
            task_id="",
            session_id=self.session_id,
            state=self.state,
            db=self.db,
            policy=self.policy,
            approval=self.approval,
        )
        return get_tool_definitions(
            enabled_toolsets=self._enabled_toolsets,
            disabled_toolsets=self._disabled_toolsets,
            registry=self.registry,
            context=ctx,
        )

    def _prepare_history(self, conversation_history: list[dict] | None) -> list[dict]:
        if conversation_history is not None:
            return [dict(m) for m in conversation_history]
        if self.state is not None and self.session_id:
            return self.state.get_messages_as_conversation(self.session_id)
        return []

    def _call_provider(self, request: ProviderRequest):
        """Run the provider call on a worker thread so it can be interrupted."""
        if not self._interrupt.is_set():
            outcome: dict[str, Any] = {}

            def runner() -> None:
                try:
                    outcome["response"] = self.provider.complete(request)
                except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
                    outcome["error"] = exc

            thread = threading.Thread(target=runner, daemon=True, name="tsql-provider")
            thread.start()
            while thread.is_alive():
                if self._interrupt.wait(0.05):
                    # Abandon the response; it is never injected into history.
                    raise KeyboardInterrupt("provider call interrupted")
                thread.join(0.05)
            if "error" in outcome:
                raise outcome["error"]
            return outcome["response"]
        raise KeyboardInterrupt("interrupted before the call started")

    def _record_usage(self, usage: Usage) -> None:
        self._usage = Usage(
            input_tokens=self._usage.input_tokens + usage.input_tokens,
            output_tokens=self._usage.output_tokens + usage.output_tokens,
            cache_read_tokens=self._usage.cache_read_tokens + usage.cache_read_tokens,
            cache_write_tokens=self._usage.cache_write_tokens + usage.cache_write_tokens,
            reasoning_tokens=self._usage.reasoning_tokens + usage.reasoning_tokens,
        )
        if self.state is not None and self.session_id:
            self.state.update_session_usage(
                self.session_id, usage.input_tokens, usage.output_tokens
            )
            self.state.set_usage_anchor(self.session_id, self._anchor.to_dict())

    def _maybe_compress(self) -> None:
        """Compress the transcript when the context engine says it is needed."""
        engine = self.context_engine
        if engine is None:
            return
        decision = engine.should_compress(
            self._messages,
            anchor=self._anchor,
            context_length=self.provider.context_length,
        )
        if not getattr(decision, "should_compress", False):
            return
        result = engine.compress(self._messages)
        self._messages = result.messages
        # The provider's old count no longer describes the transcript.
        self._anchor.invalidate(self._messages)
        if self.state is not None and self.session_id:
            try:
                self.state.replace_messages(self.session_id, self._messages)
            except Exception:  # noqa: BLE001 - persistence must not break the turn
                pass
        self.emitter.emit(
            EventType.CONTEXT_COMPRESSION,
            messages_before=result.messages_before,
            messages_after=result.messages_after,
            tokens_before=result.tokens_before,
            tokens_after=result.tokens_after,
            method=result.method,
            reason=getattr(decision, "reason", ""),
        )

    # ------------------------------------------------------------ persistence
    def _persist(self, role: str, content: str | None = None, **kwargs: Any) -> None:
        if self.state is None or not self.session_id:
            return
        try:
            self.state.append_message(self.session_id, role, content=content, **kwargs)
        except Exception:  # noqa: BLE001 - persistence must not break the turn
            pass

    def _persist_assistant(self, message: dict) -> None:
        self._persist(
            "assistant",
            content=message.get("content"),
            tool_calls=message.get("tool_calls"),
            reasoning=message.get("reasoning"),
            finish_reason=message.get("finish_reason"),
        )

    def _persist_tool(self, result) -> None:
        self._persist(
            "tool",
            content=result.content,
            tool_call_id=result.tool_call_id,
            tool_name=result.name,
        )

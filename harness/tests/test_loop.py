"""The agentic loop: turn lifecycle, tool dispatch, budget, events."""

from __future__ import annotations

import json
import threading
import time

import pytest

from tsql.agent.events import EventType
from tsql.agent.loop import Agent
from tsql.config import Config
from tsql.providers.base import Provider, ProviderRequest, ProviderResponse, ToolCall, Usage
from tsql.tools.registry import ToolRegistry


class FakeProvider(Provider):
    """Replays a scripted list of responses and records every request."""

    name = "fake"

    def __init__(self, responses: list[ProviderResponse], context_length: int = 100_000):
        self.responses = list(responses)
        self.requests: list[ProviderRequest] = []
        self._context_length = context_length

    @property
    def context_length(self) -> int:
        return self._context_length

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("FakeProvider ran out of scripted responses")
        return self.responses.pop(0)


def text_response(text: str, **kwargs) -> ProviderResponse:
    return ProviderResponse(text=text, usage=Usage(input_tokens=10, output_tokens=5), **kwargs)


def tool_response(calls: list[tuple[str, dict]], text: str = "") -> ProviderResponse:
    return ProviderResponse(
        text=text,
        tool_calls=[ToolCall(id=f"call_{i}", name=name, arguments=args)
                    for i, (name, args) in enumerate(calls)],
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def make_agent(provider, registry=None, config=None, **kwargs) -> Agent:
    config = config or Config()
    config.model.model = "fake-model"
    return Agent(config=config, provider=provider, registry=registry or ToolRegistry(), **kwargs)


# ------------------------------------------------------------------ happy path
def test_returns_final_text_without_tools():
    provider = FakeProvider([text_response("Revenue was $1,240.")])
    agent = make_agent(provider)
    result = agent.run_conversation("total revenue?")
    assert result.final_response == "Revenue was $1,240."
    assert result.iterations == 1
    assert result.stop_reason == "completed"


def test_tool_call_then_answer():
    registry = ToolRegistry()
    registry.register(
        name="run_sql",
        toolset="sql",
        schema={"description": "run", "parameters": {"type": "object"}},
        handler=lambda args, **kw: json.dumps({"rows": [[1]], "result_ref": "tbl_1"}),
    )
    provider = FakeProvider([
        tool_response([("run_sql", {"sql": "SELECT 1"})]),
        text_response("The answer is 1."),
    ])
    agent = make_agent(provider, registry)
    result = agent.run_conversation("how many?")

    assert result.final_response == "The answer is 1."
    assert result.iterations == 2
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "run_sql"
    assert result.tool_calls[0]["is_error"] is False


def test_tool_result_is_fed_back_to_the_model():
    registry = ToolRegistry()
    registry.register(
        name="run_sql",
        toolset="sql",
        schema={"description": "run", "parameters": {"type": "object"}},
        handler=lambda args, **kw: json.dumps({"value": 42}),
    )
    provider = FakeProvider([
        tool_response([("run_sql", {})]),
        text_response("done"),
    ])
    agent = make_agent(provider, registry)
    agent.run_conversation("q")

    second_request = provider.requests[1]
    tool_messages = [m for m in second_request.messages if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert "42" in tool_messages[0]["content"]


def test_parallel_tool_calls_preserve_order():
    registry = ToolRegistry()
    registry.register(
        name="slow",
        toolset="sql",
        schema={"description": "s", "parameters": {"type": "object"}},
        handler=lambda args, **kw: (time.sleep(0.05), json.dumps({"n": args["n"]}))[1],
    )
    provider = FakeProvider([
        tool_response([("slow", {"n": 1}), ("slow", {"n": 2}), ("slow", {"n": 3})]),
        text_response("ok"),
    ])
    agent = make_agent(provider, registry)
    agent.run_conversation("q")

    tool_messages = [m for m in provider.requests[1].messages if m["role"] == "tool"]
    assert [json.loads(m["content"])["n"] for m in tool_messages] == [1, 2, 3]


# ------------------------------------------------------------------ error paths
def test_unknown_tool_returns_an_error_to_the_model():
    provider = FakeProvider([
        tool_response([("nope", {})]),
        text_response("I cannot use that tool."),
    ])
    agent = make_agent(provider)
    result = agent.run_conversation("q")

    tool_message = [m for m in provider.requests[1].messages if m["role"] == "tool"][0]
    assert "error" in tool_message["content"]
    assert "nope" in tool_message["content"]
    assert result.final_response == "I cannot use that tool."


def test_tool_that_raises_becomes_an_error_result():
    def boom(args, **kw):
        raise RuntimeError("kaboom")

    registry = ToolRegistry()
    registry.register(
        name="run_sql",
        toolset="sql",
        schema={"description": "run", "parameters": {"type": "object"}},
        handler=boom,
    )
    provider = FakeProvider([
        tool_response([("run_sql", {})]),
        text_response("recovered"),
    ])
    agent = make_agent(provider, registry)
    result = agent.run_conversation("q")

    tool_message = [m for m in provider.requests[1].messages if m["role"] == "tool"][0]
    assert "kaboom" in tool_message["content"]
    assert result.tool_calls[0]["is_error"] is True
    assert result.final_response == "recovered"


def test_provider_error_ends_the_turn_cleanly():
    from tsql.errors import ProviderError

    class Failing(FakeProvider):
        def complete(self, request):
            raise ProviderError("bad key", code="auth_error")

    agent = make_agent(Failing([]))
    result = agent.run_conversation("q")
    assert result.stop_reason == "provider_error"
    assert "auth_error" in result.error


# ---------------------------------------------------------------------- budget
def test_budget_exhaustion_stops_the_loop():
    config = Config()
    config.agent.max_iterations = 3
    registry = ToolRegistry()
    registry.register(
        name="loop_forever",
        toolset="sql",
        schema={"description": "l", "parameters": {"type": "object"}},
        handler=lambda args, **kw: json.dumps({"ok": True}),
    )
    provider = FakeProvider([tool_response([("loop_forever", {})]) for _ in range(10)])
    agent = make_agent(provider, registry, config=config)
    result = agent.run_conversation("q")

    assert result.stop_reason == "budget_exhausted"
    assert result.iterations == 3


# ----------------------------------------------------------------------- events
def test_events_are_emitted_in_order():
    events = []
    registry = ToolRegistry()
    registry.register(
        name="run_sql",
        toolset="sql",
        schema={"description": "run", "parameters": {"type": "object"}},
        handler=lambda args, **kw: json.dumps({"ok": True}),
    )
    provider = FakeProvider([
        tool_response([("run_sql", {})]),
        text_response("answer"),
    ])
    agent = make_agent(provider, registry, on_event=events.append)
    agent.run_conversation("q")

    types = [e.type for e in events]
    assert types[0] == EventType.AGENT_START
    assert EventType.TOOL_START in types
    assert EventType.TOOL_END in types
    assert types[-1] == EventType.ASSISTANT_TEXT


def test_tool_error_emits_a_tool_error_event():
    events = []
    provider = FakeProvider([tool_response([("nope", {})]), text_response("ok")])
    agent = make_agent(provider, on_event=events.append)
    agent.run_conversation("q")
    assert any(e.type == EventType.TOOL_ERROR for e in events)


# ------------------------------------------------------------------ transcript
def test_transcript_alternates_roles_across_the_turn():
    provider = FakeProvider([text_response("hi")])
    agent = make_agent(provider)
    agent.run_conversation("hello")
    roles = [m["role"] for m in agent._messages]
    assert roles == ["system", "user", "assistant"]


def test_system_prompt_carries_the_database_context():
    from tsql.agent.prompt_builder import PromptContext

    provider = FakeProvider([text_response("ok")])
    agent = make_agent(
        provider,
        prompt_context=PromptContext(
            db_label="sales.db", dialect="sqlite", schema_block="table orders(id INT, amount REAL)"
        ),
    )
    agent.run_conversation("q")
    system = agent._messages[0]["content"]
    assert "sales.db" in system
    assert "table orders" in system


def test_tool_specs_are_sent_to_the_provider():
    registry = ToolRegistry()
    registry.register(
        name="run_sql",
        toolset="sql",
        schema={"description": "run", "parameters": {"type": "object"}},
        handler=lambda args, **kw: "{}",
    )
    provider = FakeProvider([text_response("ok")])
    agent = make_agent(provider, registry, enabled_toolsets=["sql"])
    agent.run_conversation("q")
    assert [t.name for t in provider.requests[0].tools] == ["run_sql"]


def test_unavailable_tool_is_hidden_from_the_model():
    registry = ToolRegistry()
    registry.register(
        name="needs_key",
        toolset="sql",
        schema={"description": "x", "parameters": {"type": "object"}},
        handler=lambda args, **kw: "{}",
        check_fn=lambda: False,
    )
    provider = FakeProvider([text_response("ok")])
    agent = make_agent(provider, registry, enabled_toolsets=["sql"])
    agent.run_conversation("q")
    assert provider.requests[0].tools == []


# ---------------------------------------------------------------- interruption
def test_interrupt_abandons_the_in_flight_call():
    class SlowProvider(FakeProvider):
        def complete(self, request):
            time.sleep(0.5)
            return text_response("too late")

    provider = SlowProvider([text_response("too late")])
    agent = make_agent(provider)

    def interrupt_soon():
        time.sleep(0.05)
        agent.interrupt()

    thread = threading.Thread(target=interrupt_soon)
    thread.start()
    result = agent.run_conversation("q")
    thread.join()

    assert result.stop_reason == "interrupted"
    assert result.final_response == ""


# ---------------------------------------------------------------- persistence
def test_messages_are_persisted_to_the_state_store(tmp_path):
    from tsql.state.store import SessionDB

    state = SessionDB(tmp_path / "state.db")
    session_id = state.create_session(source="test")

    registry = ToolRegistry()
    registry.register(
        name="run_sql",
        toolset="sql",
        schema={"description": "run", "parameters": {"type": "object"}},
        handler=lambda args, **kw: json.dumps({"ok": True}),
    )
    provider = FakeProvider([tool_response([("run_sql", {})]), text_response("answer")])
    agent = make_agent(provider, registry, state=state, session_id=session_id)
    agent.run_conversation("q")

    stored = state.get_messages(session_id)
    assert [m["role"] for m in stored] == ["user", "assistant", "tool", "assistant"]
    state.close()

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


# ------------------------------------------------------------ stalled turns
def test_a_promise_without_a_tool_call_is_nudged_and_retried():
    """A turn that only promises to look must not be the answer.

    The model answered "Let me take a look at the data" and stopped. The user
    saw a sentence and no work. The loop puts the promise back to the model.
    """
    provider = FakeProvider([
        text_response("Let me take a quick look at the data so I can explain it."),
        text_response("There are four tables: customers, products, orders, and a view."),
    ])
    agent = make_agent(provider)

    events: list[tuple[str, dict]] = []
    agent.emitter.add_listener(lambda e: events.append((e.type, e.data)))

    result = agent.run_conversation("explain the data to me in simple")

    assert result.final_response.startswith("There are four tables")
    assert result.iterations == 2
    assert any(t == "nudge" for t, _ in events)


def test_the_nudge_tells_the_model_to_act():
    provider = FakeProvider([
        text_response("Let me take a quick look at the data."),
        text_response("Done: 24 orders."),
    ])
    agent = make_agent(provider)
    agent.run_conversation("explain the data")

    follow_up = provider.requests[1].messages
    last = follow_up[-1]
    assert last["role"] == "user"
    assert "Call the tool now" in last["content"]
    assert "Let me take a quick look" not in last["content"]


def test_the_promise_is_never_shown_to_the_user_as_the_answer():
    provider = FakeProvider([
        text_response("Let me take a quick look at the data."),
        text_response("Done: 24 orders."),
    ])
    agent = make_agent(provider)

    texts: list[str] = []
    agent.emitter.add_listener(
        lambda e: texts.append(e.data["text"]) if e.type == EventType.ASSISTANT_TEXT else None
    )
    result = agent.run_conversation("explain the data")

    assert texts == ["Done: 24 orders."]


def test_a_stalled_turn_is_reported_once_the_nudge_budget_is_spent():
    provider = FakeProvider([
        text_response("Let me take a look."),
        text_response("Let me take a look."),
        text_response("Let me take a look."),
    ])
    config = Config()
    config.agent.max_nudges = 2
    agent = make_agent(provider, config=config)

    result = agent.run_conversation("explain the data")

    assert result.stop_reason == "stalled_turn"
    assert result.final_response == ""
    assert "describing what it was about to do" in result.error
    assert len(provider.requests) == 3  # the original plus two nudges


def test_a_greeting_is_not_a_stall():
    provider = FakeProvider([text_response("Hey! What can I help you with today?")])
    agent = make_agent(provider)
    result = agent.run_conversation("hlw")

    assert result.stop_reason == "completed"
    assert result.final_response == "Hey! What can I help you with today?"


def test_let_me_know_is_not_a_promise():
    provider = FakeProvider([text_response("Revenue was $6,330. Let me know if you need more.")])
    agent = make_agent(provider)
    result = agent.run_conversation("total revenue?")

    assert result.final_response.startswith("Revenue was $6,330")


def test_a_long_answer_that_opens_with_i_ll_is_left_alone():
    answer = "I'll walk you through it. " + ("The database holds customer and order data. " * 12)
    provider = FakeProvider([text_response(answer)])
    agent = make_agent(provider)
    result = agent.run_conversation("explain the data")

    assert result.final_response == answer.strip()
    assert result.iterations == 1


def test_a_promise_alongside_a_tool_call_is_not_a_stall():
    """The normal case: the model narrates *and* calls the tool. It is fine."""
    registry = ToolRegistry()
    registry.register(
        name="run_sql",
        toolset="sql",
        schema={"description": "run", "parameters": {"type": "object"}},
        handler=lambda args, **kw: json.dumps({"rows": [[1]], "result_ref": "tbl_1"}),
    )
    provider = FakeProvider([
        tool_response([("run_sql", {"sql": "SELECT 1"})], text="Let me take a look."),
        text_response("There is one row."),
    ])
    agent = make_agent(provider, registry)
    result = agent.run_conversation("how many rows?")

    assert result.final_response == "There is one row."
    assert result.iterations == 2
    assert len(result.tool_calls) == 1


# ------------------------------------------------------------- truncation
def test_a_truncated_response_is_an_error_not_an_answer():
    provider = FakeProvider([
        text_response("Let me take a quick look at the data so I can explain it",
                      finish_reason="length"),
    ])
    agent = make_agent(provider)

    result = agent.run_conversation("explain the data")

    assert result.stop_reason == "truncated_response"
    assert result.final_response == ""
    assert "ran out of output budget" in result.error


def test_an_empty_response_is_an_error():
    provider = FakeProvider([text_response("")])
    agent = make_agent(provider)

    result = agent.run_conversation("explain the data")

    assert result.stop_reason == "empty_response"
    assert result.final_response == ""


# ------------------------------------------------------- serialised tools
def test_a_mutating_tool_does_not_run_alongside_itself():
    """Two `add_panel` calls in one batch must not race.

    They would each create the session's dashboard, and one panel would end up
    in a dashboard of its own.
    """
    registry = ToolRegistry()
    state = {"active": 0, "peak": 0}
    lock = threading.Lock()

    def handler(args, **kwargs):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.05)
        with lock:
            state["active"] -= 1
        return json.dumps({"ok": True})

    registry.register(
        name="add_panel",
        toolset="charts",
        schema={"description": "add", "parameters": {"type": "object"}},
        handler=handler,
        parallel_safe=False,
    )
    provider = FakeProvider([
        tool_response([("add_panel", {}), ("add_panel", {}), ("add_panel", {})]),
        text_response("Three panels added."),
    ])
    agent = make_agent(provider, registry)
    result = agent.run_conversation("build a dashboard")

    assert result.final_response == "Three panels added."
    assert state["peak"] == 1, "the mutating tool ran alongside itself"


def test_read_only_tools_still_run_in_parallel():
    registry = ToolRegistry()

    def handler(args, **kwargs):
        time.sleep(0.25)
        return json.dumps({"ok": True})

    registry.register(
        name="run_sql",
        toolset="sql",
        schema={"description": "run", "parameters": {"type": "object"}},
        handler=handler,
    )
    provider = FakeProvider([
        tool_response([("run_sql", {}), ("run_sql", {}), ("run_sql", {}), ("run_sql", {})]),
        text_response("Done."),
    ])
    agent = make_agent(provider, registry)

    started = time.monotonic()
    agent.run_conversation("how many?")
    elapsed = time.monotonic() - started

    # four reads of a quarter of a second: in parallel, about a quarter second
    assert elapsed < 0.8, f"the read-only tools did not run in parallel ({elapsed:.2f}s)"


def test_add_panel_is_registered_as_not_parallel_safe():
    from tsql.tools import chart_tools

    entry = chart_tools.registry.get("add_panel")
    assert entry is not None
    assert entry.parallel_safe is False

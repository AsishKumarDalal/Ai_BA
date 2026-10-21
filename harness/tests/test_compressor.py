"""The context compressor: pruning, boundaries, summarization, assembly."""

from __future__ import annotations

import json

import pytest

from tsql.agent.context_compressor import (
    SUMMARY_PREFIX,
    ContextCompressor,
    ProviderSummarizer,
)
from tsql.agent.usage_anchor import UsageAnchor
from tsql.config import CompressionConfig


def tool_result(ref: str, rows: int = 5000, columns=("region", "revenue")) -> str:
    return json.dumps({
        "status": "ok",
        "result_ref": ref,
        "row_count": rows,
        "columns": [{"name": c} for c in columns],
        "rows": [["x"] * len(columns)] * 10,
    })


def summary_of(result) -> dict:
    """The compressed-transcript summary message."""
    for message in result.messages:
        if SUMMARY_PREFIX in (message.get("content") or ""):
            return message
    raise AssertionError("no summary message in the compressed transcript")


def build_transcript(turns: int = 12) -> list[dict]:
    """A transcript shaped like a long report-building session."""
    messages: list[dict] = [
        {"role": "system", "content": "you are the analyst"},
        {"role": "user", "content": "build a Q3 sales report"},
    ]
    for index in range(turns):
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": f"c{index}",
                "type": "function",
                "function": {"name": "run_sql", "arguments": '{"sql": "SELECT ..."}'},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": f"c{index}",
            "tool_name": "run_sql",
            "content": tool_result(f"tbl_{index}"),
        })
    messages.append({"role": "user", "content": "now add the narrative"})
    return messages


@pytest.fixture
def compressor() -> ContextCompressor:
    config = CompressionConfig()
    config.protect_first_n = 3
    config.protect_last_n = 8
    return ContextCompressor(config, model="test-model", context_length=8_000)


# ------------------------------------------------------------------ decision
def test_does_not_compress_a_short_transcript(compressor):
    decision = compressor.should_compress(
        [{"role": "user", "content": "hi"}], context_length=8_000
    )
    assert not decision.should_compress


def test_compresses_when_over_the_threshold(compressor):
    messages = build_transcript(100)
    decision = compressor.should_compress(messages, context_length=8_000)
    assert decision.should_compress
    assert decision.threshold_tokens == 4_000
    assert "threshold" in decision.reason


def test_compression_can_be_disabled():
    config = CompressionConfig(enabled=False)
    engine = ContextCompressor(config, context_length=8_000)
    assert not engine.should_compress(build_transcript(40), context_length=8_000).should_compress


def test_per_model_threshold_override():
    config = CompressionConfig(threshold=0.5, model_thresholds={"test-model": 0.2})
    engine = ContextCompressor(config, model="test-model", context_length=8_000)
    decision = engine.should_compress(build_transcript(20), context_length=8_000)
    assert decision.threshold_tokens == 1_600


def test_uses_the_anchor_when_available(compressor):
    messages = build_transcript(4)
    anchor = UsageAnchor()
    anchor.update(type("U", (), {"input_tokens": 900_000, "output_tokens": 10})(), messages, 8_000)
    decision = compressor.should_compress(messages, anchor=anchor, context_length=8_000)
    assert decision.should_compress
    assert decision.estimated_tokens >= 900_000


# -------------------------------------------------------------------- phase 1
def test_old_tool_results_become_stubs_that_keep_the_handle(compressor):
    messages = build_transcript(20)
    result = compressor.compress(messages)
    stubs = [
        m for m in result.messages
        if m.get("role") == "tool" and "tool result cleared" in (m.get("content") or "")
    ]
    assert stubs
    assert "result_ref=tbl_" in stubs[0]["content"]
    assert "region" in stubs[0]["content"]


def test_the_most_recent_tool_results_stay_verbatim(compressor):
    messages = build_transcript(20)
    result = compressor.compress(messages)
    verbatim = [
        m for m in result.messages
        if m.get("role") == "tool" and "tool result cleared" not in (m.get("content") or "")
    ]
    assert verbatim
    assert json.loads(verbatim[-1]["content"])["result_ref"]


def test_the_summarizer_input_is_pruned(compressor):
    captured = {}

    def summarizer(middle, focus=None):
        captured["middle"] = middle
        return "## Goal\nsummary"

    compressor.set_summarizer(summarizer)
    compressor.compress(build_transcript(10))
    tool_messages = [m for m in captured["middle"] if m.get("role") == "tool"]
    assert tool_messages
    assert all("tool result cleared" in (m.get("content") or "") for m in tool_messages)


def test_small_tool_results_are_kept(compressor):
    messages = build_transcript(10)
    result = compressor.compress(messages)
    tail_tools = [
        m for m in result.messages
        if m.get("role") == "tool" and "tool result cleared" not in (m.get("content") or "")
    ]
    assert tail_tools


def test_a_blocked_query_keeps_its_reason(compressor):
    messages = build_transcript(10)
    blocked = {
        "role": "tool", "tool_call_id": "cx", "tool_name": "run_sql",
        "content": json.dumps({"status": "blocked", "errors": ["unknown table: x"] * 20}),
    }
    messages.insert(5, blocked)
    result = compressor.compress(messages)
    summary = summary_of(result)["content"]
    assert "unknown table" in summary


# -------------------------------------------------------------------- phase 2
def test_the_head_is_preserved(compressor):
    messages = build_transcript(10)
    result = compressor.compress(messages)
    assert result.messages[0]["role"] == "system"
    assert result.messages[0]["content"] == "you are the analyst"
    assert result.messages[1]["content"] == "build a Q3 sales report"


def test_the_last_user_message_survives(compressor):
    messages = build_transcript(10)
    result = compressor.compress(messages)
    user_messages = [m for m in result.messages if m.get("role") == "user"]
    assert "now add the narrative" in [m["content"] for m in user_messages]


def test_tool_calls_are_never_split_from_their_results(compressor):
    messages = build_transcript(10)
    result = compressor.compress(messages)
    result_ids = {
        call["id"]
        for m in result.messages
        if m.get("role") == "assistant"
        for call in (m.get("tool_calls") or [])
    }
    answered = {
        m.get("tool_call_id")
        for m in result.messages
        if m.get("role") == "tool"
    }
    assert result_ids <= answered


# -------------------------------------------------------------------- phase 3
def test_the_summary_is_inserted(compressor):
    result = compressor.compress(build_transcript(10))
    assert any(SUMMARY_PREFIX in (m.get("content") or "") for m in result.messages)


def test_the_deterministic_summary_lists_artifacts(compressor):
    result = compressor.compress(build_transcript(6))
    summary = summary_of(result)["content"]
    assert "tbl_" in summary
    assert "run_sql" in summary


def test_the_deterministic_summary_quotes_the_goal(compressor):
    result = compressor.compress(build_transcript(6))
    assert "build a Q3 sales report" in summary_of(result)["content"]


def test_a_summarizer_is_used_when_available(compressor):
    calls = []

    def summarizer(messages, focus=None):
        calls.append(messages)
        return "## Goal\nShip the report."

    compressor.set_summarizer(summarizer)
    result = compressor.compress(build_transcript(6))
    assert result.method == "llm_summary"
    assert calls
    assert "Ship the report." in summary_of(result)["content"]


def test_a_failing_summarizer_falls_back_deterministically(compressor):
    def summarizer(messages, focus=None):
        raise RuntimeError("provider down")

    compressor.set_summarizer(summarizer)
    result = compressor.compress(build_transcript(6))
    assert result.method == "deterministic_fallback"
    assert SUMMARY_PREFIX in summary_of(result)["content"]


def test_the_previous_summary_is_carried_forward(compressor):
    compressor.set_summarizer(lambda m, f=None: "## Goal\nFirst pass.")
    compressor.compress(build_transcript(6))
    compressor.set_summarizer(None)
    result = compressor.compress(build_transcript(6))
    assert "First pass." in summary_of(result)["content"]


# -------------------------------------------------------------------- phase 4
def test_the_result_is_smaller(compressor):
    messages = build_transcript(30)
    result = compressor.compress(messages)
    assert result.tokens_after < result.tokens_before
    assert result.messages_after < result.messages_before


def test_the_result_has_a_legal_role_sequence(compressor):
    from tsql.agent.messages import prepare_api_messages

    result = compressor.compress(build_transcript(12))
    prepared = prepare_api_messages(result.messages)
    roles = [m["role"] for m in prepared]
    for index in range(1, len(roles) - 1):
        if roles[index] == roles[index + 1] and roles[index] != "tool":
            raise AssertionError(f"illegal role sequence: {roles}")


def test_every_assistant_tool_call_has_a_result(compressor):
    from tsql.agent.messages import prepare_api_messages

    result = compressor.compress(build_transcript(12))
    prepared = prepare_api_messages(result.messages)
    calls = {
        call["id"]
        for m in prepared
        if m.get("role") == "assistant"
        for call in (m.get("tool_calls") or [])
    }
    answered = {m.get("tool_call_id") for m in prepared if m.get("role") == "tool"}
    assert calls <= answered


# ------------------------------------------------------------- provider summarizer
def test_provider_summarizer_calls_the_model():
    class FakeProvider:
        def __init__(self):
            self.requests = []

        def complete(self, request):
            self.requests.append(request)
            return type("R", (), {"text": "the summary"})()

    provider = FakeProvider()
    summarizer = ProviderSummarizer(provider, model="m", max_tokens=100)
    result = summarizer([{"role": "user", "content": "do the thing"}], focus="revenue")

    assert result == "the summary"
    assert provider.requests[0].tools == []
    assert "revenue" in provider.requests[0].messages[0]["content"]


# ------------------------------------------------------------------- anchoring
def test_anchor_invalidation_rebases_the_count():
    anchor = UsageAnchor()
    anchor.update(type("U", (), {"input_tokens": 5000, "output_tokens": 100})(),
                  [{"role": "user", "content": "x"}], 8_000)
    assert anchor.prompt_tokens == 5000
    anchor.invalidate([{"role": "user", "content": "short"}])
    assert anchor.prompt_tokens < 5000


# ------------------------------------------------------------------ integration
def test_the_agent_compresses_mid_run(tmp_path):
    """A long turn must survive the context window, with the last user
    message still intact at the end."""
    from tsql.agent.loop import Agent
    from tsql.config import Config
    from tsql.providers.base import Provider, ProviderRequest, ProviderResponse, ToolCall, Usage
    from tsql.state.store import SessionDB

    class LoopingProvider(Provider):
        name = "looping"

        def __init__(self):
            self.calls = 0
            self.requests = []

        @property
        def context_length(self) -> int:
            return 4_000

        def complete(self, request: ProviderRequest) -> ProviderResponse:
            from tsql.util import estimate_messages_tokens

            self.requests.append(request)
            self.calls += 1
            # A real provider reports the whole prompt it was sent.
            prompt_tokens = estimate_messages_tokens(request.messages)
            if self.calls < 12:
                return ProviderResponse(
                    tool_calls=[ToolCall(id=f"c{self.calls}", name="run_sql",
                                         arguments={"sql": "SELECT 1", "purpose": "p"})],
                    usage=Usage(input_tokens=prompt_tokens, output_tokens=20),
                )
            return ProviderResponse(
                text="All done.",
                usage=Usage(input_tokens=prompt_tokens, output_tokens=20),
            )

    config = Config()
    config.model.model = "looping"
    config.agent.max_iterations = 30
    config.compression.threshold = 0.5
    config.compression.protect_last_n = 4

    compressor = ContextCompressor(config.compression, model="looping", context_length=4_000)
    provider = LoopingProvider()
    state = SessionDB(tmp_path / "state.db")
    session_id = state.create_session(source="test")

    # A tool that returns a realistically bulky result.
    from tsql.tools.registry import ToolRegistry
    registry = ToolRegistry()
    registry.register(
        name="run_sql", toolset="sql",
        schema={"description": "run", "parameters": {"type": "object"}},
        handler=lambda args, **kw: json.dumps({
            "status": "ok", "result_ref": "tbl_x", "row_count": 4000,
            "columns": [{"name": "region"}, {"name": "revenue"}],
            "rows": [["East", 1234.5]] * 120,
        }),
    )

    events = []
    agent = Agent(
        config=config,
        provider=provider,
        registry=registry,
        state=state,
        session_id=session_id,
        context_engine=compressor,
        on_event=events.append,
    )
    result = agent.run_conversation("build me a report")

    compressions = [e for e in events if e.type == "context_compression"]
    assert compressions, "the transcript should have been compressed"
    assert result.final_response == "All done."

    # The last user message survived the compression.
    user_messages = [m for m in agent._messages if m.get("role") == "user"]
    assert any("build me a report" in (m.get("content") or "") for m in user_messages)

    # The transcript stayed under the window.
    from tsql.util import estimate_messages_tokens
    assert estimate_messages_tokens(agent._messages) < 4_000

    state.close()



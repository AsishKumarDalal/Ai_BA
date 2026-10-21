"""Message normalization: role alternation and tool-call pairing."""

from __future__ import annotations

from tsql.agent.messages import (
    enforce_alternation,
    prepare_api_messages,
    sanitize_tool_pairs,
)


def test_system_stays_first():
    messages = enforce_alternation([
        {"role": "system", "content": "a"},
        {"role": "user", "content": "b"},
    ])
    assert messages[0]["role"] == "system"
    assert len(messages) == 2


def test_late_system_message_is_folded_into_the_head():
    messages = enforce_alternation([
        {"role": "system", "content": "a"},
        {"role": "user", "content": "b"},
        {"role": "system", "content": "c"},
    ])
    assert len(messages) == 2
    assert messages[0]["content"] == "a\n\nc"


def test_consecutive_user_messages_are_merged():
    messages = enforce_alternation([
        {"role": "user", "content": "one"},
        {"role": "user", "content": "two"},
    ])
    assert len(messages) == 1
    assert messages[0]["content"] == "one\n\ntwo"


def test_consecutive_assistant_messages_are_merged():
    messages = enforce_alternation([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a1"},
        {"role": "assistant", "content": "a2"},
    ])
    assert len(messages) == 2
    assert messages[1]["content"] == "a1a2"


def test_merging_assistants_keeps_tool_calls():
    messages = enforce_alternation([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {"name": "a", "arguments": "{}"}}]},
        {"role": "assistant", "content": "tail",
         "tool_calls": [{"id": "2", "function": {"name": "b", "arguments": "{}"}}]},
    ])
    assert len(messages[1]["tool_calls"]) == 2
    assert messages[1]["content"] == "tail"


def test_consecutive_tool_messages_are_allowed():
    messages = enforce_alternation([
        {"role": "user", "content": "q"},
        {"role": "assistant", "tool_calls": [
            {"id": "1", "function": {"name": "a", "arguments": "{}"}},
            {"id": "2", "function": {"name": "b", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "r1"},
        {"role": "tool", "tool_call_id": "2", "content": "r2"},
    ])
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "tool"]


def test_assistant_before_user_gets_a_user_placeholder():
    messages = enforce_alternation([
        {"role": "system", "content": "s"},
        {"role": "assistant", "content": "orphan"},
    ])
    assert messages[1]["role"] == "user"
    assert messages[2]["role"] == "assistant"


def test_sanitize_stubs_missing_tool_results():
    messages = sanitize_tool_pairs([
        {"role": "user", "content": "q"},
        {"role": "assistant", "tool_calls": [
            {"id": "1", "function": {"name": "a", "arguments": "{}"}}]},
    ])
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "1"
    assert "unavailable" in messages[2]["content"]


def test_sanitize_keeps_matched_tool_results():
    messages = sanitize_tool_pairs([
        {"role": "assistant", "tool_calls": [
            {"id": "1", "function": {"name": "a", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "ok"},
    ])
    assert len(messages) == 2
    assert messages[1]["content"] == "ok"


def test_prepare_api_messages_is_idempotent():
    raw = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a", "tool_calls": [
            {"id": "1", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "r"},
    ]
    once = prepare_api_messages(raw)
    twice = prepare_api_messages(once)
    assert once == twice

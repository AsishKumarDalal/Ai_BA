"""Provider layer: message conversion, response parsing, error normalization."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tsql.config import ModelConfig
from tsql.errors import ProviderError
from tsql.providers import build_provider, resolve_api_mode
from tsql.providers.anthropic import convert_messages
from tsql.providers.base import ProviderRequest, ToolCall, ToolSpec, parse_tool_arguments
from tsql.providers.openai_compat import OpenAICompatProvider


# ----------------------------------------------------------------- api mode
def test_api_mode_detection_by_provider():
    assert resolve_api_mode(ModelConfig(provider="anthropic")) == "anthropic_messages"
    assert resolve_api_mode(ModelConfig(provider="openai")) == "chat_completions"


def test_api_mode_detection_by_base_url():
    config = ModelConfig(provider="custom", base_url="https://api.anthropic.com")
    assert resolve_api_mode(config) == "anthropic_messages"


def test_explicit_api_mode_wins():
    config = ModelConfig(provider="anthropic", api_mode="chat_completions")
    assert resolve_api_mode(config) == "chat_completions"


def test_build_provider_selects_adapter():
    assert isinstance(build_provider(ModelConfig(provider="anthropic")), type(
        build_provider(ModelConfig(provider="anthropic"))
    ))
    assert build_provider(ModelConfig(provider="openai")).name == "openai_compat"


# ------------------------------------------------------------- tool arguments
def test_parse_tool_arguments_handles_json():
    assert parse_tool_arguments('{"a": 1}') == {"a": 1}


def test_parse_tool_arguments_handles_empty():
    assert parse_tool_arguments("") == {}
    assert parse_tool_arguments(None) == {}


def test_parse_tool_arguments_flags_malformed():
    parsed = parse_tool_arguments('{"a": ')
    assert "__invalid_arguments__" in parsed


# ------------------------------------------------------------- openai adapter
def _fake_response(*, text="", tool_calls=None, usage=None, finish_reason="stop"):
    message = SimpleNamespace(content=text, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
                           usage=usage)


class _FakeCompletions:
    def __init__(self, response):
        self._response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.chat = SimpleNamespace(completions=_FakeCompletions(response))


def test_openai_provider_parses_text():
    provider = OpenAICompatProvider(ModelConfig(model="m"), client=_FakeClient(_fake_response(text="hi")))
    response = provider.complete(ProviderRequest(messages=[{"role": "user", "content": "x"}]))
    assert response.text == "hi"
    assert response.tool_calls == []
    assert response.finish_reason == "stop"


def test_openai_provider_parses_tool_calls():
    call = SimpleNamespace(id="call_1", function=SimpleNamespace(name="run_sql",
                                                                 arguments='{"sql": "SELECT 1"}'))
    provider = OpenAICompatProvider(ModelConfig(model="m"),
                                    client=_FakeClient(_fake_response(tool_calls=[call])))
    response = provider.complete(ProviderRequest(messages=[{"role": "user", "content": "x"}]))
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "run_sql"
    assert response.tool_calls[0].arguments == {"sql": "SELECT 1"}


def test_openai_provider_parses_usage():
    usage = SimpleNamespace(prompt_tokens=120, completion_tokens=30)
    provider = OpenAICompatProvider(ModelConfig(model="m"),
                                    client=_FakeClient(_fake_response(text="ok", usage=usage)))
    response = provider.complete(ProviderRequest(messages=[]))
    assert response.usage.input_tokens == 120
    assert response.usage.output_tokens == 30


def test_openai_provider_sends_tool_schemas():
    provider = OpenAICompatProvider(ModelConfig(model="m"), client=_FakeClient(_fake_response()))
    spec = ToolSpec(name="run_sql", description="run", parameters={"type": "object"})
    provider.complete(ProviderRequest(messages=[], tools=[spec]))
    sent = provider._client.chat.completions.kwargs["tools"]
    assert sent[0]["function"]["name"] == "run_sql"


def test_openai_provider_normalizes_rate_limit():
    class Boom(Exception):
        status_code = 429

    provider = OpenAICompatProvider(ModelConfig(model="m"), client=_FakeClient(_fake_response()))
    provider._client.chat.completions.create = lambda **kw: (_ for _ in ()).throw(Boom("rate limit exceeded"))
    with pytest.raises(ProviderError) as excinfo:
        provider.complete(ProviderRequest(messages=[]))
    assert excinfo.value.retryable is True
    assert excinfo.value.code == "rate_limited"


def test_openai_provider_marks_auth_error_non_retryable():
    class Boom(Exception):
        status_code = 401

    provider = OpenAICompatProvider(ModelConfig(model="m"), client=_FakeClient(_fake_response()))
    provider._client.chat.completions.create = lambda **kw: (_ for _ in ()).throw(Boom("bad key"))
    with pytest.raises(ProviderError) as excinfo:
        provider.complete(ProviderRequest(messages=[]))
    assert excinfo.value.retryable is False
    assert excinfo.value.code == "auth_error"


# ------------------------------------------------------- anthropic conversion
def test_convert_messages_extracts_system():
    system, messages = convert_messages([
        {"role": "system", "content": "be precise"},
        {"role": "user", "content": "hello"},
    ])
    assert system == [{"type": "text", "text": "be precise"}]
    assert messages[0]["role"] == "user"


def test_convert_messages_builds_tool_use_blocks():
    _, messages = convert_messages([
        {"role": "assistant", "content": "let me check",
         "tool_calls": [{"id": "t1", "function": {"name": "run_sql", "arguments": '{"sql":"SELECT 1"}'}}]},
    ])
    blocks = messages[0]["content"]
    assert blocks[0] == {"type": "text", "text": "let me check"}
    assert blocks[1]["type"] == "tool_use"
    assert blocks[1]["input"] == {"sql": "SELECT 1"}


def test_convert_messages_builds_tool_result_blocks():
    _, messages = convert_messages([
        {"role": "assistant", "tool_calls": [
            {"id": "t1", "function": {"name": "run_sql", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": '{"rows": [[1]]}'},
    ])
    assert messages[1]["role"] == "user"
    assert messages[1]["content"][0]["type"] == "tool_result"
    assert messages[1]["content"][0]["tool_use_id"] == "t1"


def test_convert_messages_merges_consecutive_user_turns():
    _, messages = convert_messages([
        {"role": "user", "content": "one"},
        {"role": "user", "content": "two"},
    ])
    assert len(messages) == 1
    assert len(messages[0]["content"]) == 2

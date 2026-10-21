"""Small shared helpers."""

from __future__ import annotations

from tsql.util import (
    canonical_json,
    collapse_whitespace,
    estimate_message_tokens,
    estimate_tokens,
    fingerprint,
    new_id,
    sha256_hex,
    truncate,
    truncate_cell,
)


def test_new_id_has_prefix_and_is_unique():
    first = new_id("tbl")
    second = new_id("tbl")
    assert first.startswith("tbl_")
    assert first != second


def test_estimate_tokens_scales_with_length():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0
    assert estimate_tokens("a" * 400) > estimate_tokens("a" * 40)


def test_estimate_message_tokens_counts_tool_calls():
    plain = {"role": "assistant", "content": "short"}
    with_calls = {
        "role": "assistant",
        "content": "short",
        "tool_calls": [{"function": {"name": "run_sql", "arguments": '{"sql": "SELECT 1"}'}}],
    }
    assert estimate_message_tokens(with_calls) > estimate_message_tokens(plain)


def test_estimate_message_tokens_handles_list_content():
    message = {"role": "user", "content": [{"type": "text", "text": "hello there"}]}
    assert estimate_message_tokens(message) > 0


def test_canonical_json_is_key_order_independent():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_fingerprint_is_stable_and_content_sensitive():
    a = {"role": "user", "content": "hello"}
    b = {"role": "user", "content": "hello"}
    c = {"role": "user", "content": "world"}
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a) != fingerprint(c)


def test_sha256_hex_is_hex():
    digest = sha256_hex("payload")
    assert len(digest) == 64
    int(digest, 16)


def test_truncate_adds_suffix():
    assert truncate("abcdef", 10) == "abcdef"
    assert truncate("abcdef", 4).endswith("…")
    assert len(truncate("abcdef", 4)) == 4


def test_truncate_cell_preserves_non_strings():
    assert truncate_cell(42, 1) == 42
    assert truncate_cell(None, 1) is None
    assert truncate_cell("x" * 100, 10).endswith("…")
    assert "bytes elided" in truncate_cell(b"x" * 100, 10)


def test_collapse_whitespace():
    assert collapse_whitespace("  a \n\t b  ") == "a b"

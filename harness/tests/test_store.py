"""The SQLite state store: sessions, transcript, artifacts, audit chain."""

from __future__ import annotations

import json
import threading

import pytest

from tsql.errors import SessionError
from tsql.state.store import SessionDB, sanitize_fts_query


@pytest.fixture
def db(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    yield store
    store.close()


# ------------------------------------------------------------------ sessions
def test_create_and_read_a_session(db):
    session_id = db.create_session(source="cli", model="test-model", title="My session")
    session = db.get_session(session_id)
    assert session["id"] == session_id
    assert session["source"] == "cli"
    assert session["title"] == "My session"
    assert session["ended_at"] is None


def test_end_and_reopen_a_session(db):
    session_id = db.create_session()
    db.end_session(session_id, end_reason="user_exit")
    assert db.get_session(session_id)["ended_at"] is not None
    db.reopen_session(session_id)
    assert db.get_session(session_id)["ended_at"] is None


def test_list_sessions_returns_the_newest_first(db):
    first = db.create_session(title="a")
    second = db.create_session(title="b")
    ids = [s["id"] for s in db.list_sessions()]
    assert ids[0] == second
    assert ids[-1] == first


def test_session_titles_are_unique(db):
    db.create_session(title="same")
    with pytest.raises(Exception):
        db.create_session(title="same")


def test_list_sessions_includes_a_preview(db):
    session_id = db.create_session()
    db.append_message(session_id, "user", content="what were the top regions?")
    rows = db.list_sessions()
    assert "top regions" in rows[0]["preview"]


# ------------------------------------------------------------------ messages
def test_append_and_read_messages(db):
    session_id = db.create_session()
    db.append_message(session_id, "user", content="hello")
    db.append_message(session_id, "assistant", content="hi there")
    messages = db.get_messages(session_id)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "hi there"


def test_tool_calls_are_stored_as_json(db):
    session_id = db.create_session()
    calls = [{"id": "c1", "function": {"name": "run_sql", "arguments": "{}"}}]
    db.append_message(session_id, "assistant", content="", tool_calls=calls)
    message = db.get_messages(session_id)[0]
    assert message["tool_calls"] == calls


def test_conversation_projection_uses_the_provider_format(db):
    session_id = db.create_session()
    db.append_message(session_id, "user", content="q")
    db.append_message(session_id, "assistant", content="a")
    db.append_message(session_id, "tool", content="r", tool_call_id="c1", tool_name="run_sql")
    conversation = db.get_messages_as_conversation(session_id)
    assert conversation[2] == {
        "role": "tool", "tool_call_id": "c1", "content": "r"
    }


def test_replace_messages_archives_the_old_rows(db):
    session_id = db.create_session()
    for i in range(5):
        db.append_message(session_id, "user", content=f"m{i}")
    db.replace_messages(session_id, [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "summary"},
    ])
    live = db.get_messages(session_id)
    assert len(live) == 2
    # The archived rows are still there, soft-deleted.
    all_rows = db.get_messages(session_id, active_only=False)
    assert len(all_rows) == 7
    assert sum(1 for m in all_rows if m["active"] == 0) == 5


def test_archived_messages_remain_searchable(db):
    session_id = db.create_session()
    db.append_message(session_id, "user", content="the quarterly revenue target")
    db.replace_messages(session_id, [{"role": "user", "content": "unrelated"}])
    results = db.search_messages("quarterly revenue")
    assert any(r["session_id"] == session_id for r in results)


def test_message_uid_is_stable_across_a_replace(db):
    session_id = db.create_session()
    db.append_message(session_id, "user", content="original")
    uid = db.get_messages(session_id)[0]["message_uid"]
    db.replace_messages(session_id, [
        {"role": "user", "content": "original", "message_uid": uid}
    ])
    assert db.get_messages(session_id)[0]["message_uid"] == uid


# -------------------------------------------------------------------- search
def test_search_finds_messages(db):
    session_id = db.create_session()
    db.append_message(session_id, "user", content="docker deployment plan")
    results = db.search_messages("docker deployment")
    assert results
    assert "docker" in results[0]["snippet"]


def test_search_supports_phrase_and_boolean(db):
    session_id = db.create_session()
    db.append_message(session_id, "user", content="docker deployment")
    db.append_message(session_id, "user", content="kubernetes deployment")
    assert db.search_messages('"docker deployment"')
    assert db.search_messages("docker OR kubernetes")


def test_search_can_filter_by_role(db):
    session_id = db.create_session()
    db.append_message(session_id, "user", content="alpha")
    db.append_message(session_id, "assistant", content="alpha")
    assert all(r["role"] == "user" for r in db.search_messages("alpha", role_filter=["user"]))


def test_search_survives_a_malformed_query(db):
    db.search_messages('unbalanced "quote')
    db.search_messages("dangling AND")
    db.search_messages("hyphenated-term")


def test_fts_query_sanitizer():
    assert sanitize_fts_query('unbalanced "quote') != ""
    assert "AND" not in sanitize_fts_query("hello AND").split()
    assert '"chat-send"' in sanitize_fts_query("chat-send")


# ----------------------------------------------------------------- artifacts
def test_create_and_read_an_artifact(db):
    session_id = db.create_session()
    artifact_id = db.create_artifact(
        session_id, kind="table", title="revenue",
        payload={"rows": [[1, 2]]}, meta={"sql": "SELECT 1"},
    )
    artifact = db.get_artifact(artifact_id)
    assert artifact["kind"] == "table"
    assert artifact["payload"]["rows"] == [[1, 2]]
    assert artifact["meta"]["sql"] == "SELECT 1"


def test_artifact_ids_are_prefixed_by_kind(db):
    session_id = db.create_session()
    table = db.create_artifact(session_id, kind="table", payload={})
    chart = db.create_artifact(session_id, kind="chart", payload={})
    dashboard = db.create_artifact(session_id, kind="dashboard", payload={})
    assert table.startswith("tbl_")
    assert chart.startswith("cht_")
    assert dashboard.startswith("dash_")


def test_update_an_artifact(db):
    session_id = db.create_session()
    artifact_id = db.create_artifact(session_id, kind="dashboard", payload={"panels": []})
    db.update_artifact(artifact_id, payload={"panels": [{"id": "p1"}]})
    assert db.get_artifact(artifact_id)["payload"]["panels"] == [{"id": "p1"}]


def test_list_artifacts_by_kind(db):
    session_id = db.create_session()
    db.create_artifact(session_id, kind="table", payload={})
    db.create_artifact(session_id, kind="dashboard", payload={})
    assert len(db.list_artifacts(session_id, kind="table")) == 1
    assert len(db.list_artifacts(session_id)) == 2


def test_missing_artifact_is_none(db):
    assert db.get_artifact("tbl_nope") is None


# --------------------------------------------------------------------- audit
def test_audit_entries_are_chained(db):
    db.append_audit("s1", "sql_allowed", {"sql": "SELECT 1"})
    db.append_audit("s1", "sql_blocked", {"sql": "DROP TABLE x"})
    entries = db.list_audit()
    assert len(entries) == 2
    assert entries[1]["prev_hash"] == entries[0]["hash"]


def test_audit_chain_verifies(db):
    db.append_audit("s1", "sql_allowed", {"sql": "SELECT 1"})
    db.append_audit("s1", "sql_allowed", {"sql": "SELECT 2"})
    result = db.verify_audit_chain()
    assert result["valid"] is True
    assert result["checked"] == 2


def test_tampering_with_the_audit_log_is_detected(db):
    db.append_audit("s1", "sql_allowed", {"sql": "SELECT 1"})
    db.append_audit("s1", "sql_allowed", {"sql": "SELECT 2"})
    with db._lock:
        db._conn.execute("UPDATE audit_log SET detail = ? WHERE id = 1",
                         (json.dumps({"sql": "SELECT evil"}),))
    result = db.verify_audit_chain()
    assert result["valid"] is False
    assert result["broken_at_id"] == 1


def test_empty_audit_verifies(db):
    assert db.verify_audit_chain()["valid"] is True


# ------------------------------------------------------------------ counters
def test_usage_counters_accumulate(db):
    session_id = db.create_session()
    db.update_session_usage(session_id, 100, 20)
    db.update_session_usage(session_id, 50, 10)
    session = db.get_session(session_id)
    assert session["input_tokens"] == 150
    assert session["output_tokens"] == 30


def test_usage_anchor_round_trips(db):
    session_id = db.create_session()
    anchor = {"prompt_tokens": 1000, "completion_tokens": 50, "last_fingerprint": "abc"}
    db.set_usage_anchor(session_id, anchor)
    assert json.loads(db.get_session(session_id)["usage_anchor"])["prompt_tokens"] == 1000


# ----------------------------------------------------------------- contention
def test_concurrent_writes_all_succeed(db):
    session_id = db.create_session()
    errors: list[Exception] = []

    def writer(index: int):
        try:
            for i in range(20):
                db.append_message(session_id, "user", content=f"w{index}-{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    assert len(db.get_messages(session_id)) == 80

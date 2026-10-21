"""SQLite-backed session, artifact and audit store.

Design notes (mirroring the Hermes state design):

* WAL mode so readers never block the single writer.
* ``BEGIN IMMEDIATE`` plus jittered retry so competing writers do not convoy.
* In-place compression: the live transcript is rewritten on the *same* session id and
  the replaced rows are archived (``active=0, compacted=1``) rather than deleted, so
  they stay searchable through FTS5.
* The artifact store is the shared spine between harness and UI: SQL results, charts
  and dashboards are all addressable artifacts.
"""

from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from ..errors import SessionError
from ..util import canonical_json, new_id, now, sha256_hex

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 1

# Write-contention tuning (mirrors Hermes' patience ladder).
_WRITE_TIMEOUT_S = 1.0
_TRANSCRIPT_PATIENCE_S = 60.0
_RETRY_MIN_S = 0.020
_RETRY_MAX_S = 0.150
_RETRY_SLOW_MIN_S = 0.250
_RETRY_SLOW_MAX_S = 1.000
_CHECKPOINT_EVERY_N_WRITES = 50


class SessionDB:
    """Thread-safe facade over the state database."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or ":memory:").expanduser()
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._write_count = 0
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=_WRITE_TIMEOUT_S,
            isolation_level=None,  # explicit transaction control
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._apply_schema()

    # ------------------------------------------------------------- lifecycle
    def _configure(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute(f"PRAGMA busy_timeout={int(_WRITE_TIMEOUT_S * 1000)}")
        cur.close()

    def _apply_schema(self) -> None:
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with self._lock:
            self._conn.executescript(sql)
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
            elif int(row["version"]) < SCHEMA_VERSION:
                self._conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass
            self._conn.close()

    def __enter__(self) -> "SessionDB":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------------------------------------------------------------- writes
    def _execute_write(self, sql: str, params: tuple = (), patience: float = _TRANSCRIPT_PATIENCE_S):
        """Run a write with BEGIN IMMEDIATE and a jittered retry ladder."""
        deadline = time.monotonic() + patience
        attempt = 0
        while True:
            attempt += 1
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        cur = self._conn.execute(sql, params)
                        result = cur.lastrowid
                        self._conn.execute("COMMIT")
                    except Exception:
                        self._conn.execute("ROLLBACK")
                        raise
                self._after_write()
                return result
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                if time.monotonic() >= deadline:
                    raise SessionError(
                        f"state database is locked: {exc}",
                        code="state_locked",
                    ) from exc
                time.sleep(self._backoff(attempt))

    def _backoff(self, attempt: int) -> float:
        if attempt <= 6:
            return random.uniform(_RETRY_MIN_S, _RETRY_MAX_S)
        return random.uniform(_RETRY_SLOW_MIN_S, _RETRY_SLOW_MAX_S)

    def _after_write(self) -> None:
        self._write_count += 1
        if self._write_count % _CHECKPOINT_EVERY_N_WRITES == 0:
            try:
                with self._lock:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def _query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # -------------------------------------------------------------- sessions
    def create_session(
        self,
        session_id: str | None = None,
        source: str = "cli",
        title: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        db_ref: str | None = None,
        parent_session_id: str | None = None,
        context_length: int | None = None,
    ) -> str:
        session_id = session_id or new_id("sess")
        self._execute_write(
            """
            INSERT INTO sessions (id, source, title, model, provider, db_ref,
                                 parent_session_id, started_at, context_length)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                session_id, source, title, model, provider, db_ref,
                parent_session_id, now(), context_length,
            ),
        )
        return session_id

    def end_session(self, session_id: str, end_reason: str = "completed") -> None:
        self._execute_write(
            "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
            (now(), end_reason, session_id),
        )

    def reopen_session(self, session_id: str) -> None:
        self._execute_write(
            "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ?",
            (session_id,),
        )

    def get_session(self, session_id: str) -> dict | None:
        row = self._query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return dict(row) if row else None

    def list_sessions(self, limit: int = 50, source: str | None = None) -> list[dict]:
        if source:
            rows = self._query(
                """
                SELECT s.*,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id AND m.active = 1)
                           AS active_messages,
                       (SELECT substr(m.content, 1, 120) FROM messages m
                         WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                         ORDER BY m.timestamp, m.id LIMIT 1) AS preview
                FROM sessions s
                WHERE s.source = ?
                ORDER BY s.started_at DESC
                LIMIT ?
                """,
                (source, limit),
            )
        else:
            rows = self._query(
                """
                SELECT s.*,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id AND m.active = 1)
                           AS active_messages,
                       (SELECT substr(m.content, 1, 120) FROM messages m
                         WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                         ORDER BY m.timestamp, m.id LIMIT 1) AS preview
                FROM sessions s
                ORDER BY s.started_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        return [dict(r) for r in rows]

    def set_session_title(self, session_id: str, title: str) -> None:
        self._execute_write("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))

    def set_usage_anchor(self, session_id: str, anchor: dict) -> None:
        self._execute_write(
            "UPDATE sessions SET usage_anchor = ? WHERE id = ?",
            (canonical_json(anchor), session_id),
        )

    def update_session_usage(self, session_id: str, input_tokens: int, output_tokens: int) -> None:
        self._execute_write(
            """
            UPDATE sessions
               SET input_tokens = input_tokens + ?,
                   output_tokens = output_tokens + ?
             WHERE id = ?
            """,
            (input_tokens, output_tokens, session_id),
        )

    def bump_counters(self, session_id: str, messages: int = 0, tool_calls: int = 0) -> None:
        self._execute_write(
            """
            UPDATE sessions
               SET message_count = message_count + ?,
                   tool_call_count = tool_call_count + ?
             WHERE id = ?
            """,
            (messages, tool_calls, session_id),
        )

    # -------------------------------------------------------------- messages
    def append_message(
        self,
        session_id: str,
        role: str,
        content: str | None = None,
        tool_calls: list[dict] | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        reasoning: str | None = None,
        token_count: int | None = None,
        finish_reason: str | None = None,
        timestamp: float | None = None,
        message_uid: str | None = None,
    ) -> int:
        payload = canonical_json(tool_calls) if tool_calls else None
        uid = message_uid or new_id("m", 32).split("_", 1)[1]
        row_id = self._execute_write(
            """
            INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls,
                                  tool_name, reasoning, timestamp, token_count,
                                  finish_reason, message_uid)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session_id, role, content, tool_call_id, payload, tool_name,
                reasoning, timestamp if timestamp is not None else now(),
                token_count, finish_reason, uid,
            ),
        )
        return int(row_id)

    def get_messages(self, session_id: str, active_only: bool = True) -> list[dict]:
        clause = "AND active = 1" if active_only else ""
        rows = self._query(
            f"SELECT * FROM messages WHERE session_id = ? {clause} ORDER BY timestamp, id",
            (session_id,),
        )
        return [self._row_to_message(r) for r in rows]

    def get_messages_as_conversation(self, session_id: str, active_only: bool = True) -> list[dict]:
        """Project the transcript into the OpenAI message format used by providers."""
        conversation: list[dict] = []
        for message in self.get_messages(session_id, active_only=active_only):
            role = message["role"]
            if role == "tool":
                conversation.append({
                    "role": "tool",
                    "tool_call_id": message.get("tool_call_id") or "",
                    "content": message.get("content") or "",
                })
                continue
            entry: dict[str, Any] = {"role": role, "content": message.get("content") or ""}
            if message.get("tool_calls"):
                entry["tool_calls"] = message["tool_calls"]
            if message.get("reasoning"):
                entry["reasoning"] = message["reasoning"]
            conversation.append(entry)
        return conversation

    def archive_messages(self, session_id: str, message_ids: Iterable[int]) -> None:
        """Soft-archive rows (used by in-place compression)."""
        ids = [int(i) for i in message_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self._execute_write(
            f"UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ? AND id IN ({placeholders})",
            (session_id, *ids),
        )

    def replace_messages(self, session_id: str, messages: list[dict]) -> None:
        """Atomically replace the live transcript (in-place compression)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ? AND active = 1",
                    (session_id,),
                )
                for message in messages:
                    self._conn.execute(
                        """
                        INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls,
                                              tool_name, reasoning, timestamp, token_count,
                                              finish_reason, message_uid)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            session_id,
                            message["role"],
                            message.get("content"),
                            message.get("tool_call_id"),
                            canonical_json(message["tool_calls"]) if message.get("tool_calls") else None,
                            message.get("tool_name"),
                            message.get("reasoning"),
                            message.get("timestamp", now()),
                            message.get("token_count"),
                            message.get("finish_reason"),
                            message.get("message_uid") or new_id("m", 32).split("_", 1)[1],
                        ),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        self._after_write()

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> dict:
        message = dict(row)
        raw_calls = message.get("tool_calls")
        if raw_calls:
            try:
                message["tool_calls"] = json.loads(raw_calls)
            except json.JSONDecodeError:
                message["tool_calls"] = []
        else:
            message["tool_calls"] = None
        return message

    # ---------------------------------------------------------------- search
    def search_messages(
        self,
        query: str,
        limit: int = 20,
        source_filter: list[str] | None = None,
        role_filter: list[str] | None = None,
    ) -> list[dict]:
        sanitized = sanitize_fts_query(query)
        if not sanitized:
            return []
        sql = """
            SELECT m.id, m.session_id, m.role, m.timestamp, m.tool_name,
                   snippet(messages_fts, 0, '>>>', '<<<', '…', 12) AS snippet,
                   s.source, s.title AS session_title
              FROM messages_fts
              JOIN messages m ON m.id = messages_fts.rowid
              JOIN sessions s ON s.id = m.session_id
             WHERE messages_fts MATCH ?
        """
        params: list[Any] = [sanitized]
        if source_filter:
            sql += f" AND s.source IN ({','.join('?' for _ in source_filter)})"
            params.extend(source_filter)
        if role_filter:
            sql += f" AND m.role IN ({','.join('?' for _ in role_filter)})"
            params.extend(role_filter)
        sql += " ORDER BY m.timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self._query(sql, tuple(params))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- artifacts
    def create_artifact(
        self,
        session_id: str,
        kind: str,
        payload: dict | list,
        title: str | None = None,
        meta: dict | None = None,
        artifact_id: str | None = None,
    ) -> str:
        artifact_id = artifact_id or new_id({"table": "tbl", "chart": "cht",
                                             "dashboard": "dash"}.get(kind, "art"))
        timestamp = now()
        self._execute_write(
            """
            INSERT INTO artifacts (id, session_id, kind, title, payload, meta, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                artifact_id, session_id, kind, title,
                canonical_json(payload), canonical_json(meta) if meta else None,
                timestamp, timestamp,
            ),
        )
        return artifact_id

    def update_artifact(
        self,
        artifact_id: str,
        payload: dict | list | None = None,
        title: str | None = None,
        meta: dict | None = None,
    ) -> None:
        sets = ["updated_at = ?"]
        params: list[Any] = [now()]
        if payload is not None:
            sets.append("payload = ?")
            params.append(canonical_json(payload))
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if meta is not None:
            sets.append("meta = ?")
            params.append(canonical_json(meta))
        params.append(artifact_id)
        self._execute_write(
            f"UPDATE artifacts SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )

    def get_artifact(self, artifact_id: str) -> dict | None:
        row = self._query_one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        if not row:
            return None
        artifact = dict(row)
        for field in ("payload", "meta"):
            raw = artifact.get(field)
            if raw:
                try:
                    artifact[field] = json.loads(raw)
                except json.JSONDecodeError:
                    artifact[field] = None
            else:
                artifact[field] = None
        return artifact

    def list_artifacts(self, session_id: str, kind: str | None = None) -> list[dict]:
        if kind:
            rows = self._query(
                "SELECT id, session_id, kind, title, meta, created_at, updated_at "
                "FROM artifacts WHERE session_id = ? AND kind = ? ORDER BY created_at",
                (session_id, kind),
            )
        else:
            rows = self._query(
                "SELECT id, session_id, kind, title, meta, created_at, updated_at "
                "FROM artifacts WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            )
        artifacts = []
        for row in rows:
            artifact = dict(row)
            if artifact.get("meta"):
                try:
                    artifact["meta"] = json.loads(artifact["meta"])
                except json.JSONDecodeError:
                    artifact["meta"] = None
            artifacts.append(artifact)
        return artifacts

    def delete_artifact(self, artifact_id: str) -> None:
        self._execute_write("DELETE FROM artifacts WHERE id = ?", (artifact_id,))

    # ----------------------------------------------------------------- audit
    def append_audit(self, session_id: str | None, event: str, detail: dict) -> str:
        """Append to the hash-chained audit log. Returns the new entry hash."""
        with self._lock:
            row = self._query_one("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1")
            prev_hash = row["hash"] if row else "genesis"
            payload = canonical_json({"session_id": session_id, "event": event, "detail": detail})
            digest = sha256_hex(prev_hash + payload)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    INSERT INTO audit_log (session_id, ts, event, detail, prev_hash, hash)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (session_id, now(), event, canonical_json(detail), prev_hash, digest),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        self._after_write()
        return digest

    def list_audit(self, session_id: str | None = None, limit: int = 500) -> list[dict]:
        if session_id:
            rows = self._query(
                "SELECT * FROM audit_log WHERE session_id = ? ORDER BY id LIMIT ?",
                (session_id, limit),
            )
        else:
            rows = self._query("SELECT * FROM audit_log ORDER BY id LIMIT ?", (limit,))
        entries = []
        for row in rows:
            entry = dict(row)
            try:
                entry["detail"] = json.loads(entry["detail"])
            except (json.JSONDecodeError, TypeError):
                pass
            entries.append(entry)
        return entries

    def verify_audit_chain(self) -> dict:
        """Walk the audit chain and report the first broken link, if any."""
        rows = self._query("SELECT * FROM audit_log ORDER BY id")
        prev_hash = "genesis"
        for index, row in enumerate(rows):
            detail = row["detail"]
            payload = canonical_json({
                "session_id": row["session_id"],
                "event": row["event"],
                "detail": json.loads(detail) if detail else None,
            })
            expected = sha256_hex(prev_hash + payload)
            if row["prev_hash"] != prev_hash or row["hash"] != expected:
                return {
                    "valid": False,
                    "checked": index,
                    "broken_at_id": row["id"],
                    "expected_hash": expected,
                    "stored_hash": row["hash"],
                }
            prev_hash = row["hash"]
        return {"valid": True, "checked": len(rows), "head": prev_hash}


def sanitize_fts_query(query: str) -> str:
    """Make a user query safe for FTS5 MATCH.

    Strips unmatched quotes, quotes hyphenated terms, and removes dangling boolean
    operators, all of which would otherwise raise a syntax error at query time.
    """
    if not query:
        return ""
    text = query.strip()
    if text.count('"') % 2 == 1:
        text = text.replace('"', " ")
    text = text.replace("'", " ")
    tokens: list[str] = []
    for raw_token in text.split():
        token = raw_token.strip()
        if not token:
            continue
        upper = token.upper()
        if upper in ("AND", "OR", "NOT"):
            tokens.append(token)
            continue
        if "-" in token or "." in token or ":" in token:
            token = f'"{token}"'
        tokens.append(token)
    # Remove a dangling boolean operator at either end.
    while tokens and tokens[0].upper() in ("AND", "OR", "NOT"):
        tokens.pop(0)
    while tokens and tokens[-1].upper() in ("AND", "OR", "NOT"):
        tokens.pop()
    return " ".join(tokens)

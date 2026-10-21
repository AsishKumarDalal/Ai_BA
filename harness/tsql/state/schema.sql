-- text_sql harness state schema.
-- Applied by tsql/state/store.py. Idempotent: every statement is IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS sessions (
    id                  TEXT PRIMARY KEY,
    source              TEXT NOT NULL DEFAULT 'cli',
    title               TEXT,
    model               TEXT,
    provider            TEXT,
    db_ref              TEXT,
    parent_session_id   TEXT,
    started_at          REAL NOT NULL,
    ended_at            REAL,
    end_reason          TEXT,
    message_count       INTEGER NOT NULL DEFAULT 0,
    tool_call_count     INTEGER NOT NULL DEFAULT 0,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    context_length      INTEGER,
    usage_anchor        TEXT,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title ON sessions(title) WHERE title IS NOT NULL;

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    role            TEXT NOT NULL,
    content         TEXT,
    tool_call_id    TEXT,
    tool_calls      TEXT,
    tool_name       TEXT,
    reasoning       TEXT,
    timestamp       REAL NOT NULL,
    token_count     INTEGER,
    finish_reason   TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    compacted       INTEGER NOT NULL DEFAULT 0,
    message_uid     TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_uid ON messages(message_uid);

-- External-content FTS5 index over the transcript.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    tool_name,
    tool_calls,
    content='messages',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content, tool_name, tool_calls)
    VALUES (new.id, new.content, new.tool_name, new.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, tool_name, tool_calls)
    VALUES ('delete', old.id, old.content, old.tool_name, old.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, tool_name, tool_calls)
    VALUES ('delete', old.id, old.content, old.tool_name, old.tool_calls);
    INSERT INTO messages_fts(rowid, content, tool_name, tool_calls)
    VALUES (new.id, new.content, new.tool_name, new.tool_calls);
END;

-- Durable objects: SQL results, charts, dashboards, notes.
CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    kind        TEXT NOT NULL,
    title       TEXT,
    payload     TEXT NOT NULL,
    meta        TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(session_id, kind);

-- Hash-chained audit trail. hash = sha256(prev_hash || canonical_json(detail)).
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT,
    ts          REAL NOT NULL,
    event       TEXT NOT NULL,
    detail      TEXT NOT NULL,
    prev_hash   TEXT,
    hash        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id, id);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

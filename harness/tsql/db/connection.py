"""Read-only database connections.

The read-only guarantee is enforced **here**, at the connection layer, not in
a prompt:

* SQLite  — opened with ``file:...?mode=ro`` plus ``PRAGMA query_only=ON``.
* DuckDB  — opened with ``read_only=True``.
* Postgres — ``SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`` and a
  statement timeout.
* MySQL   — ``SET SESSION TRANSACTION READ ONLY`` and a max execution time.

A prompt saying "only run SELECT" is a request. These are permissions.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..config import DBConfig
from ..errors import DatabaseError


@dataclass
class ColumnInfo:
    name: str
    type: str = ""
    nullable: bool = True
    is_pk: bool = False
    is_fk: bool = False
    fk_target: str | None = None
    default: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "nullable": self.nullable,
            "is_pk": self.is_pk,
            "is_fk": self.is_fk,
            "fk_target": self.fk_target,
        }


@dataclass
class QueryResult:
    columns: list[ColumnInfo] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def to_dict(self, include_rows: bool = True) -> dict:
        payload: dict[str, Any] = {
            "columns": [c.to_dict() for c in self.columns],
            "row_count": self.row_count,
            "truncated": self.truncated,
            "elapsed_ms": self.elapsed_ms,
        }
        if include_rows:
            payload["rows"] = self.rows
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass
class ConnectionInfo:
    dialect: str
    label: str
    read_only: bool = True
    server_version: str = ""
    tables: int = 0

    def to_dict(self) -> dict:
        return {
            "dialect": self.dialect,
            "label": self.label,
            "read_only": self.read_only,
            "server_version": self.server_version,
            "tables": self.tables,
        }


class Database(ABC):
    """A read-only connection to a SQL database."""

    dialect: str = "unknown"

    def __init__(self, config: DBConfig):
        self.config = config
        self._read_only = True

    # ------------------------------------------------------------- execution
    @abstractmethod
    def execute(self, sql: str, max_rows: int | None = None) -> QueryResult:
        """Run a statement and return at most ``max_rows`` rows."""

    @abstractmethod
    def list_tables(self) -> list[dict]:
        """Tables and views, with a row estimate when one is cheap to get."""

    @abstractmethod
    def describe_table(self, table: str) -> dict:
        """Columns, types, keys, and per-column statistics."""

    @abstractmethod
    def sample_table(self, table: str, limit: int = 20) -> dict:
        """A small sample of rows."""

    @abstractmethod
    def profile_table(
        self,
        table: str,
        columns: Sequence[str] | None = None,
        top_k: int = 5,
        max_rows_scanned: int = 50_000,
    ) -> dict:
        """Distributional statistics for the table's columns."""

    @abstractmethod
    def close(self) -> None:
        ...

    @property
    def info(self) -> ConnectionInfo:
        return ConnectionInfo(dialect=self.dialect, label=self.config.path or self.dialect)

    @property
    def read_only(self) -> bool:
        return self._read_only

    # ---------------------------------------------------------------- helper
    def _timed(self, fn) -> QueryResult:
        """Run ``fn``, capturing any exception as a query error."""
        started = time.monotonic()
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - surfaced to the model
            return QueryResult(error=f"{type(exc).__name__}: {exc}")
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    def _timed_value(self, fn, default):
        """Run ``fn`` for a non-QueryResult value, swallowing failures."""
        try:
            return fn()
        except Exception:  # noqa: BLE001 - best effort
            return default


# ------------------------------------------------------------------- SQLite


class SqliteDatabase(Database):
    dialect = "sqlite"

    def __init__(self, config: DBConfig):
        super().__init__(config)
        import sqlite3

        path = config.path
        if not path:
            raise DatabaseError("a SQLite database path is required", code="missing_path")

        if path == ":memory:":
            self._conn = sqlite3.connect(":memory:")
        else:
            # mode=ro is enforced by SQLite itself, not by us.
            uri = f"file:{path}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA query_only=ON")
        except sqlite3.Error:
            pass
        self._read_only = True

    def execute(self, sql: str, max_rows: int | None = None) -> QueryResult:
        def run() -> QueryResult:
            cursor = self._conn.execute(sql)
            rows = cursor.fetchmany(max_rows) if max_rows else cursor.fetchall()
            columns = [ColumnInfo(name=d[0], type=str(d[1] or "")) for d in (cursor.description or [])]
            return QueryResult(columns=columns, rows=[list(r) for r in rows], row_count=len(rows))

        return self._timed(run)

    def list_tables(self) -> list[dict]:
        def run() -> list[dict]:
            cursor = self._conn.execute(
                "SELECT name, type FROM sqlite_master "
                "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
            tables = []
            for row in cursor.fetchall():
                tables.append({
                    "name": row["name"],
                    "kind": row["type"],
                    "row_estimate": self._row_estimate(row["name"]),
                })
            return tables

        return self._timed_value(run, [])

    def _row_estimate(self, table: str) -> int:
        try:
            row = self._conn.execute(f'SELECT COUNT(*) AS n FROM "{table}"').fetchone()
            return int(row["n"])
        except Exception:  # noqa: BLE001 - an estimate is best effort
            return 0

    def describe_table(self, table: str) -> dict:
        def run() -> dict:
            info = self._conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            if not info:
                raise DatabaseError(f"table not found: {table}", code="unknown_table")
            fks = self._conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
            pk_columns = {row["name"] for row in info if row["pk"]}
            fk_by_column = {row["from"]: f"{row['table']}.{row['to'] or 'id'}" for row in fks}
            columns = []
            for row in info:
                name = row["name"]
                columns.append(ColumnInfo(
                    name=name,
                    type=str(row["type"] or ""),
                    nullable=not bool(row["notnull"]),
                    is_pk=name in pk_columns,
                    is_fk=name in fk_by_column,
                    fk_target=fk_by_column.get(name),
                    default=row["dflt_value"],
                ))
            return {
                "table": table,
                "kind": self._table_kind(table),
                "columns": [c.to_dict() for c in columns],
                "primary_key": [c.name for c in columns if c.is_pk],
                "foreign_keys": [
                    {"column": c.name, "references": c.fk_target} for c in columns if c.is_fk
                ],
            }

        outcome = self._timed_value(run, None)
        if outcome is None:
            raise DatabaseError(f"could not describe {table}", code="describe_failed")
        return outcome

    def _table_kind(self, table: str) -> str:
        row = self._conn.execute(
            "SELECT type FROM sqlite_master WHERE name = ?", (table,)
        ).fetchone()
        return row["type"] if row else "table"

    def sample_table(self, table: str, limit: int = 20) -> dict:
        result = self.execute(f'SELECT * FROM "{table}" LIMIT {int(limit)}')
        if result.error:
            raise DatabaseError(result.error, code="sample_failed")
        return result.to_dict()

    def profile_table(self, table, columns=None, top_k=5, max_rows_scanned=50_000) -> dict:
        return _generic_profile(self, table, columns, top_k, max_rows_scanned)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------- DuckDB


class DuckDbDatabase(Database):
    dialect = "duckdb"

    def __init__(self, config: DBConfig):
        super().__init__(config)
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise DatabaseError(
                "the 'duckdb' package is required for DuckDB connections",
                code="missing_dependency",
            ) from exc
        if not config.path:
            raise DatabaseError("a DuckDB database path is required", code="missing_path")
        self._conn = duckdb.connect(config.path, read_only=True)
        self._read_only = True

    def execute(self, sql: str, max_rows: int | None = None) -> QueryResult:
        def run() -> QueryResult:
            cursor = self._conn.execute(sql)
            rows = cursor.fetchmany(max_rows) if max_rows else cursor.fetchall()
            names = [d[0] for d in (cursor.description or [])]
            types = [str(d[1]) for d in (cursor.description or [])]
            columns = [ColumnInfo(name=n, type=t) for n, t in zip(names, types)]
            return QueryResult(columns=columns, rows=[list(r) for r in rows], row_count=len(rows))

        return self._timed(run)

    def list_tables(self) -> list[dict]:
        def run() -> list[dict]:
            cursor = self._conn.execute(
                "SELECT table_name, estimated_size FROM duckdb_tables() ORDER BY table_name"
            )
            rows = cursor.fetchall()
            views = {
                r[0] for r in self._conn.execute(
                    "SELECT view_name FROM duckdb_views() WHERE NOT internal"
                ).fetchall()
            }
            return [
                {
                    "name": r[0],
                    "kind": "view" if r[0] in views else "table",
                    "row_estimate": int(r[1] or 0),
                }
                for r in rows
            ]

        result = self._timed(run)
        if result.error:
            return []
        return result.rows

    def describe_table(self, table: str) -> dict:
        def run() -> dict:
            cursor = self._conn.execute(f'DESCRIBE "{table}"')
            names = [d[0] for d in cursor.description]
            rows = [dict(zip(names, r)) for r in cursor.fetchall()]
            if not rows:
                raise DatabaseError(f"table not found: {table}", code="unknown_table")
            columns = [
                ColumnInfo(name=r["column_name"], type=str(r.get("column_type") or ""),
                           nullable=str(r.get("null", "YES")).upper() == "YES")
                for r in rows
            ]
            return {
                "table": table,
                "kind": "table",
                "columns": [c.to_dict() for c in columns],
                "primary_key": [],
                "foreign_keys": [],
            }

        outcome = self._timed_value(run, None)
        if outcome is None:
            raise DatabaseError(f"could not describe {table}", code="describe_failed")
        return outcome

    def sample_table(self, table: str, limit: int = 20) -> dict:
        result = self.execute(f'SELECT * FROM "{table}" LIMIT {int(limit)}')
        if result.error:
            raise DatabaseError(result.error, code="sample_failed")
        return result.to_dict()

    def profile_table(self, table, columns=None, top_k=5, max_rows_scanned=50_000) -> dict:
        return _generic_profile(self, table, columns, top_k, max_rows_scanned)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass


# ----------------------------------------------------------------- Postgres


class PostgresDatabase(Database):
    dialect = "postgres"

    def __init__(self, config: DBConfig):
        super().__init__(config)
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise DatabaseError(
                "the 'psycopg' package is required for PostgreSQL connections",
                code="missing_dependency",
            ) from exc
        self._conn = psycopg.connect(
            host=config.host, port=config.port, user=config.user,
            password=config.password, dbname=config.database,
        )
        self._conn.autocommit = True
        with self._conn.cursor() as cursor:
            cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            cursor.execute(f"SET statement_timeout = {int(config.statement_timeout_seconds) * 1000}")
        self._read_only = True

    def execute(self, sql: str, max_rows: int | None = None) -> QueryResult:
        def run() -> QueryResult:
            with self._conn.cursor() as cursor:
                cursor.execute(sql)
                if cursor.description is None:
                    return QueryResult(row_count=0)
                names = [d.name for d in cursor.description]
                types = [str(getattr(d, "type_code", "") or "") for d in cursor.description]
                columns = [ColumnInfo(name=n, type=t) for n, t in zip(names, types)]
                rows = cursor.fetchmany(max_rows) if max_rows else cursor.fetchall()
                return QueryResult(columns=columns, rows=[list(r) for r in rows],
                                   row_count=len(rows))

        return self._timed(run)

    def list_tables(self) -> list[dict]:
        def run() -> list[dict]:
            with self._conn.cursor() as cursor:
                cursor.execute(
                    "SELECT table_name, table_type FROM information_schema.tables "
                    "WHERE table_schema = 'public' ORDER BY table_name"
                )
                return [
                    {"name": r[0], "kind": "view" if r[1] == "VIEW" else "table",
                     "row_estimate": 0}
                    for r in cursor.fetchall()
                ]

        return self._timed_value(run, [])

    def describe_table(self, table: str) -> dict:
        def run() -> dict:
            with self._conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT column_name, data_type, is_nullable, column_default
                      FROM information_schema.columns
                     WHERE table_name = %s AND table_schema = 'public'
                     ORDER BY ordinal_position
                    """,
                    (table,),
                )
                columns = [
                    ColumnInfo(name=r[0], type=str(r[1]), nullable=(r[2] == "YES"),
                               default=r[3])
                    for r in cursor.fetchall()
                ]
                if not columns:
                    raise DatabaseError(f"table not found: {table}", code="unknown_table")
                return {
                    "table": table, "kind": "table",
                    "columns": [c.to_dict() for c in columns],
                    "primary_key": [], "foreign_keys": [],
                }

        outcome = self._timed_value(run, None)
        if outcome is None:
            raise DatabaseError(f"could not describe {table}", code="describe_failed")
        return outcome

    def sample_table(self, table: str, limit: int = 20) -> dict:
        result = self.execute(f'SELECT * FROM "{table}" LIMIT {int(limit)}')
        if result.error:
            raise DatabaseError(result.error, code="sample_failed")
        return result.to_dict()

    def profile_table(self, table, columns=None, top_k=5, max_rows_scanned=50_000) -> dict:
        return _generic_profile(self, table, columns, top_k, max_rows_scanned)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass


# -------------------------------------------------------------------- MySQL


class MySqlDatabase(Database):
    dialect = "mysql"

    def __init__(self, config: DBConfig):
        super().__init__(config)
        try:
            import pymysql
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise DatabaseError(
                "the 'pymysql' package is required for MySQL connections",
                code="missing_dependency",
            ) from exc
        self._conn = pymysql.connect(
            host=config.host, port=config.port or 3306, user=config.user,
            password=config.password, database=config.database, autocommit=True,
        )
        with self._conn.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute(
                f"SET SESSION max_execution_time = {int(config.statement_timeout_seconds) * 1000}"
            )
        self._read_only = True

    def execute(self, sql: str, max_rows: int | None = None) -> QueryResult:
        def run() -> QueryResult:
            with self._conn.cursor() as cursor:
                cursor.execute(sql)
                if cursor.description is None:
                    return QueryResult(row_count=0)
                names = [d[0] for d in cursor.description]
                types = [str(d[1] or "") for d in cursor.description]
                columns = [ColumnInfo(name=n, type=t) for n, t in zip(names, types)]
                rows = cursor.fetchmany(max_rows) if max_rows else cursor.fetchall()
                return QueryResult(columns=columns, rows=[list(r) for r in rows],
                                   row_count=len(rows))

        return self._timed(run)

    def list_tables(self) -> list[dict]:
        def run() -> list[dict]:
            with self._conn.cursor() as cursor:
                cursor.execute(
                    "SELECT table_name, table_type FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() ORDER BY table_name"
                )
                return [
                    {"name": r[0], "kind": "view" if r[1] == "VIEW" else "table",
                     "row_estimate": 0}
                    for r in cursor.fetchall()
                ]

        return self._timed_value(run, [])

    def describe_table(self, table: str) -> dict:
        def run() -> dict:
            with self._conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT column_name, data_type, is_nullable, column_key
                      FROM information_schema.columns
                     WHERE table_name = %s AND table_schema = DATABASE()
                     ORDER BY ordinal_position
                    """,
                    (table,),
                )
                rows = cursor.fetchall()
                if not rows:
                    raise DatabaseError(f"table not found: {table}", code="unknown_table")
                columns = [
                    ColumnInfo(name=r[0], type=str(r[1]), nullable=(r[2] == "YES"),
                               is_pk=(r[3] == "PRI"))
                    for r in rows
                ]
                return {
                    "table": table, "kind": "table",
                    "columns": [c.to_dict() for c in columns],
                    "primary_key": [c.name for c in columns if c.is_pk],
                    "foreign_keys": [],
                }

        outcome = self._timed_value(run, None)
        if outcome is None:
            raise DatabaseError(f"could not describe {table}", code="describe_failed")
        return outcome

    def sample_table(self, table: str, limit: int = 20) -> dict:
        result = self.execute(f'SELECT * FROM `{table}` LIMIT {int(limit)}')
        if result.error:
            raise DatabaseError(result.error, code="sample_failed")
        return result.to_dict()

    def profile_table(self, table, columns=None, top_k=5, max_rows_scanned=50_000) -> dict:
        return _generic_profile(self, table, columns, top_k, max_rows_scanned)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ factory


def connect_database(config: DBConfig) -> Database:
    """Open a read-only connection for the configured dialect."""
    dialect = (config.dialect or "sqlite").lower()
    if dialect == "sqlite":
        return SqliteDatabase(config)
    if dialect == "duckdb":
        return DuckDbDatabase(config)
    if dialect in ("postgres", "postgresql"):
        return PostgresDatabase(config)
    if dialect == "mysql":
        return MySqlDatabase(config)
    raise DatabaseError(f"unsupported dialect: {config.dialect}", code="unsupported_dialect")


# ----------------------------------------------------------------- profiling


def _generic_profile(
    db: Database,
    table: str,
    columns: Sequence[str] | None,
    top_k: int,
    max_rows_scanned: int,
) -> dict:
    """Column statistics, computed over a bounded sample of the table."""
    described = db.describe_table(table)
    available = [c["name"] for c in described.get("columns", [])]
    targets = [c for c in (columns or available) if c in available]
    if not targets:
        raise DatabaseError(f"no matching columns in {table}", code="unknown_column")

    # Bound the scan: every statistic is computed over at most N rows.
    sample = f'(SELECT * FROM "{table}" LIMIT {int(max_rows_scanned)})'

    stats: dict[str, dict] = {}
    for column in targets:
        quoted = f'"{column}"'
        try:
            row = db.execute(
                f"SELECT COUNT(*) AS n, COUNT({quoted}) AS non_null, "
                f"COUNT(DISTINCT {quoted}) AS distinct_count, "
                f"MIN({quoted}) AS min_value, MAX({quoted}) AS max_value, "
                f"AVG(CAST({quoted} AS REAL)) AS avg_value "
                f"FROM {sample} AS t"
            )
        except Exception as exc:  # noqa: BLE001 - one bad column must not kill the profile
            stats[column] = {"error": str(exc)}
            continue
        if row.error or not row.rows:
            stats[column] = {"error": row.error or "no rows"}
            continue
        n, non_null, distinct_count, min_value, max_value, avg_value = row.rows[0]
        entry: dict[str, Any] = {
            "rows_sampled": int(n or 0),
            "null_pct": round(100.0 * (1 - (non_null / n)) , 2) if n else None,
            "distinct": int(distinct_count or 0),
            "min": min_value,
            "max": max_value,
            "avg": avg_value,
        }
        try:
            top = db.execute(
                f"SELECT {quoted} AS value, COUNT(*) AS n FROM {sample} AS t "
                f"WHERE {quoted} IS NOT NULL GROUP BY {quoted} "
                f"ORDER BY n DESC LIMIT {int(top_k)}"
            )
            if not top.error:
                entry["top_values"] = [{"value": r[0], "count": r[1]} for r in top.rows]
        except Exception:  # noqa: BLE001
            pass
        stats[column] = entry

    return {
        "table": table,
        "columns": stats,
        "row_count_estimate": described.get("row_estimate", 0),
    }

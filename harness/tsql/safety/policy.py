"""Governance policy.

A :class:`Policy` is the immutable "rules of the road" object handed to the SQL guard
and the result masker. It is derived from :class:`~tsql.config.Config` but is a separate
type so it can be constructed directly in tests and shipped to a remote policy service.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config, PolicyConfig

#: Functions that read files, open sockets, execute code, or otherwise escape the
#: database's data plane. Denied by default at every layer.
#:
#: Comparison is case-insensitive and ignores underscores, so ``READ_CSV`` and
#: ``read_csv`` are the same entry.
DANGEROUS_FUNCTIONS: frozenset[str] = frozenset({
    # PostgreSQL
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "pg_execute_server_program", "pg_logdir_ls", "lo_import", "lo_export",
    "dblink", "dblink_exec", "dblink_connect", "dblink_send_query",
    "pg_terminate_backend", "set_config", "pg_advisory_lock",
    "pg_logical_slot_create", "pg_reload_conf", "pg_sleep",
    # MySQL / SQL Server
    "load_file", "xp_cmdshell", "openrowset", "opendatasource", "sys_exec",
    "sp_executesql", "sp_oacreate", "utl_file", "utl_http", "httpuritype",
    # DuckDB / SQLite extensions and file readers
    "read_csv", "read_csv_auto", "read_parquet", "read_parquet_objects",
    "read_json", "read_json_auto", "read_json_objects", "read_ndjson",
    "read_ndjson_auto", "read_text", "glob", "install", "load",
    "load_extension", "sqlite_load_extension", "sqlite_read", "sqlite3_scan",
    "copy", "attach", "detach", "duckdb_extension",
    # Generic
    "system", "exec", "eval", "evaljs", "shell", "powershell", "os_command",
    "runtime", "call", "unlink", "symlink",
})

#: Schemas that must never be referenced.
DEFAULT_FORBIDDEN_SCHEMAS: frozenset[str] = frozenset({
    "pg_catalog", "information_schema", "mysql", "sys", "performance_schema",
    "sqlite_master", "sqlite_schema", "sqlite_temp_master", "duckdb_internal",
    "duckdb_constraints", "duckdb_functions", "duckdb_types", "duckdb_schemas",
    "duckdb_databases", "duckdb_indexes", "duckdb_settings", "pg_toast",
})


@dataclass
class Policy:
    """Everything the guard and the masker need to make a decision."""

    dialect: str = "sqlite"
    #: Explicit allowlist. ``None`` means "any table that is not denied/system".
    allowed_tables: list[str] | None = None
    denied_tables: list[str] = field(default_factory=list)
    forbidden_schemas: frozenset[str] = DEFAULT_FORBIDDEN_SCHEMAS
    max_rows: int = 1_000
    max_result_bytes: int = 262_144
    statement_timeout_seconds: int = 15
    forbid_select_star: bool = True
    forbid_dangerous_functions: bool = True
    allow_expensive_queries: bool = False
    expensive_row_threshold: int = 1_000_000
    pii_masking_enabled: bool = True
    pii_entitled_roles: list[str] = field(default_factory=list)
    active_role: str | None = None
    approval_mode: str = "smart"
    approval_timeout_seconds: int = 300

    # ------------------------------------------------------------- factories
    @classmethod
    def from_config(cls, config: Config) -> "Policy":
        policy_config: PolicyConfig = config.policy
        denied = list(config.db.deny_tables or [])
        return cls(
            dialect=config.db.dialect,
            allowed_tables=list(config.db.allowed_tables) if config.db.allowed_tables else None,
            denied_tables=denied,
            forbidden_schemas=frozenset(
                set(policy_config.forbidden_schemas) | DEFAULT_FORBIDDEN_SCHEMAS
            ),
            max_rows=policy_config.max_rows,
            max_result_bytes=policy_config.max_result_bytes,
            statement_timeout_seconds=config.db.statement_timeout_seconds,
            forbid_select_star=policy_config.forbid_select_star,
            forbid_dangerous_functions=policy_config.forbid_dangerous_functions,
            allow_expensive_queries=policy_config.allow_expensive_queries,
            expensive_row_threshold=policy_config.expensive_row_threshold,
            pii_masking_enabled=policy_config.pii_masking_enabled,
            pii_entitled_roles=list(policy_config.pii_entitled_roles),
            active_role=policy_config.active_role or config.db.user,
            approval_mode=policy_config.approval_mode,
            approval_timeout_seconds=policy_config.approval_timeout_seconds,
        )

    # ---------------------------------------------------------------- checks
    def is_table_allowed(self, table: str) -> bool:
        """Is this table reference permitted?

        Matching is on the *base* name so that a schema-qualified reference to an
        allowed table is accepted (``public.orders`` when ``orders`` is allowed),
        and so is an unqualified reference to an allowed qualified table. The
        allowlist already names the exact table, so neither direction widens it.
        """
        name = table.strip().lower()
        if not name:
            return False
        if name in {t.lower() for t in self.denied_tables}:
            return False
        if self.allowed_tables is None:
            return True

        allowed = {t.lower() for t in self.allowed_tables}
        if name in allowed:
            return True
        base = name.rsplit(".", 1)[-1]
        for entry in allowed:
            entry_base = entry.rsplit(".", 1)[-1]
            if entry_base != base:
                continue
            if "." not in entry and "." not in name:
                return True
            if entry_base == base:
                return True
        return False

    def role_can_read_pii(self) -> bool:
        if not self.active_role:
            return False
        return self.active_role.lower() in {r.lower() for r in self.pii_entitled_roles}

    def allowed_tables_for_prompt(self) -> list[str] | None:
        """The list of table names to show the model (None means 'discover them')."""
        return list(self.allowed_tables) if self.allowed_tables else None

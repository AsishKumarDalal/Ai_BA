"""Configuration model.

Precedence (lowest to highest):
    built-in defaults  ->  config file (JSON, or YAML when PyYAML is present)
                       ->  environment variables  ->  explicit constructor kwargs

Nothing here reaches the network or the filesystem at import time.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError

# --------------------------------------------------------------------- constants

DEFAULT_STATE_DB = "~/.text_sql/state.db"

#: Known context windows, keyed by a substring of the model id. Longest match wins.
KNOWN_CONTEXT_LENGTHS: dict[str, int] = {
    "gpt-4.1": 1_047_576,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-5": 400_000,
    "o3": 200_000,
    "o1": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-7-sonnet": 200_000,
    "claude-sonnet-4": 1_000_000,
    "claude-opus-4": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-haiku": 200_000,
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.0-flash": 1_048_576,
    "llama-3.3": 128_000,
    "llama-3.1": 128_000,
    "qwen": 32_768,
    "deepseek-chat": 128_000,
    "deepseek-reasoner": 128_000,
    "kimi": 256_000,
    "minimax": 1_000_000,
}


def guess_context_length(model: str, default: int = 128_000) -> int:
    """Best-effort context window for a model id."""
    if not model:
        return default
    lowered = model.lower()
    best_len = 0
    best = default
    for key, length in KNOWN_CONTEXT_LENGTHS.items():
        if key in lowered and len(key) > best_len:
            best_len = len(key)
            best = length
    return best


# ------------------------------------------------------------------- dataclasses


@dataclass
class ModelConfig:
    provider: str = "openai"
    model: str = ""
    api_key: str | None = None
    base_url: str | None = None
    #: "chat_completions" | "anthropic_messages"; None means auto-detect.
    api_mode: str | None = None
    context_length: int | None = None
    max_tokens: int = 4096
    temperature: float = 0.0
    timeout_seconds: float = 120.0
    #: Extended-thinking effort for reasoning models: "none" | "low" | "medium" | "high".
    #: "none" switches thinking off on recent Ollama builds, which is a large
    #: saving when the model does not need to deliberate.
    reasoning_effort: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)

    def resolved_context_length(self) -> int:
        if self.context_length:
            return int(self.context_length)
        return guess_context_length(self.model)


@dataclass
class DBConfig:
    #: sqlite | duckdb | postgres | mysql
    dialect: str = "sqlite"
    path: str | None = None
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    database: str | None = None
    #: Explicit allowlist of tables/views the agent may touch. None means "all non-system".
    allowed_tables: list[str] | None = None
    #: Extra tables that are always refused.
    deny_tables: list[str] = field(default_factory=list)
    max_rows_scanned: int = 50_000
    statement_timeout_seconds: int = 15
    #: Rows returned inline in the tool response. Larger results become handle + preview.
    inline_row_limit: int = 50
    max_result_bytes: int = 262_144
    #: Cache TTL for the introspected catalog.
    catalog_ttl_seconds: float = 300.0


@dataclass
class PolicyConfig:
    """Governance rules applied to every statement before execution."""

    max_rows: int = 1_000
    max_result_bytes: int = 262_144
    statement_timeout_seconds: int = 15
    #: Allow queries the cost estimator flags as expensive.
    allow_expensive_queries: bool = False
    #: Row estimate above which a query is considered expensive.
    expensive_row_threshold: int = 1_000_000
    #: Mask PII columns and values.
    pii_masking_enabled: bool = True
    #: Role names entitled to see unmasked PII. The active role comes from DBConfig.user
    #: or an explicit override.
    pii_entitled_roles: list[str] = field(default_factory=list)
    active_role: str | None = None
    #: smart | manual | off
    approval_mode: str = "smart"
    approval_timeout_seconds: int = 300
    #: Reject SELECT * against a bare table (explicit columns required).
    forbid_select_star: bool = True
    #: Block statements that call file/system/network functions.
    forbid_dangerous_functions: bool = True
    #: System schemas that may never be referenced.
    forbidden_schemas: list[str] = field(
        default_factory=lambda: [
            "pg_catalog", "information_schema", "mysql", "sys",
            "performance_schema", "sqlite_master", "sqlite_schema",
            "duckdb_internal", "duckdb_constraints", "duckdb_functions",
        ]
    )


@dataclass
class CompressionConfig:
    enabled: bool = True
    #: In-loop trigger, as a fraction of the context window.
    threshold: float = 0.50
    #: Pre-agent hygiene trigger (safety net for sessions that grew between turns).
    hygiene_threshold: float = 0.85
    protect_first_n: int = 3
    protect_last_n: int = 8
    min_tail_user_messages: int = 1
    #: How many of the most recent tool results stay verbatim in the tail.
    keep_recent_tool_results: int = 2
    #: Tail budget as a fraction of the context window, clamped by the bounds below.
    tail_ratio: float = 0.25
    tail_min_tokens: int = 10_000
    tail_max_tokens: int = 25_000
    #: Upper bound on the generated summary.
    summary_max_tokens: int = 12_000
    summary_min_tokens: int = 2_000
    #: Per-model threshold overrides (substring match, longest wins).
    model_thresholds: dict[str, float] = field(default_factory=dict)

    def threshold_for(self, model: str) -> float:
        best_len = 0
        value = self.threshold
        for key, override in self.model_thresholds.items():
            if key and key.lower() in (model or "").lower() and len(key) > best_len:
                best_len = len(key)
                value = override
        return value


@dataclass
class AgentConfig:
    max_iterations: int = 60
    parallel_tools: bool = True
    max_parallel_tools: int = 4
    enabled_toolsets: list[str] | None = None
    disabled_toolsets: list[str] = field(default_factory=list)
    system_prompt_extra: str = ""
    #: Approval callback: (tool_name, args, reason) -> bool
    auto_approve_low_risk: bool = False


@dataclass
class Config:
    model: ModelConfig = field(default_factory=ModelConfig)
    db: DBConfig = field(default_factory=DBConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    compression: CompressionConfig = field(default_factory=CompressionConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    state_db_path: str = DEFAULT_STATE_DB
    #: Business term -> table/column mapping, surfaced to the agent as a glossary.
    glossary: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        """Load configuration from a file, then apply environment overrides."""
        config = cls()
        if path:
            config = config._merge_file(Path(path).expanduser())
        config = config._apply_env()
        return config

    def _merge_file(self, path: Path) -> "Config":
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        text = path.read_text(encoding="utf-8")
        data: Any
        if path.suffix.lower() in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise ConfigError(
                    "YAML config requires PyYAML; use a .json config file instead"
                ) from exc
            data = yaml.safe_load(text) or {}
        else:
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"invalid JSON in {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"config file must contain an object: {path}")
        return self._merge_dict(data)

    def _merge_dict(self, data: dict) -> "Config":
        for key, value in data.items():
            if not hasattr(self, key):
                raise ConfigError(f"unknown config key: {key}")
            current = getattr(self, key)
            if is_dataclass(current) and isinstance(value, dict):
                setattr(self, key, _merge_dataclass(current, value))
            else:
                setattr(self, key, value)
        return self

    def _apply_env(self) -> "Config":
        env = os.environ
        if v := env.get("TSQL_MODEL"):
            self.model.model = v
        if v := env.get("TSQL_PROVIDER"):
            self.model.provider = v
        if v := env.get("TSQL_API_KEY"):
            self.model.api_key = v
        if v := env.get("TSQL_BASE_URL"):
            self.model.base_url = v
        if v := env.get("TSQL_API_MODE"):
            self.model.api_mode = v
        if v := env.get("TSQL_CONTEXT_LENGTH"):
            self.model.context_length = int(v)
        if v := env.get("TSQL_REASONING_EFFORT"):
            self.model.reasoning_effort = v
        if v := env.get("TSQL_TIMEOUT_SECONDS"):
            self.model.timeout_seconds = float(v)
        if v := env.get("TSQL_MAX_TOKENS"):
            self.model.max_tokens = int(v)
        if v := env.get("TSQL_DB_DIALECT"):
            self.db.dialect = v
        if v := env.get("TSQL_DB_PATH"):
            self.db.path = v
        if v := env.get("TSQL_DB_URL"):
            # postgres://user:pass@host:port/dbname
            self._apply_db_url(v)
        if v := env.get("TSQL_STATE_DB"):
            self.state_db_path = v
        if v := env.get("TSQL_ALLOWED_TABLES"):
            self.db.allowed_tables = [t.strip() for t in v.split(",") if t.strip()]
        if v := env.get("TSQL_APPROVAL_MODE"):
            self.policy.approval_mode = v
        return self

    def _apply_db_url(self, url: str) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        dialect = {
            "postgres": "postgres", "postgresql": "postgres",
            "mysql": "mysql", "duckdb": "duckdb", "sqlite": "sqlite",
        }.get(scheme)
        if not dialect:
            raise ConfigError(f"unsupported database URL scheme: {scheme}")
        self.db.dialect = dialect
        if parsed.hostname:
            self.db.host = parsed.hostname
            self.db.port = parsed.port
        if parsed.username:
            self.db.user = parsed.username
        if parsed.password:
            self.db.password = parsed.password
        if dialect == "sqlite":
            self.db.path = parsed.path or None
        else:
            self.db.database = (parsed.path or "").lstrip("/") or None

    # ----------------------------------------------------------------- helpers
    @property
    def resolved_state_db_path(self) -> Path:
        return Path(self.state_db_path).expanduser()

    def to_dict(self) -> dict:
        return asdict(self)


def _merge_dataclass(instance: Any, values: dict) -> Any:
    """Recursively overlay ``values`` onto a dataclass instance."""
    known = {f.name for f in fields(instance)}
    for key, value in values.items():
        if key not in known:
            raise ConfigError(f"unknown config key: {key}")
        current = getattr(instance, key)
        if is_dataclass(current) and isinstance(value, dict):
            setattr(instance, key, _merge_dataclass(current, value))
        else:
            setattr(instance, key, value)
    return instance

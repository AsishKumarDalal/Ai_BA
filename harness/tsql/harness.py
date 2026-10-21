"""Wiring: assemble a working analyst from a configuration.

This is the one-call entry point the CLI, the HTTP server and the tests all
use, so there is a single place where the pieces are connected and a single
place to look when something is misconfigured.
"""

from __future__ import annotations

from typing import Any, Callable

from .agent.context_compressor import ContextCompressor, ProviderSummarizer
from .agent.events import AgentEvent
from .agent.loop import Agent
from .agent.prompt_builder import PromptContext
from .config import Config
from .db.catalog import Catalog, CatalogBuilder
from .db.connection import Database, connect_database
from .providers import build_provider
from .safety.policy import Policy
from .state.store import SessionDB
from .tools.approval import ApprovalManager
from .tools.registry import discover_builtin_tools, registry


class AnalystHarness:
    """A configured analyst: database, policy, state, provider and agent."""

    def __init__(
        self,
        config: Config,
        db: Database,
        catalog: Catalog,
        policy: Policy,
        state: SessionDB,
        provider: Any,
        approval: ApprovalManager,
        session_id: str | None = None,
    ):
        self.config = config
        self.db = db
        self.catalog = catalog
        self.policy = policy
        self.state = state
        self.provider = provider
        self.approval = approval
        self.session_id = session_id
        self._agent: Agent | None = None

    # ------------------------------------------------------------- factories
    @classmethod
    def create(
        cls,
        config: Config,
        on_event: Callable[[AgentEvent], None] | None = None,
        approval_callback: Callable[[Any], str] | None = None,
        session_id: str | None = None,
        discover_tools: bool = True,
    ) -> "AnalystHarness":
        """Assemble the harness from a configuration."""
        if discover_tools:
            discover_builtin_tools()

        db = connect_database(config.db)
        catalog = CatalogBuilder(db, ttl_seconds=config.db.catalog_ttl_seconds).get()

        policy = Policy.from_config(config)
        # The connected schema is the implicit allowlist: the agent may query
        # the tables that exist, and nothing else.
        policy.known_tables = catalog.table_names()

        state = SessionDB(config.resolved_state_db_path)
        provider = build_provider(config.model)
        approval = ApprovalManager(
            mode=policy.approval_mode,
            timeout=policy.approval_timeout_seconds,
            callback=approval_callback,
        )

        if session_id is None:
            session_id = state.create_session(
                source="cli",
                model=config.model.model,
                provider=config.model.provider,
                db_ref=config.db.path or config.db.database,
                context_length=provider.context_length,
            )

        harness = cls(
            config=config,
            db=db,
            catalog=catalog,
            policy=policy,
            state=state,
            provider=provider,
            approval=approval,
            session_id=session_id,
        )
        harness._on_event = on_event
        return harness

    # ------------------------------------------------------------------ agent
    @property
    def agent(self) -> Agent:
        """The agent, built on first use (so the prompt context is current)."""
        if self._agent is None:
            self._agent = Agent(
                config=self.config,
                provider=self.provider,
                registry=registry,
                state=self.state,
                db=self.db,
                policy=self.policy,
                approval=self.approval,
                on_event=getattr(self, "_on_event", None),
                session_id=self.session_id,
                prompt_context=self.prompt_context(),
                catalog=self.catalog,
                context_engine=self._build_context_engine(),
            )
        return self._agent

    def _build_context_engine(self) -> ContextCompressor:
        compressor = ContextCompressor(
            self.config.compression,
            model=self.config.model.model,
            context_length=self.provider.context_length,
        )
        compressor.set_summarizer(
            ProviderSummarizer(
                self.provider,
                model=self.config.model.model,
                max_tokens=min(self.config.compression.summary_max_tokens, 4_000),
                temperature=0.0,
            )
        )
        return compressor

    def prompt_context(self) -> PromptContext:
        """The context tier of the system prompt."""
        return PromptContext(
            db_label=self.catalog.label or self.config.db.path or self.config.db.database or "",
            dialect=self.db.dialect,
            schema_block=self.catalog.to_prompt_block(),
            glossary=dict(self.config.glossary),
            allowed_tables=self.policy.allowed_tables_for_prompt(),
        )

    # --------------------------------------------------------------- sessions
    def new_session(self, source: str = "cli") -> str:
        self.session_id = self.state.create_session(
            source=source,
            model=self.config.model.model,
            provider=self.config.model.provider,
            db_ref=self.config.db.path or self.config.db.database,
            context_length=self.provider.context_length,
        )
        self._agent = None
        return self.session_id

    def resume_session(self, session_id: str) -> None:
        self.session_id = session_id
        self._agent = None

    # ------------------------------------------------------------------- run
    def ask(self, user_message: str):
        return self.agent.run_conversation(user_message)

    # ----------------------------------------------------------------- close
    def close(self) -> None:
        try:
            self.db.close()
        finally:
            self.state.close()

    def __enter__(self) -> "AnalystHarness":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ info
    def describe(self) -> dict:
        """A summary of how the harness is configured — used by `tsql doctor`."""
        return {
            "model": {
                "provider": self.config.model.provider,
                "model": self.config.model.model,
                "base_url": self.config.model.base_url,
                "api_mode": self.config.model.api_mode,
                "context_length": self.provider.context_length,
                "api_key_set": bool(self.config.model.api_key),
            },
            "database": {
                "dialect": self.db.dialect,
                "label": self.catalog.label,
                "read_only": self.db.read_only,
                "tables": self.catalog.table_names(),
            },
            "policy": {
                "allowed_tables": self.policy.allowed_tables_for_prompt(),
                "max_rows": self.policy.max_rows,
                "pii_masking": self.policy.pii_masking_enabled,
                "pii_entitled": self.policy.role_can_read_pii(),
                "approval_mode": self.policy.approval_mode,
                "forbid_select_star": self.policy.forbid_select_star,
            },
            "state": {
                "path": str(self.config.resolved_state_db_path),
                "session_id": self.session_id,
            },
            "tools": registry.names(),
        }

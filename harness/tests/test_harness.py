"""The wiring: one call to a working analyst."""

from __future__ import annotations

import pytest

from tsql.config import Config
from tsql.harness import AnalystHarness


@pytest.fixture
def config(sales_db, tmp_path):
    cfg = Config()
    cfg.model.model = "test-model"
    cfg.model.api_key = "not-needed"
    cfg.db.dialect = "sqlite"
    cfg.db.path = str(sales_db)
    cfg.state_db_path = str(tmp_path / "state.db")
    return cfg


def test_create_assembles_everything(config):
    harness = AnalystHarness.create(config)
    try:
        assert harness.catalog.table_names()
        assert "orders" in harness.catalog.table_names()
        assert harness.session_id
        assert harness.provider is not None
        assert harness.approval is not None
    finally:
        harness.close()


def test_the_connected_schema_is_the_implicit_allowlist(config):
    harness = AnalystHarness.create(config)
    try:
        assert harness.policy.known_tables == harness.catalog.table_names()
        assert harness.policy.is_table_allowed("orders")
        assert not harness.policy.is_table_allowed("salaries")
    finally:
        harness.close()


def test_the_prompt_context_carries_the_schema(config):
    harness = AnalystHarness.create(config)
    try:
        context = harness.prompt_context()
        assert "orders" in context.schema_block
        assert context.dialect == "sqlite"
    finally:
        harness.close()


def test_describe_reports_the_configuration(config):
    harness = AnalystHarness.create(config)
    try:
        described = harness.describe()
        assert described["model"]["model"] == "test-model"
        assert described["database"]["read_only"] is True
        assert "orders" in described["database"]["tables"]
        assert "run_sql" in described["tools"]
    finally:
        harness.close()


def test_a_new_session_resets_the_agent(config):
    harness = AnalystHarness.create(config)
    try:
        first = harness.session_id
        second = harness.new_session()
        assert first != second
        assert harness.session_id == second
    finally:
        harness.close()


def test_the_agent_is_built_with_the_context_engine(config):
    harness = AnalystHarness.create(config)
    try:
        assert harness.agent.context_engine is not None
        assert harness.agent._catalog is not None
    finally:
        harness.close()

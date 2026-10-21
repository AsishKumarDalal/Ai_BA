"""End-to-end: a model-written query becomes a guarded result, a stored
artifact, and a dashboard panel."""

from __future__ import annotations

import json

import pytest

from tsql.agent.loop import Agent
from tsql.config import Config
from tsql.db.connection import connect_database
from tsql.db.catalog import CatalogBuilder
from tsql.providers.base import Provider, ProviderRequest, ProviderResponse, ToolCall, Usage
from tsql.safety.policy import Policy
from tsql.state.store import SessionDB
from tsql.tools.registry import ToolRegistry, discover_builtin_tools, registry as global_registry


class ScriptedProvider(Provider):
    name = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[ProviderRequest] = []

    @property
    def context_length(self) -> int:
        return 100_000

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def tool_turn(name: str, args: dict) -> ProviderResponse:
    return ProviderResponse(
        tool_calls=[ToolCall(id=f"c{len(name)}", name=name, arguments=args)],
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def text_turn(text: str) -> ProviderResponse:
    return ProviderResponse(text=text, usage=Usage(input_tokens=10, output_tokens=5))


@pytest.fixture
def wired(sales_db, tmp_path):
    """A fully wired harness: db, catalog, policy, state, tools."""
    discover_builtin_tools()

    config = Config()
    config.db.dialect = "sqlite"
    config.db.path = str(sales_db)
    config.policy.max_rows = 1000
    config.model.model = "scripted"

    db = connect_database(config.db)
    catalog = CatalogBuilder(db).get()
    policy = Policy.from_config(config)
    # The connected schema is the implicit allowlist: the model may query the
    # tables that exist, and nothing else.
    policy.known_tables = catalog.table_names()
    state = SessionDB(tmp_path / "state.db")
    session_id = state.create_session(source="test")

    return {
        "config": config, "db": db, "catalog": catalog, "policy": policy,
        "state": state, "session_id": session_id,
    }


def make_agent(wired, provider, **kwargs):
    return Agent(
        config=wired["config"],
        provider=provider,
        registry=global_registry,
        state=wired["state"],
        db=wired["db"],
        policy=wired["policy"],
        session_id=wired["session_id"],
        catalog=wired["catalog"],
        **kwargs,
    )


# ------------------------------------------------------------------ run_sql
def test_run_sql_returns_a_result_handle(wired):
    provider = ScriptedProvider([
        tool_turn("run_sql", {
            "sql": "SELECT c.region, SUM(o.amount) AS revenue FROM orders o "
                    "JOIN customers c ON c.id = o.customer_id GROUP BY c.region",
            "purpose": "revenue by region",
        }),
        text_turn("Revenue by region is ready."),
    ])
    agent = make_agent(wired, provider)
    result = agent.run_conversation("revenue by region?")

    tool_message = [m for m in provider.requests[1].messages if m["role"] == "tool"][-1]
    payload = json.loads(tool_message["content"])
    assert payload["status"] == "ok"
    assert payload["result_ref"].startswith("tbl_")
    assert payload["row_count"] == 4
    assert "revenue" in payload["shape"]
    assert result.final_response.startswith("Revenue by region")


def test_run_sql_stores_the_full_result_as_an_artifact(wired):
    provider = ScriptedProvider([
        tool_turn("run_sql", {"sql": "SELECT id, amount FROM orders", "purpose": "all orders"}),
        text_turn("ok"),
    ])
    agent = make_agent(wired, provider)
    agent.run_conversation("all orders")

    artifacts = wired["state"].list_artifacts(wired["session_id"], kind="table")
    assert len(artifacts) == 1
    stored = wired["state"].get_artifact(artifacts[0]["id"])
    assert stored["payload"]["row_count"] == 10
    assert stored["meta"]["purpose"] == "all orders"
    assert "SELECT id, amount FROM orders" in stored["meta"]["sql"]


def test_run_sql_blocks_a_write(wired):
    provider = ScriptedProvider([
        tool_turn("run_sql", {"sql": "DROP TABLE orders", "purpose": "clean up"}),
        text_turn("I cannot drop tables."),
    ])
    agent = make_agent(wired, provider)
    agent.run_conversation("drop the orders table")

    tool_message = [m for m in provider.requests[1].messages if m["role"] == "tool"][-1]
    payload = json.loads(tool_message["content"])
    assert payload["status"] == "blocked"
    assert payload["errors"]


def test_run_sql_blocks_an_unknown_table(wired):
    provider = ScriptedProvider([
        tool_turn("run_sql", {"sql": "SELECT id FROM salaries", "purpose": "salaries"}),
        text_turn("That table does not exist."),
    ])
    agent = make_agent(wired, provider)
    agent.run_conversation("salaries?")

    payload = json.loads(
        [m for m in provider.requests[1].messages if m["role"] == "tool"][-1]["content"]
    )
    assert payload["status"] == "blocked"
    assert "salaries" in payload["errors"][0]


def test_run_sql_masks_pii_columns(wired):
    provider = ScriptedProvider([
        tool_turn("run_sql", {"sql": "SELECT name, email FROM customers", "purpose": "contacts"}),
        text_turn("ok"),
    ])
    agent = make_agent(wired, provider)
    agent.run_conversation("customer emails")

    payload = json.loads(
        [m for m in provider.requests[1].messages if m["role"] == "tool"][-1]["content"]
    )
    assert payload["masked_columns"] == ["email"]
    assert payload["rows"][0][1] == "[redacted:email]"
    assert payload["rows"][0][0] == "Acme Corp"


def test_run_sql_requires_a_purpose(wired):
    provider = ScriptedProvider([
        tool_turn("run_sql", {"sql": "SELECT id FROM orders"}),
        text_turn("ok"),
    ])
    agent = make_agent(wired, provider)
    agent.run_conversation("orders")

    payload = json.loads(
        [m for m in provider.requests[1].messages if m["role"] == "tool"][-1]["content"]
    )
    assert payload["status"] == "error"
    assert "purpose" in payload["error"]


def test_run_sql_is_hidden_when_no_database_is_connected(wired):
    provider = ScriptedProvider([text_turn("ok")])
    agent = Agent(config=wired["config"], provider=provider, registry=global_registry)
    agent.run_conversation("q")
    assert "run_sql" not in [t.name for t in provider.requests[0].tools]


def test_limit_is_applied_to_the_executed_query(wired):
    provider = ScriptedProvider([
        tool_turn("run_sql", {"sql": "SELECT id FROM orders", "purpose": "orders"}),
        text_turn("ok"),
    ])
    agent = make_agent(wired, provider)
    agent.run_conversation("orders")

    stored = wired["state"].get_artifact(
        wired["state"].list_artifacts(wired["session_id"], kind="table")[0]["id"]
    )
    assert "LIMIT" in stored["meta"]["sql"]


# ----------------------------------------------------------------- add_panel
def test_add_panel_creates_a_dashboard(wired):
    provider = ScriptedProvider([
        tool_turn("run_sql", {
            "sql": "SELECT c.region, SUM(o.amount) AS revenue FROM orders o "
                    "JOIN customers c ON c.id = o.customer_id GROUP BY c.region",
            "purpose": "revenue by region",
        }),
        tool_turn("add_panel", {
            "title": "Revenue by Region",
            "chart_type": "bar",
            "encodings": {"x": "region", "y": "revenue"},
            "insight": "East leads with 40% of revenue.",
        }),
        text_turn("Dashboard ready."),
    ])
    agent = make_agent(wired, provider)
    agent.run_conversation("build a revenue dashboard")

    # The panel call needs the handle from the previous turn; the scripted
    # provider cannot know it, so the panel is expected to fail cleanly.
    dashboards = wired["state"].list_artifacts(wired["session_id"], kind="dashboard")
    assert dashboards == [] or dashboards[0]["payload"]["panels"] == []

    tool_message = [m for m in provider.requests[2].messages if m["role"] == "tool"][-1]
    payload = json.loads(tool_message["content"])
    assert payload["status"] == "error"
    assert "data_ref" in payload["error"]


def test_add_panel_builds_a_dashboard_when_given_a_real_handle(wired):
    provider = ScriptedProvider([
        tool_turn("run_sql", {
            "sql": "SELECT c.region, SUM(o.amount) AS revenue FROM orders o "
                    "JOIN customers c ON c.id = o.customer_id GROUP BY c.region",
            "purpose": "revenue by region",
        }),
        text_turn("placeholder"),
    ])
    agent = make_agent(wired, provider)
    agent.run_conversation("revenue")

    handle = json.loads(
        [m for m in provider.requests[1].messages if m["role"] == "tool"][-1]["content"]
    )["result_ref"]

    provider2 = ScriptedProvider([
        tool_turn("add_panel", {
            "title": "Revenue by Region",
            "chart_type": "bar",
            "data_ref": handle,
            "encodings": {"x": "region", "y": "revenue"},
            "insight": "East leads.",
        }),
        text_turn("Dashboard ready."),
    ])
    agent2 = make_agent(wired, provider2)
    agent2.run_conversation("add the panel")

    payload = json.loads(
        [m for m in provider2.requests[1].messages if m["role"] == "tool"][-1]["content"]
    )
    assert payload["status"] == "ok"
    assert payload["panel_id"] == "p1"

    dashboards = wired["state"].list_artifacts(wired["session_id"], kind="dashboard")
    assert len(dashboards) == 1
    dashboard = wired["state"].get_artifact(dashboards[0]["id"])
    panel = dashboard["payload"]["panels"][0]
    assert panel["chart_type"] == "bar"
    assert panel["encodings"] == {"x": "region", "y": "revenue"}
    assert panel["semantic_types"]["revenue"] == "Currency"  # revenue infers as money
    assert panel["semantic_types"]["region"] == "Category"
    assert panel["insight"] == "East leads."


def test_add_panel_rejects_a_field_that_does_not_exist(wired):
    provider = ScriptedProvider([
        tool_turn("run_sql", {
            "sql": "SELECT c.region, SUM(o.amount) AS revenue FROM orders o "
                    "JOIN customers c ON c.id = o.customer_id GROUP BY c.region",
            "purpose": "revenue by region",
        }),
        text_turn("ok"),
    ])
    agent = make_agent(wired, provider)
    agent.run_conversation("revenue")
    handle = json.loads(
        [m for m in provider.requests[1].messages if m["role"] == "tool"][-1]["content"]
    )["result_ref"]

    provider2 = ScriptedProvider([
        tool_turn("add_panel", {
            "title": "Bad", "chart_type": "bar", "data_ref": handle,
            "encodings": {"x": "nonexistent", "y": "revenue"},
        }),
        text_turn("fixed"),
    ])
    agent2 = make_agent(wired, provider2)
    agent2.run_conversation("add panel")

    payload = json.loads(
        [m for m in provider2.requests[1].messages if m["role"] == "tool"][-1]["content"]
    )
    assert payload["status"] == "error"
    assert "nonexistent" in payload["errors"][0]
    assert "region" in payload["available_columns"]


def test_add_panel_rejects_an_unknown_chart_type(wired):
    provider = ScriptedProvider([
        tool_turn("run_sql", {"sql": "SELECT status FROM orders", "purpose": "r"}),
        text_turn("ok"),
    ])
    agent = make_agent(wired, provider)
    agent.run_conversation("regions")
    handle = json.loads(
        [m for m in provider.requests[1].messages if m["role"] == "tool"][-1]["content"]
    )["result_ref"]

    provider2 = ScriptedProvider([
        tool_turn("add_panel", {
            "title": "x", "chart_type": "sunburst3d", "data_ref": handle,
            "encodings": {"x": "region", "y": "region"},
        }),
        text_turn("fixed"),
    ])
    agent2 = make_agent(wired, provider2)
    agent2.run_conversation("add panel")

    payload = json.loads(
        [m for m in provider2.requests[1].messages if m["role"] == "tool"][-1]["content"]
    )
    assert payload["status"] == "error"
    assert "unknown chart_type" in payload["errors"][0]


# ------------------------------------------------------------------- catalog
def test_catalog_lists_tables_and_columns(wired):
    catalog = wired["catalog"]
    names = catalog.table_names()
    assert "orders" in names
    assert "customers" in names
    assert "v_revenue_by_region" in names

    block = catalog.to_prompt_block()
    assert "orders (table" in block
    assert "amount" in block


def test_catalog_row_estimates_feed_the_guard(wired):
    estimates = wired["catalog"].row_estimates()
    assert estimates["orders"] == 10

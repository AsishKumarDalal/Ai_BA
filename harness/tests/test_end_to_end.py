"""End-to-end: a scripted analyst builds a dashboard from a real database.

This is the whole product in one test — a model writes SQL, the guard checks
it, the database answers, a chart panel is added, and the artifacts land in
the store.
"""

from __future__ import annotations

import json

import pytest

from tsql.config import Config
from tsql.harness import AnalystHarness
from tsql.providers.base import Provider, ProviderRequest, ProviderResponse, ToolCall, Usage


class ScriptedAnalyst(Provider):
    """A model that writes a plausible query and then a plausible panel."""

    name = "scripted"

    def __init__(self):
        self.requests: list[ProviderRequest] = []
        self.step = 0

    @property
    def context_length(self) -> int:
        return 128_000

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        self.step += 1
        usage = Usage(input_tokens=500, output_tokens=100)

        if self.step == 1:
            return ProviderResponse(
                tool_calls=[ToolCall(
                    id="c1", name="run_sql",
                    arguments={
                        "sql": (
                            "SELECT c.region, SUM(o.amount) AS revenue, COUNT(*) AS orders "
                            "FROM orders o JOIN customers c ON c.id = o.customer_id "
                            "WHERE o.status = 'paid' GROUP BY c.region"
                        ),
                        "purpose": "revenue and order count by region for paid orders",
                    },
                )],
                usage=usage,
            )

        if self.step == 2:
            # Read the handle the harness gave us.
            tool_messages = [m for m in request.messages if m["role"] == "tool"]
            payload = json.loads(tool_messages[-1]["content"])
            assert payload["status"] == "ok", payload
            return ProviderResponse(
                tool_calls=[ToolCall(
                    id="c2", name="add_panel",
                    arguments={
                        "title": "Revenue by Region",
                        "chart_type": "bar",
                        "data_ref": payload["result_ref"],
                        "encodings": {"x": "region", "y": "revenue"},
                        "semantic_types": {"region": "Category", "revenue": "Currency"},
                        "insight": "The East leads on revenue.",
                    },
                )],
                usage=usage,
            )

        if self.step == 3:
            tool_messages = [m for m in request.messages if m["role"] == "tool"]
            payload = json.loads(tool_messages[-1]["content"])
            assert payload["status"] == "ok", payload
            return ProviderResponse(
                tool_calls=[ToolCall(
                    id="c3", name="run_sql",
                    arguments={
                        "sql": (
                            "SELECT p.name AS product, SUM(o.quantity) AS units "
                            "FROM orders o JOIN products p ON p.id = o.product_id "
                            "GROUP BY p.name ORDER BY units DESC"
                        ),
                        "purpose": "units sold by product",
                    },
                )],
                usage=usage,
            )

        if self.step == 4:
            tool_messages = [m for m in request.messages if m["role"] == "tool"]
            payload = json.loads(tool_messages[-1]["content"])
            assert payload["status"] == "ok", payload
            return ProviderResponse(
                tool_calls=[ToolCall(
                    id="c4", name="add_panel",
                    arguments={
                        "title": "Top Products by Units",
                        "chart_type": "hbar",
                        "data_ref": payload["result_ref"],
                        "encodings": {"x": "product", "y": "units"},
                        "insight": "Widgets lead on volume.",
                    },
                )],
                usage=usage,
            )

        return ProviderResponse(
            text=(
                "Here is the Q3 sales dashboard.\n\n"
                "- Revenue is concentrated in the East.\n"
                "- Widgets lead on units sold.\n\n"
                "The full breakdown is on the dashboard."
            ),
            usage=usage,
        )


@pytest.fixture
def harness(sales_db, tmp_path):
    config = Config()
    config.model.model = "scripted"
    config.model.api_key = "not-needed"
    config.db.dialect = "sqlite"
    config.db.path = str(sales_db)
    config.state_db_path = str(tmp_path / "state.db")

    provider = ScriptedAnalyst()
    built = AnalystHarness.create(config)
    built.provider = provider
    built._agent = None
    yield built
    built.close()


def test_the_analyst_builds_a_dashboard(harness):
    result = harness.ask("build me a Q3 sales report")

    assert result.final_response.startswith("Here is the Q3 sales dashboard")
    assert len(result.tool_calls) == 4
    assert all(call["is_error"] is False for call in result.tool_calls)

    # Two result artifacts and one dashboard.
    tables = harness.state.list_artifacts(harness.session_id, kind="table")
    dashboards = harness.state.list_artifacts(harness.session_id, kind="dashboard")
    assert len(tables) == 2
    assert len(dashboards) == 1

    dashboard = harness.state.get_artifact(dashboards[0]["id"])
    panels = dashboard["payload"]["panels"]
    assert len(panels) == 2
    assert panels[0]["title"] == "Revenue by Region"
    assert panels[0]["chart_type"] == "bar"
    assert panels[0]["encodings"] == {"x": "region", "y": "revenue"}
    assert panels[0]["semantic_types"]["revenue"] == "Currency"
    assert panels[0]["insight"]
    assert panels[1]["title"] == "Top Products by Units"


def test_every_panel_points_at_a_live_result(harness):
    harness.ask("build me a Q3 sales report")
    dashboard = harness.state.get_artifact(
        harness.state.list_artifacts(harness.session_id, kind="dashboard")[0]["id"]
    )
    for panel in dashboard["payload"]["panels"]:
        result = harness.state.get_artifact(panel["data_ref"])
        assert result is not None
        assert result["kind"] == "table"
        assert result["payload"]["row_count"] > 0


def test_the_audit_chain_records_the_run(harness):
    harness.ask("build me a Q3 sales report")
    events = [entry["event"] for entry in harness.state.list_audit(harness.session_id)]
    assert events.count("sql_allowed") == 2
    assert harness.state.verify_audit_chain()["valid"] is True


def test_the_transcript_is_persisted(harness):
    harness.ask("build me a Q3 sales report")
    messages = harness.state.get_messages(harness.session_id)
    roles = [m["role"] for m in messages]
    assert roles[0] == "user"
    assert "tool" in roles
    assert roles[-1] == "assistant"


def test_the_session_can_be_resumed(harness):
    harness.ask("build me a Q3 sales report")
    session_id = harness.session_id
    harness.new_session()
    assert harness.session_id != session_id
    harness.resume_session(session_id)
    assert harness.session_id == session_id
    # The resumed agent sees the earlier transcript.
    result = harness.ask("what did you just find?")
    assert result.iterations >= 1

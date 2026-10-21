"""The HTTP + SSE API the UI talks to."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from tsql.config import Config
from tsql.harness import AnalystHarness
from tsql.providers.base import Provider, ProviderRequest, ProviderResponse, ToolCall, Usage
from tsql.server.api import AnalystServer


class ScriptedProvider(Provider):
    name = "scripted"

    def __init__(self):
        self.step = 0

    @property
    def context_length(self) -> int:
        return 128_000

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.step += 1
        usage = Usage(input_tokens=200, output_tokens=50)
        if self.step == 1:
            return ProviderResponse(
                tool_calls=[ToolCall(id="c1", name="run_sql", arguments={
                    "sql": (
                        "SELECT c.region, SUM(o.amount) AS revenue FROM orders o "
                        "JOIN customers c ON c.id = o.customer_id GROUP BY c.region"
                    ),
                    "purpose": "revenue by region",
                })],
                usage=usage,
            )
        if self.step == 2:
            tool_messages = [m for m in request.messages if m["role"] == "tool"]
            payload = json.loads(tool_messages[-1]["content"])
            return ProviderResponse(
                tool_calls=[ToolCall(id="c2", name="add_panel", arguments={
                    "title": "Revenue by Region",
                    "chart_type": "bar",
                    "data_ref": payload["result_ref"],
                    "encodings": {"x": "region", "y": "revenue"},
                    "insight": "The East leads.",
                })],
                usage=usage,
            )
        return ProviderResponse(text="Here is your dashboard.", usage=usage)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def server(sales_db, tmp_path):
    config = Config()
    config.model.model = "scripted"
    config.model.api_key = "not-needed"
    config.db.dialect = "sqlite"
    config.db.path = str(sales_db)
    config.state_db_path = str(tmp_path / "state.db")

    harness = AnalystHarness.create(config)
    harness.provider = ScriptedProvider()
    harness._agent = None

    port = _free_port()
    instance = AnalystServer(harness, host="127.0.0.1", port=port)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()

    # Wait for the socket to accept.
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)

    yield f"http://127.0.0.1:{port}", harness
    instance.shutdown()
    harness.close()


def get_json(base: str, path: str):
    with urllib.request.urlopen(f"{base}{path}", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def test_health_reports_the_configuration(server):
    base, _ = server
    payload = get_json(base, "/api/health")
    assert payload["database"]["read_only"] is True
    assert "orders" in payload["database"]["tables"]
    assert "run_sql" in payload["tools"]


def test_catalog_lists_the_tables(server):
    base, _ = server
    payload = get_json(base, "/api/catalog")
    names = [t["name"] for t in payload["tables"]]
    assert "orders" in names
    assert payload["read_only"] is True


def test_artifacts_start_empty(server):
    base, _ = server
    payload = get_json(base, "/api/artifacts")
    assert payload["artifacts"] == []


def test_sessions_are_listed(server):
    base, _ = server
    payload = get_json(base, "/api/sessions")
    assert len(payload["sessions"]) >= 1


def test_chat_streams_events_and_builds_a_dashboard(server):
    base, _ = server
    request = urllib.request.Request(
        f"{base}/api/chat",
        data=json.dumps({"message": "build a revenue dashboard"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    events: list[tuple[str, dict]] = []
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.headers["Content-Type"] == "text/event-stream"
        buffer = ""
        while True:
            chunk = response.read(4096)
            if not chunk:
                break
            buffer += chunk.decode("utf-8")
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                event_type = None
                data_lines = []
                for line in frame.split("\n"):
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                if event_type and data_lines:
                    events.append((event_type, json.loads("\n".join(data_lines))))

    types = [t for t, _ in events]
    assert "tool_start" in types
    assert "artifact_created" in types
    assert "result" in types

    result = next(data for t, data in events if t == "result")
    assert result["final_response"] == "Here is your dashboard."

    # The dashboard and its panels are now artifacts.
    artifacts = get_json(base, "/api/artifacts")["artifacts"]
    kinds = {a["kind"] for a in artifacts}
    assert "dashboard" in kinds
    assert "table" in kinds


def test_a_built_dashboard_can_be_fetched_by_id(server):
    base, _ = server
    request = urllib.request.Request(
        f"{base}/api/chat",
        data=json.dumps({"message": "build a revenue dashboard"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()

    artifacts = get_json(base, "/api/artifacts")["artifacts"]
    dashboard = next(a for a in artifacts if a["kind"] == "dashboard")
    payload = get_json(base, f"/api/artifacts/{dashboard['id']}")["artifact"]
    panels = payload["payload"]["panels"]
    assert len(panels) == 1
    assert panels[0]["title"] == "Revenue by Region"
    assert panels[0]["encodings"] == {"x": "region", "y": "revenue"}

    # The panel points at a live result.
    result = get_json(base, f"/api/artifacts/{panels[0]['data_ref']}")["artifact"]
    assert result["kind"] == "table"
    assert result["payload"]["row_count"] > 0


def test_audit_is_exposed(server):
    base, _ = server
    payload = get_json(base, "/api/audit")
    assert payload["verification"]["valid"] is True


def test_an_empty_message_is_rejected(server):
    base, _ = server
    request = urllib.request.Request(
        f"{base}/api/chat",
        data=json.dumps({"message": "  "}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=10)
    assert excinfo.value.code == 400

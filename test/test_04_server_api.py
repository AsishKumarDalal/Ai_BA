"""End-to-end: the whole thing through the API the UI talks to.

    python test/test_04_server_api.py

Starts the real server, then drives it the way the browser does:

* ``GET /``                 — the UI is served
* ``GET /api/health``       — the resolved configuration
* ``GET /api/catalog``      — the connected schema
* ``POST /api/chat``        — a turn, streamed as Server-Sent Events
* ``GET /api/artifacts``    — what the UI renders on the canvas

The event stream is checked as it arrives: the turn has to start, call tools,
answer, and end with a result. The artifacts have to be there afterwards, or
the canvas would be empty.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402
from helpers import bad, heading, info, ok, skip  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
VENV_PY = REPO / ".venv" / "bin" / "python"
#: A fresh state database per run, so two runs never fight over one file.
STATE_DB = Path(f"/tmp/demo-e2e-server-{int(time.time())}.db")


class ServerTest(unittest.TestCase):
    server: subprocess.Popen | None = None
    port: int = 0
    base: str = ""

    @classmethod
    def setUpClass(cls):
        if helpers.OFFLINE:
            return
        helpers.ensure_demo_db()
        cls.port = helpers.free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.server = subprocess.Popen(
            [
                str(VENV_PY), "-m", "tsql.cli",
                "--config", str(helpers.CONFIG_PATH),
                "--db-path", str(helpers.DEMO_DB),
                "--state-db", str(STATE_DB),
                "serve",
                "--port", str(cls.port),
            ],
            cwd=str(REPO / "harness"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.time() + 60
        while time.time() < deadline:
            if cls.server.poll() is not None:
                raise RuntimeError(f"the server died:\n{cls.server.stdout.read()}")
            status, _ = helpers.get_json(f"{cls.base}/api/health", timeout=3)
            if status == 200:
                return
            time.sleep(0.4)
        raise RuntimeError("the server did not come up in 60 seconds")

    @classmethod
    def tearDownClass(cls):
        if cls.server is not None:
            cls.server.terminate()
            try:
                cls.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.server.kill()

    def test_01_the_ui_is_served(self):
        heading("the UI is served")
        status, body = helpers.get_json(f"{self.base}/")
        self.assertEqual(status, 200)
        self.assertIn("<div id=\"root\">", body, "the UI shell is not in the page")
        ok("GET / returns the built UI")

    def test_02_health_and_catalog(self):
        heading("the configuration the UI shows")
        status, health = helpers.get_json(f"{self.base}/api/health")
        self.assertEqual(status, 200)
        self.assertIn(health["model"]["model"], ("k2-horizon-mova-36b-a4b",))
        self.assertTrue(health["database"]["label"].endswith("demo.db"))
        self.assertTrue(health["model"]["api_key_set"])
        ok(f"model: {health['model']['model']}")
        ok(f"database: {health['database']['label']} "
           f"({len(health['database']['tables'])} tables, "
           f"read-only: {health['database']['read_only']})")

        status, catalog = helpers.get_json(f"{self.base}/api/catalog")
        self.assertEqual(status, 200)
        names = {t["name"] for t in catalog["tables"]}
        self.assertIn("orders", names)
        ok(f"catalog: {sorted(names)}")

    def test_03_a_turn_streams_over_sse(self):
        heading("a turn, streamed")
        events: list[tuple[str, dict]] = []
        for event_type, data in helpers.post_sse(
            f"{self.base}/api/chat", {"message": "What is the total revenue from paid orders?"}
        ):
            events.append((event_type, data))

        types = [t for t, _ in events]
        info(f"events: {types}")
        self.assertIn("agent_start", types)
        self.assertIn("tool_start", types, "the model never called a tool")
        self.assertIn("tool_end", types)
        self.assertIn("assistant_text", types)
        self.assertEqual(types[-1], "result", "the stream did not end with a result")

        result = events[-1][1]
        self.assertEqual(result["stop_reason"], "completed", result.get("error") or "")
        self.assertTrue(result["tool_calls"], "no tool calls in the result")

        expected = helpers.total_revenue()
        figures = helpers.amounts(result["final_response"])
        self.assertTrue(figures, "the answer has no number in it")
        self.assertTrue(
            any(abs(f - expected) < 0.01 for f in figures),
            f"the answer has {figures}, the database says {expected}",
        )
        ok(f"the turn ended with ${expected:,.2f} — the number in the database")
        ok(f"the stream closed after {len(events)} events")

    def test_04_the_dashboard_reaches_the_canvas(self):
        heading("a dashboard, and the artifacts the canvas reads")
        events: list[tuple[str, dict]] = []
        for event_type, data in helpers.post_sse(
            f"{self.base}/api/chat",
            {
                "message": (
                    "Build a dashboard with revenue by region (bar) and revenue "
                    "by month (line). One-line insight on each panel."
                )
            },
        ):
            events.append((event_type, data))

        types = [t for t, _ in events]
        self.assertIn("artifact_created", types, "no artifact was created")
        ok(f"artifact_created events: {types.count('artifact_created')}")

        status, payload = helpers.get_json(f"{self.base}/api/artifacts")
        self.assertEqual(status, 200)
        found = [a for a in payload["artifacts"] if a["kind"] == "dashboard"]
        self.assertTrue(found, "the canvas would be empty: no dashboard artifact")
        dashboard_id = found[0]["id"]
        ok(f"the canvas has a dashboard: {dashboard_id}")

        # the list is a summary; the payload comes from the single-artifact
        # endpoint, the way the UI fetches it
        status, single = helpers.get_json(f"{self.base}/api/artifacts/{dashboard_id}")
        self.assertEqual(status, 200)
        self.assertEqual(single["artifact"]["id"], dashboard_id)
        panels = single["artifact"]["payload"]["panels"]
        self.assertGreaterEqual(len(panels), 2)
        ok(f"the canvas has {len(panels)} panels: {[p['chart_type'] for p in panels]}")

        for panel in panels:
            status, table = helpers.get_json(f"{self.base}/api/artifacts/{panel['data_ref']}")
            self.assertEqual(status, 200, f"the data for {panel['id']} is missing")
            self.assertTrue(table["artifact"]["payload"]["rows"])
        ok("every panel's data is fetchable and has rows")

    def test_05_the_audit_log_is_written(self):
        heading("the audit log")
        status, payload = helpers.get_json(f"{self.base}/api/audit")
        self.assertEqual(status, 200)
        self.assertTrue(payload["entries"], "the audit log is empty")
        ok(f"{len(payload['entries'])} entries, chain valid: "
           f"{payload['verification']['valid']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

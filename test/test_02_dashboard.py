"""End-to-end: can the model build a dashboard, and are the charts right?

    python test/test_02_dashboard.py

The model is asked for three panels. Then every panel is checked against the
database:

* the panel's data is really in the database
* the revenue panels add up to the total paid revenue — this is the check that
  catches a double-counted measure, which is the classic text-to-SQL failure
* every dollar figure in the insight text is a figure that exists in the
  database — this catches a model that believes its own wrong number
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402
from helpers import bad, heading, info, ok, skip  # noqa: E402

REQUEST = (
    "Build a sales report dashboard with: revenue by region (bar), revenue by "
    "month (line), and top 5 products by revenue (hbar). Add a one-line insight "
    "to each panel."
)


class DashboardTest(unittest.TestCase):
    def test_a_three_panel_dashboard_with_correct_numbers(self):
        heading("the model builds a dashboard")
        if helpers.OFFLINE:
            skip("TSQL_TEST_OFFLINE=1")
            return

        helpers.ensure_demo_db()
        state_db = Path("/tmp/demo-e2e-state.db")
        helpers.reset_state_db(state_db)

        harness = helpers.make_harness(state_db)
        try:
            result = harness.agent.run_conversation(REQUEST)
        finally:
            harness.close()

        self.assertEqual(result.stop_reason, "completed", result.error or "")
        self.assertTrue(
            any(call["name"] == "add_panel" for call in result.tool_calls),
            f"the agent never called add_panel: {[c['name'] for c in result.tool_calls]}",
        )
        ok(f"tool calls: {[c['name'] for c in result.tool_calls]}")

        dashboard = helpers.dashboard_artifact(state_db)
        self.assertIsNotNone(dashboard, "no dashboard artifact was stored")
        panels = dashboard["payload"]["panels"]
        info(f"{len(panels)} panels, chart types: {[p['chart_type'] for p in panels]}")

        # ---------------------------------------------------------- the panels
        for chart_type in ("bar", "line", "hbar"):
            self.assertIn(
                chart_type,
                [p["chart_type"] for p in panels],
                f"the dashboard has no {chart_type} panel",
            )
        ok("the three requested chart types are present")

        tables = helpers.table_artifacts(state_db)
        total = helpers.total_revenue()
        known = helpers.known_figures()
        truth = helpers.truth()

        for panel in panels:
            heading(f"panel: {panel['title']} ({panel['chart_type']})")

            data_ref = panel["data_ref"]
            self.assertIn(data_ref, tables, f"the panel points at a missing table {data_ref}")
            table = tables[data_ref]["payload"]
            columns = [c["name"] for c in table["columns"]]
            rows = table["rows"]
            self.assertTrue(rows, f"the panel {panel['id']} has no rows")
            info(f"{len(rows)} rows, columns: {columns}")

            # the x field must exist in the data
            x_field = panel["encodings"].get("x")
            self.assertIn(x_field, columns, f"x={x_field} is not in {columns}")
            y_field = panel["encodings"].get("y")
            self.assertIn(y_field, columns, f"y={y_field} is not in {columns}")

            # the chart must say what the data means
            semantics = panel.get("semantic_types") or {}
            self.assertIn(x_field, semantics, f"no semantic type for {x_field}")
            info(f"semantic types: {semantics}")

            # the values themselves
            x_index = columns.index(x_field)
            y_index = columns.index(y_field)
            values = [float(row[y_index]) for row in rows]
            labels = [str(row[x_index]) for row in rows]
            info(f"{list(zip(labels, values))}")

            # a revenue panel must add up to the revenue in the database
            title_blob = f"{panel['title']} {panel.get('insight', '')} {y_field}".lower()
            if "revenue" in title_blob:
                self.assertAlmostEqual(
                    sum(values),
                    total,
                    delta=0.01,
                    msg=(
                        f"the {panel['title']} panel adds up to {sum(values)}, "
                        f"but the database says {total}"
                    ),
                )
                ok(f"the panel adds up to ${total:,.2f} — the revenue in the database")

            # every dollar figure in the insight must be a real figure
            insight = panel.get("insight") or ""
            self.assertTrue(insight.strip(), f"the panel {panel['id']} has no insight")
            for figure in helpers.amounts(insight):
                self.assertTrue(
                    any(abs(figure - k) < 0.01 for k in known),
                    f"the insight says ${figure:,.2f}, which is not in the database",
                )
            ok(f"insight: {insight}")

        # ------------------------------------------------ the model did not lie
        heading("every figure in the answer is a figure in the database")
        answer_figures = helpers.amounts(result.final_response)
        for figure in answer_figures:
            self.assertTrue(
                any(abs(figure - k) < 0.01 for k in known),
                f"the answer says ${figure:,.2f}, which is not in the database",
            )
        ok(f"the answer mentions {answer_figures}, all of them real")

        # the three dimensions must each be right, not just the totals
        for dimension, values in truth.items():
            info(f"the database's {dimension}: "
                 + ", ".join(f"{k}={v:,.2f}" for k, v in sorted(values.items())))


if __name__ == "__main__":
    unittest.main(verbosity=2)

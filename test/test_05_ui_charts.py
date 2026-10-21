"""End-to-end: the live dashboard's charts, compiled by the real UI compiler.

    python test/test_05_ui_charts.py

The model writes a *semantic* chart request — a chart type, a data handle and
the encodings. The UI turns that into Vega-Lite. This test runs the same
compiler, and the same row conversion, that the browser runs, over the panels
the model actually produced, and checks that each one comes out as a real spec
with the right data in it.

It also runs the UI's own test suite, so a broken compiler is caught here
rather than in the browser.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402
from helpers import heading, info, ok, skip  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
UI = REPO / "ui"
COMPILE_TS = UI / "src" / "charts" / "compile.ts"
ROWS_TS = UI / "src" / "charts" / "rows.ts"
ESBUILD = UI / "node_modules" / ".bin" / "esbuild"
STATE_DB = Path("/tmp/demo-e2e-state.db")

REQUEST = (
    "Build a sales report dashboard with: revenue by region (bar), revenue by "
    "month (line), and top 5 products by revenue (hbar). One-line insight on "
    "each panel."
)

NODE_SCRIPT = """
import { compileToVegaLite } from "%(module)s";
import { rowsAsObjects } from "%(rows_module)s";
import fs from "node:fs";

const input = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const out = input.map(({ spec, columns, rows }) => {
  // The browser's own conversion, not a copy of it.
  const objects = rowsAsObjects(columns, rows);
  return {
    spec,
    rowCount: objects.length,
    columns: objects.length ? Object.keys(objects[0]) : [],
    vega: compileToVegaLite(spec, objects),
  };
});
fs.writeFileSync(process.argv[3], JSON.stringify(out, null, 2));
"""


def compile_panels(panels: list[dict]) -> list[dict]:
    """Run the UI's compiler over a list of panels. Returns the Vega-Lite specs."""
    with tempfile.TemporaryDirectory() as work:
        work_dir = Path(work)
        module = work_dir / "compile.mjs"
        rows_module = work_dir / "rows.mjs"
        for source, target in ((COMPILE_TS, module), (ROWS_TS, rows_module)):
            subprocess.run(
                [str(ESBUILD), str(source), "--format=esm", f"--outfile={target}"],
                check=True,
                capture_output=True,
            )

        script = work_dir / "run.mjs"
        script.write_text(
            NODE_SCRIPT % {"module": module.as_uri(), "rows_module": rows_module.as_uri()}
        )
        source = work_dir / "panels.json"
        source.write_text(json.dumps(panels))
        result_path = work_dir / "vega.json"

        result = subprocess.run(
            ["node", str(script), str(source), str(result_path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"the compiler failed:\n{result.stderr}")
        return json.loads(result_path.read_text())


class ChartCompilerTest(unittest.TestCase):
    def test_01_the_ui_test_suite_passes(self):
        heading("the UI's own tests")
        result = subprocess.run(
            ["npx", "vitest", "run"],
            cwd=str(UI),
            capture_output=True,
            text=True,
            timeout=600,
        )
        tail = "\n".join(result.stdout.strip().splitlines()[-6:])
        info(tail)
        self.assertEqual(result.returncode, 0, "the UI test suite failed")
        ok("the chart compiler's own tests pass")

    def test_02_the_live_panels_compile(self):
        heading("the live panels, compiled")
        if helpers.OFFLINE:
            skip("TSQL_TEST_OFFLINE=1")
            return

        helpers.ensure_demo_db()
        dashboard = helpers.dashboard_artifact(STATE_DB)
        if dashboard is None:
            info("no dashboard found — asking the model for one")
            dashboard = helpers.build_dashboard(REQUEST, STATE_DB)
        panels = dashboard["payload"]["panels"]
        self.assertTrue(panels, "the dashboard has no panels")

        tables = helpers.table_artifacts(STATE_DB)
        payload = []
        for panel in panels:
            table = tables.get(panel["data_ref"])
            self.assertIsNotNone(table, f"the panel {panel['id']} points at a missing table")
            table_payload = table["payload"]
            payload.append({
                "spec": {
                    "chart_type": panel["chart_type"],
                    "data_ref": panel["data_ref"],
                    "encodings": panel["encodings"],
                    "semantic_types": panel.get("semantic_types") or {},
                    "title": panel.get("title"),
                    "insight": panel.get("insight"),
                },
                # the raw table, exactly as the artifact stores it
                "columns": [c["name"] for c in table_payload["columns"]],
                "rows": table_payload["rows"],
            })

        compiled = compile_panels(payload)
        self.assertEqual(len(compiled), len(panels))

        for entry, panel in zip(compiled, panels):
            vega = entry["vega"]
            heading(f"{panel['title']} ({panel['chart_type']})")
            self.assertEqual(
                vega["$schema"], "https://vega.github.io/schema/vega-lite/v5.json"
            )
            self.assertIn("mark", vega)
            self.assertIn("encoding", vega)
            self.assertTrue(vega["data"]["values"], "the chart has no data")
            self.assertEqual(
                len(vega["data"]["values"]), entry["rowCount"],
                "the chart lost rows",
            )
            info(f"mark: {json.dumps(vega['mark'])}")
            info(f"encoding: {json.dumps(vega['encoding'])}")

            # the encodings must point at columns that exist in the data
            columns = set(entry["columns"])
            for channel, definition in vega["encoding"].items():
                field = definition.get("field")
                if field:
                    self.assertIn(field, columns, f"{channel} points at a missing column")
            ok(f"the chart is a valid Vega-Lite spec with {entry['rowCount']} points")

    def test_03_a_bar_chart_of_currency_starts_at_zero(self):
        heading("the design rules still hold")
        compiled = compile_panels([{
            "spec": {
                "chart_type": "bar",
                "data_ref": "tbl_x",
                "encodings": {"x": "region", "y": "revenue"},
                "semantic_types": {"region": "Category", "revenue": "Currency"},
                "title": "Revenue by Region",
            },
            "columns": ["region", "revenue"],
            "rows": [
                ["East", 1652.5],
                ["West", 2533.5],
            ],
        }])
        vega = compiled[0]["vega"]

        y_axis = vega["encoding"]["y"]
        self.assertEqual(y_axis.get("scale", {}).get("zero"), True,
                         "a currency bar chart must start at zero")
        self.assertEqual(y_axis.get("format"), "$,.0f",
                         "a currency axis must format the numbers")
        self.assertEqual(vega["data"]["values"], [
            {"region": "East", "revenue": 1652.5},
            {"region": "West", "revenue": 2533.5},
        ])
        ok(f"the y axis: {json.dumps(y_axis)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

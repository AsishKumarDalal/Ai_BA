#!/usr/bin/env python3
"""Run the harness with a scripted analyst, so the UI can be tried offline.

    python harness/examples/run_demo_server.py --port 8000

The scripted analyst answers a handful of plausible questions about the demo
database, so the UI has something to show without a model endpoint.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tsql.config import Config  # noqa: E402
from tsql.harness import AnalystHarness  # noqa: E402
from tsql.providers.base import (  # noqa: E402
    Provider,
    ProviderRequest,
    ProviderResponse,
    ToolCall,
    Usage,
)
from tsql.server.api import AnalystServer  # noqa: E402

SCRIPT: list[dict] = [
    {
        "tool": "run_sql",
        "args": {
            "sql": (
                "SELECT c.region, SUM(o.amount) AS revenue, COUNT(*) AS orders "
                "FROM orders o JOIN customers c ON c.id = o.customer_id "
                "WHERE o.status = 'paid' GROUP BY c.region"
            ),
            "purpose": "revenue and order count by region for paid orders",
        },
    },
    {
        "tool": "add_panel",
        "args": {
            "title": "Revenue by Region",
            "chart_type": "bar",
            "data_ref": "{last_result}",
            "encodings": {"x": "region", "y": "revenue"},
            "semantic_types": {"region": "Category", "revenue": "Currency"},
            "insight": "The East leads on revenue, followed by the West.",
        },
    },
    {
        "tool": "run_sql",
        "args": {
            "sql": (
                "SELECT p.name AS product, SUM(o.quantity) AS units, "
                "SUM(o.amount) AS revenue FROM orders o "
                "JOIN products p ON p.id = o.product_id "
                "WHERE o.status = 'paid' GROUP BY p.name ORDER BY units DESC"
            ),
            "purpose": "units and revenue by product for paid orders",
        },
    },
    {
        "tool": "add_panel",
        "args": {
            "title": "Top Products by Units",
            "chart_type": "hbar",
            "data_ref": "{last_result}",
            "encodings": {"x": "product", "y": "units"},
            "semantic_types": {"product": "Category", "units": "Quantity"},
            "insight": "Widgets lead on volume; Thingamajigs lead on revenue.",
        },
    },
    {
        "tool": "run_sql",
        "args": {
            "sql": (
                "SELECT substr(o.ordered_at, 1, 7) AS month, "
                "SUM(o.amount) AS revenue FROM orders o "
                "WHERE o.status = 'paid' GROUP BY month ORDER BY month"
            ),
            "purpose": "monthly revenue for paid orders",
        },
    },
    {
        "tool": "add_panel",
        "args": {
            "title": "Revenue by Month",
            "chart_type": "line",
            "data_ref": "{last_result}",
            "encodings": {"x": "month", "y": "revenue"},
            "semantic_types": {"month": "YearMonth", "revenue": "Currency"},
            "insight": "Revenue peaks in December with the holiday orders.",
        },
    },
]

FINAL_ANSWER = (
    "Here is the sales dashboard.\n\n"
    "- **Revenue is concentrated in the East**, which leads every region by a wide margin.\n"
    "- **Widgets lead on volume**, while Thingamajigs lead on revenue — the services "
    "line is higher value per unit.\n"
    "- **Revenue peaks in December**, tracking the holiday orders.\n\n"
    "Three panels, each traceable back to the query behind it."
)


class DemoProvider(Provider):
    """Replays the script above, resolving the last result handle."""

    name = "demo"

    def __init__(self):
        self.step = 0
        self.last_result = ""

    @property
    def context_length(self) -> int:
        return 128_000

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        usage = Usage(input_tokens=400, output_tokens=80)

        if self.step >= len(SCRIPT):
            return ProviderResponse(text=FINAL_ANSWER, usage=usage)

        entry = SCRIPT[self.step]
        self.step += 1

        if entry["tool"] == "run_sql":
            return ProviderResponse(
                tool_calls=[ToolCall(id=f"c{self.step}", name="run_sql", arguments=entry["args"])],
                usage=usage,
            )

        # add_panel needs the handle the previous run_sql produced.
        tool_messages = [m for m in request.messages if m["role"] == "tool"]
        handle = self.last_result
        for message in reversed(tool_messages):
            try:
                payload = json.loads(message["content"])
            except (json.JSONDecodeError, TypeError):
                continue
            if payload.get("result_ref"):
                handle = payload["result_ref"]
                break

        args = dict(entry["args"])
        args["data_ref"] = handle
        return ProviderResponse(
            tool_calls=[ToolCall(id=f"c{self.step}", name="add_panel", arguments=args)],
            usage=usage,
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="/tmp/demo.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--state-db", default="/tmp/demo-state.db")
    args = parser.parse_args(argv[1:])

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"creating the demo database at {db_path}")
        sys.argv = ["make_demo_db", str(db_path)]
        exec(  # noqa: S102 - a tiny demo script; keep it dependency free
            compile(
                (Path(__file__).with_name("make_demo_db.py")).read_text(),
                "make_demo_db.py",
                "exec",
            ),
            {"__name__": "__main__"},
        )

    config = Config()
    config.model.model = "demo"
    config.model.api_key = "not-needed"
    config.db.dialect = "sqlite"
    config.db.path = str(db_path)
    config.state_db_path = args.state_db

    harness = AnalystHarness.create(config)
    harness.provider = DemoProvider()
    harness._agent = None

    ui_dir = Path(__file__).resolve().parents[2] / "ui" / "dist"
    server = AnalystServer(harness, host=args.host, port=args.port, ui_dir=str(ui_dir))
    print(f"demo analyst ready — the scripted provider answers without a model endpoint")
    try:
        server.serve_forever()
    finally:
        harness.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

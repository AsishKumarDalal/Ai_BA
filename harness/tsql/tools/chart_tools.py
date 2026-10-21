"""The `add_panel` tool — turn a result into a chart on a dashboard.

A **panel** is one box on a dashboard: a chart, the query behind it, and a
one-line takeaway. Panels are written *independently* so a single bad panel can
be retried without discarding the rest of the dashboard.

The model writes a compact semantic spec, not chart code. The harness
validates it against the referenced result, then stores it. The UI compiles
the spec to Vega-Lite later.
"""

from __future__ import annotations

import json
from typing import Any

from ..charts.spec import validate_spec
from ..tools.registry import registry

ADD_PANEL_SCHEMA = {
    "name": "add_panel",
    "description": (
        "Add a chart panel to the dashboard, using the result of a previous "
        "run_sql call.\n\n"
        "Give the `chart_type` (bar, hbar, stacked_bar, line, area, scatter, "
        "pie, donut, heatmap, waterfall, funnel, gauge, treemap, table, metric), "
        "the `data_ref` from run_sql, and `encodings` mapping chart roles to "
        "column names: `x` is the category or time axis, `y` is the measure, "
        "`series` splits the data into groups.\n\n"
        "Add `semantic_types` when you can — Quantity, Currency, Percent, "
        "YearMonth, Category, Date, Text — so the chart picks the right axis "
        "and number format. They are inferred when omitted.\n\n"
        "Add a one-line `insight`: what the panel shows. A chart without a "
        "takeaway is decoration."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "The panel heading."},
            "chart_type": {"type": "string", "description": "The chart type."},
            "data_ref": {
                "type": "string",
                "description": "The result_ref from a run_sql call.",
            },
            "encodings": {
                "type": "object",
                "description": "Maps chart roles (x, y, series) to column names.",
                "properties": {
                    "x": {"type": "string"},
                    "y": {"type": "string"},
                    "series": {"type": "string"},
                    "size": {"type": "string"},
                },
            },
            "semantic_types": {
                "type": "object",
                "description": "Maps column names to semantic types.",
                "additionalProperties": {"type": "string"},
            },
            "layout": {
                "type": "object",
                "description": "Optional grid position: row, col, w, h.",
                "properties": {
                    "row": {"type": "integer"},
                    "col": {"type": "integer"},
                    "w": {"type": "integer"},
                    "h": {"type": "integer"},
                },
            },
            "insight": {
                "type": "string",
                "description": "One line: what this panel shows.",
            },
            "dashboard_id": {
                "type": "string",
                "description": "Which dashboard to add to. Omit for the session dashboard.",
            },
        },
        "required": ["title", "chart_type", "data_ref", "encodings"],
    },
}


def handle_add_panel(args: dict, **kwargs: Any) -> str:
    ctx = kwargs.get("ctx")
    if ctx is None:
        return json.dumps({"status": "error", "error": "no tool context available"})

    state = getattr(ctx, "state", None)
    session_id = getattr(ctx, "session_id", None)
    if state is None or not session_id:
        return json.dumps({
            "status": "error",
            "error": "panels need a session to store them in",
        })

    data_ref = str(args.get("data_ref") or "").strip()
    if not data_ref:
        return json.dumps({"status": "error", "error": "data_ref is required"})

    source = state.get_artifact(data_ref)
    if source is None or source.get("kind") != "table":
        return json.dumps({
            "status": "error",
            "error": (
                f"no result found at '{data_ref}'. It must be the result_ref "
                "returned by a previous run_sql call."
            ),
        })

    payload = source.get("payload") or {}
    columns = [c.get("name") for c in payload.get("columns", []) if c.get("name")]
    rows = payload.get("rows", [])

    sample_values = _sample_values(columns, rows)
    declared_types = {
        c.get("name"): c.get("type", "")
        for c in payload.get("columns", [])
        if c.get("name")
    }

    validation = validate_spec(
        args,
        available_columns=columns,
        sample_values=sample_values,
        declared_types=declared_types,
    )
    if not validation.ok:
        return json.dumps({
            "status": "error",
            "error": "the chart spec is invalid",
            "errors": validation.errors,
            "available_columns": columns[:30],
            "guidance": "Fix the spec and call add_panel again.",
        })

    spec = validation.spec
    dashboard_id, dashboard = _resolve_dashboard(
        state, session_id, args.get("dashboard_id"), spec.title
    )

    panel = {
        "id": f"p{len(dashboard['panels']) + 1}",
        "title": spec.title,
        "chart_type": spec.chart_type,
        "encodings": spec.encodings,
        "semantic_types": spec.semantic_types,
        "data_ref": data_ref,
        "layout": spec.layout or _auto_layout(len(dashboard["panels"])),
        "insight": spec.insight,
        "warnings": validation.warnings,
    }
    dashboard["panels"].append(panel)

    state.update_artifact(
        dashboard_id,
        payload=dashboard,
        title=dashboard.get("title"),
    )

    if ctx.agent is not None and hasattr(ctx.agent, "emitter"):
        ctx.agent.emitter.emit(
            "artifact_created",
            artifact_id=dashboard_id,
            kind="dashboard",
            panel_id=panel["id"],
            title=panel["title"],
        )

    return json.dumps({
        "status": "ok",
        "dashboard_id": dashboard_id,
        "panel_id": panel["id"],
        "panel_count": len(dashboard["panels"]),
        "chart_type": spec.chart_type,
        "warnings": validation.warnings,
        "next_step": (
            "Add the remaining panels, then give the user a short narrative "
            "summarising what the dashboard shows."
        ),
    })


def _resolve_dashboard(
    state, session_id: str, dashboard_id: str | None, title: str
) -> tuple[str, dict]:
    """Find or create the dashboard this panel belongs to.

    Returns ``(artifact_id, payload)``. One dashboard per session by default.
    """
    if dashboard_id:
        existing = state.get_artifact(dashboard_id)
        if existing and existing.get("kind") == "dashboard":
            payload = existing.get("payload") or {}
            payload.setdefault("panels", [])
            payload.setdefault("title", title or "Dashboard")
            return existing["id"], payload

    for artifact in state.list_artifacts(session_id, kind="dashboard"):
        # list_artifacts returns summaries, not payloads; fetch the full record.
        full = state.get_artifact(artifact["id"])
        payload = (full or {}).get("payload")
        if payload:
            payload.setdefault("panels", [])
            return artifact["id"], payload

    new_id = state.create_artifact(
        session_id=session_id,
        kind="dashboard",
        title=title or "Analysis",
        payload={"title": title or "Analysis", "panels": [], "narrative": "",
                 "key_findings": [], "caveats": []},
        meta={},
    )
    return new_id, state.get_artifact(new_id).get("payload")


def _auto_layout(index: int) -> dict:
    """A sensible two-column grid position when the model does not specify one."""
    return {"row": index // 2, "col": index % 2, "w": 1, "h": 1}


def _sample_values(columns: list[str], rows: list[list[Any]], limit: int = 50) -> dict:
    samples: dict[str, list[Any]] = {}
    for index, column in enumerate(columns):
        values = [row[index] for row in rows[:limit] if index < len(row)]
        samples[column] = values
    return samples


registry.register(
    name="add_panel",
    toolset="charts",
    schema=ADD_PANEL_SCHEMA,
    handler=handle_add_panel,
    description="Add a chart panel to the dashboard.",
    emoji="📊",
)

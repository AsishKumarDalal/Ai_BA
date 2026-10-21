"""The chart IR — a compact, semantic chart request.

The model writes this, not chart code:

    {
      "chart_type": "bar",
      "data_ref": "tbl_a91f",
      "encodings": {"x": "region", "y": "revenue"},
      "semantic_types": {"region": "Category", "revenue": "Quantity"}
    }

Why a semantic IR instead of letting the model write Vega-Lite:

* **Compact.** Six lines instead of sixty, so it costs few tokens and survives
  context compression.
* **Semantic.** ``semantic_types`` says what a column *means*, so the renderer
  can pick a sane scale, baseline and format. ``2024-03`` becomes a month, not
  the number 2024. The model supplies intent; the compiler supplies design.
* **Editable.** Changing one encoding does not regenerate the whole spec, and
  the UI can re-render it without a model round-trip.
* **Portable.** The same spec compiles to Vega-Lite, ECharts or Chart.js.

The shape deliberately mirrors Microsoft Flint's ``ChartAssemblyInput``
(``data`` + ``semantic_types`` + ``chart_spec``) so the UI can later call
``assembleVegaLite()`` and drop this compiler in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Supported chart types and the encodings each one needs.
CHART_TYPES: dict[str, dict] = {
    "bar":         {"required": ["x", "y"], "optional": ["series"]},
    "hbar":        {"required": ["x", "y"], "optional": ["series"]},
    "stacked_bar": {"required": ["x", "y"], "optional": ["series"]},
    "line":        {"required": ["x", "y"], "optional": ["series"]},
    "area":        {"required": ["x", "y"], "optional": ["series"]},
    "scatter":     {"required": ["x", "y"], "optional": ["series", "size"]},
    "pie":         {"required": ["x", "y"], "optional": []},
    "donut":       {"required": ["x", "y"], "optional": []},
    "heatmap":     {"required": ["x", "y"], "optional": ["series"]},
    "waterfall":   {"required": ["x", "y"], "optional": []},
    "funnel":      {"required": ["x", "y"], "optional": []},
    "gauge":       {"required": ["y"], "optional": []},
    "treemap":     {"required": ["x", "y"], "optional": []},
    "table":       {"required": [], "optional": []},
    "metric":      {"required": ["y"], "optional": []},
}

#: Semantic types the renderer understands.
SEMANTIC_TYPES = {
    "Quantity",   # a measured amount: force a zero baseline, thousands separator
    "Currency",   # money: currency prefix
    "Percent",    # a proportion: percent suffix, no forced zero
    "YearMonth",  # a date or month: parsed temporally
    "Date",       # a full date
    "Category",   # an unordered label
    "Text",       # free text, not chartable on an axis
}

#: Types that can sit on an axis.
AXIS_TYPES = {"Quantity", "Currency", "Percent", "YearMonth", "Date", "Category"}

_ISO_DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
_YEAR_MONTH = re.compile(r"^\d{4}-\d{2}$")
_CURRENCY_HINT = re.compile(r"(amount|revenue|price|cost|revenue_total|sales|profit|mrr|arr)", re.I)
_PERCENT_HINT = re.compile(r"(pct|percent|rate|ratio|margin|share)", re.I)


@dataclass
class ChartSpec:
    """A validated chart request."""

    chart_type: str
    data_ref: str
    encodings: dict[str, str] = field(default_factory=dict)
    semantic_types: dict[str, str] = field(default_factory=dict)
    title: str = ""
    insight: str = ""
    layout: dict[str, int] = field(default_factory=dict)
    notes: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "chart_type": self.chart_type,
            "data_ref": self.data_ref,
            "encodings": self.encodings,
            "semantic_types": self.semantic_types,
            "title": self.title,
            "insight": self.insight,
            "layout": self.layout,
            "notes": self.notes,
            "warnings": self.warnings,
        }


@dataclass
class SpecValidation:
    ok: bool
    spec: ChartSpec | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "spec": self.spec.to_dict() if self.spec else None,
        }


def infer_semantic_type(column: str, values: list[Any] | None = None,
                        declared_type: str = "") -> str:
    """Infer what a column means from its name, its type and a sample of values.

    The model does not have to be right about this — an explicit
    ``semantic_types`` entry always wins, and a wrong guess degrades to a
    sensible default rather than an error.
    """
    if values:
        non_null = [v for v in values if v is not None and v != ""]
        if non_null:
            sample = non_null[:50]
            if all(isinstance(v, str) for v in sample):
                if all(_YEAR_MONTH.match(v.strip()) for v in sample):
                    return "YearMonth"
                if all(_ISO_DATE.match(v.strip()) for v in sample):
                    return "Date"
            if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in sample):
                if _PERCENT_HINT.search(column or ""):
                    return "Percent"
                if _CURRENCY_HINT.search(column or ""):
                    return "Currency"
                return "Quantity"
            if all(isinstance(v, str) for v in sample):
                return "Category"

    lowered = (declared_type or "").lower()
    if any(token in lowered for token in ("int", "real", "float", "double", "numeric", "decimal")):
        if _PERCENT_HINT.search(column or ""):
            return "Percent"
        if _CURRENCY_HINT.search(column or ""):
            return "Currency"
        return "Quantity"
    if any(token in lowered for token in ("date", "time", "timestamp")):
        return "Date"
    return "Category"


def validate_spec(
    payload: dict,
    available_columns: list[str] | None = None,
    sample_values: dict[str, list[Any]] | None = None,
    declared_types: dict[str, str] | None = None,
) -> SpecValidation:
    """Validate a chart request against the result it points at.

    ``available_columns`` is the column list of the referenced result. When it
    is ``None`` the field-existence check is skipped (used when validating a
    spec before its result exists).
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(payload, dict):
        return SpecValidation(ok=False, errors=["the chart spec must be an object"])

    chart_type = str(payload.get("chart_type") or "").strip().lower()
    if not chart_type:
        errors.append("chart_type is required")
    elif chart_type not in CHART_TYPES:
        errors.append(
            f"unknown chart_type '{chart_type}'. Supported types: "
            f"{', '.join(sorted(CHART_TYPES))}"
        )

    data_ref = str(payload.get("data_ref") or "").strip()
    if not data_ref:
        errors.append("data_ref is required: it must be the result_ref from a run_sql call")

    encodings_raw = payload.get("encodings") or {}
    if not isinstance(encodings_raw, dict):
        errors.append("encodings must be an object mapping chart roles to column names")
        encodings_raw = {}

    encodings: dict[str, str] = {}
    for role, value in encodings_raw.items():
        if role not in ("x", "y", "series", "size", "color"):
            errors.append(f"unknown encoding role '{role}'. Valid roles: x, y, series, size, color")
            continue
        if isinstance(value, list):
            if len(value) == 1:
                encodings[role] = str(value[0])
            else:
                warnings.append(f"encoding '{role}' had multiple fields; using the first")
                encodings[role] = str(value[0])
        elif value:
            encodings[role] = str(value)

    if chart_type in CHART_TYPES:
        for role in CHART_TYPES[chart_type]["required"]:
            if role not in encodings:
                errors.append(
                    f"chart_type '{chart_type}' requires the '{role}' encoding"
                )
        for role in encodings:
            allowed = set(CHART_TYPES[chart_type]["required"]) | set(
                CHART_TYPES[chart_type]["optional"]
            )
            if role not in allowed:
                errors.append(
                    f"chart_type '{chart_type}' does not use the '{role}' encoding"
                )

    # Field existence.
    if available_columns is not None:
        known = {c.lower(): c for c in available_columns}
        for role, column in encodings.items():
            if column.lower() not in known:
                errors.append(
                    f"the '{role}' field '{column}' is not a column in {data_ref}. "
                    f"Available columns: {', '.join(available_columns[:30])}"
                )

    # Semantic types.
    semantic_types: dict[str, str] = {}
    raw_types = payload.get("semantic_types") or {}
    if not isinstance(raw_types, dict):
        errors.append("semantic_types must be an object mapping column names to types")
        raw_types = {}

    for column, value in raw_types.items():
        semantic = str(value)
        if semantic not in SEMANTIC_TYPES:
            errors.append(
                f"unknown semantic type '{semantic}' for '{column}'. "
                f"Valid types: {', '.join(sorted(SEMANTIC_TYPES))}"
            )
            continue
        semantic_types[column] = semantic

    # Infer what the model did not declare.
    sample_values = sample_values or {}
    declared_types = declared_types or {}
    for role, column in encodings.items():
        if column in semantic_types:
            continue
        if available_columns is not None and column.lower() not in {
            c.lower() for c in available_columns
        }:
            continue
        canonical = column
        if available_columns is not None:
            for candidate in available_columns:
                if candidate.lower() == column.lower():
                    canonical = candidate
                    break
        semantic_types[canonical] = infer_semantic_type(
            column, sample_values.get(canonical), declared_types.get(canonical, "")
        )

    # An axis field must be chartable.
    for role in ("x", "y"):
        column = encodings.get(role)
        if not column:
            continue
        semantic = semantic_types.get(column)
        if semantic == "Text":
            errors.append(
                f"the '{role}' field '{column}' looks like free text and cannot be plotted on an axis"
            )

    layout = payload.get("layout") or {}
    if not isinstance(layout, dict):
        errors.append("layout must be an object")
        layout = {}
    clean_layout: dict[str, int] = {}
    for key in ("row", "col", "w", "h"):
        if key in layout:
            try:
                clean_layout[key] = int(layout[key])
            except (TypeError, ValueError):
                errors.append(f"layout.{key} must be an integer")

    spec = ChartSpec(
        chart_type=chart_type,
        data_ref=data_ref,
        encodings=encodings,
        semantic_types=semantic_types,
        title=str(payload.get("title") or ""),
        insight=str(payload.get("insight") or ""),
        layout=clean_layout,
        notes=str(payload.get("notes") or ""),
        warnings=warnings,
    )

    return SpecValidation(ok=not errors, spec=spec if not errors else None,
                          errors=errors, warnings=warnings)

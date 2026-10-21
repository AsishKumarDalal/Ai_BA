"""The chart IR: validation and semantic-type inference."""

from __future__ import annotations

from tsql.charts.spec import CHART_TYPES, SEMANTIC_TYPES, infer_semantic_type, validate_spec


def spec(**kwargs):
    base = {
        "chart_type": "bar",
        "data_ref": "tbl_1",
        "encodings": {"x": "region", "y": "revenue"},
    }
    base.update(kwargs)
    return base


# ------------------------------------------------------------------- validity
def test_a_valid_spec_passes():
    result = validate_spec(spec(), available_columns=["region", "revenue"])
    assert result.ok, result.errors
    assert result.spec.chart_type == "bar"
    assert result.spec.encodings == {"x": "region", "y": "revenue"}


def test_chart_type_is_required():
    result = validate_spec(spec(chart_type=""), available_columns=["region"])
    assert not result.ok
    assert "chart_type is required" in result.errors[0]


def test_unknown_chart_type_is_rejected():
    result = validate_spec(spec(chart_type="sunburst3d"), available_columns=["region"])
    assert not result.ok
    assert "unknown chart_type" in result.errors[0]


def test_data_ref_is_required():
    result = validate_spec(spec(data_ref=""), available_columns=["region"])
    assert not result.ok
    assert "data_ref is required" in result.errors[0]


def test_required_encoding_is_enforced():
    result = validate_spec(spec(encodings={"x": "region"}), available_columns=["region"])
    assert not result.ok
    assert any("requires the 'y' encoding" in e for e in result.errors)


def test_metric_needs_only_y():
    result = validate_spec(
        spec(chart_type="metric", encodings={"y": "revenue"}), available_columns=["revenue"]
    )
    assert result.ok, result.errors


def test_table_needs_no_encodings():
    result = validate_spec(
        spec(chart_type="table", encodings={}), available_columns=["region"]
    )
    assert result.ok, result.errors


def test_encoding_role_must_be_known():
    result = validate_spec(spec(encodings={"x": "region", "y": "revenue", "z": "nope"}),
                            available_columns=["region", "revenue"])
    assert not result.ok
    assert any("unknown encoding role" in e for e in result.errors)


def test_a_field_that_does_not_exist_is_rejected():
    result = validate_spec(spec(encodings={"x": "nope", "y": "revenue"}),
                            available_columns=["region", "revenue"])
    assert not result.ok
    assert any("not a column" in e for e in result.errors)
    assert any("region" in e for e in result.errors)


def test_unknown_semantic_type_is_rejected():
    result = validate_spec(spec(semantic_types={"revenue": "Money"}),
                            available_columns=["region", "revenue"])
    assert not result.ok
    assert any("unknown semantic type" in e for e in result.errors)


def test_text_cannot_be_an_axis():
    result = validate_spec(
        spec(encodings={"x": "note", "y": "revenue"},
             semantic_types={"note": "Text"}),
        available_columns=["note", "revenue"],
    )
    assert not result.ok
    assert any("free text" in e for e in result.errors)


def test_layout_values_must_be_integers():
    result = validate_spec(spec(layout={"row": "top"}), available_columns=["region"])
    assert not result.ok
    assert any("layout.row must be an integer" in e for e in result.errors)


# --------------------------------------------------------------- inference
def test_semantic_types_are_inferred_when_omitted():
    result = validate_spec(
        spec(encodings={"x": "region", "y": "units"}),
        available_columns=["region", "units"],
        sample_values={"region": ["East", "West"], "units": [100.0, 200.0]},
    )
    assert result.ok
    assert result.spec.semantic_types["region"] == "Category"
    assert result.spec.semantic_types["units"] == "Quantity"


def test_money_named_columns_infer_as_currency():
    # "revenue" is money, so the compiler picks a currency format for it. The
    # model can override this with an explicit semantic_types entry.
    result = validate_spec(
        spec(encodings={"x": "region", "y": "revenue"}),
        available_columns=["region", "revenue"],
        sample_values={"revenue": [100.0, 200.0]},
    )
    assert result.spec.semantic_types["revenue"] == "Currency"


def test_an_explicit_semantic_type_wins():
    result = validate_spec(
        spec(encodings={"x": "region", "y": "revenue"},
             semantic_types={"revenue": "Percent"}),
        available_columns=["region", "revenue"],
        sample_values={"revenue": [100.0, 200.0]},
    )
    assert result.spec.semantic_types["revenue"] == "Percent"


def test_year_month_values_are_temporal():
    result = validate_spec(
        spec(chart_type="line", encodings={"x": "month", "y": "revenue"}),
        available_columns=["month", "revenue"],
        sample_values={"month": ["2024-01", "2024-02", "2024-03"]},
    )
    assert result.spec.semantic_types["month"] == "YearMonth"


def test_infer_semantic_type_from_values():
    assert infer_semantic_type("x", ["2024-01", "2024-02"]) == "YearMonth"
    assert infer_semantic_type("x", ["2024-01-05", "2024-02-06"]) == "Date"
    assert infer_semantic_type("x", [1, 2, 3]) == "Quantity"
    assert infer_semantic_type("x", ["a", "b"]) == "Category"


def test_infer_semantic_type_from_the_name():
    assert infer_semantic_type("conversion_rate", [0.1, 0.2]) == "Percent"
    assert infer_semantic_type("revenue", [1.0, 2.0]) == "Currency"


def test_infer_semantic_type_falls_back_to_the_declared_type():
    assert infer_semantic_type("x", None, "INTEGER") == "Quantity"
    assert infer_semantic_type("x", None, "TIMESTAMP") == "Date"
    assert infer_semantic_type("x", None, "TEXT") == "Category"


def test_empty_sample_does_not_crash():
    assert infer_semantic_type("x", [None, None]) == "Category"
    assert infer_semantic_type("x", []) == "Category"


# ------------------------------------------------------------------- catalog
def test_chart_type_table_is_consistent():
    for name, definition in CHART_TYPES.items():
        assert set(definition["required"]) <= {"x", "y"}, name
        assert set(definition["optional"]) <= {"x", "y", "series", "size"}, name
        assert name == name.lower()


def test_semantic_types_are_a_closed_set():
    assert "Quantity" in SEMANTIC_TYPES
    assert "YearMonth" in SEMANTIC_TYPES

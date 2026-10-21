"""Tool registry, auto-discovery and toolset resolution."""

from __future__ import annotations

import json

import pytest

from tsql.tools.registry import (
    ToolRegistry,
    _module_registers_tools,
    discover_builtin_tools,
)
from tsql.tools.toolsets import (
    PRESETS,
    TOOLSETS,
    get_tool_definitions,
    resolve_preset,
    resolve_toolset,
)


def register_dummy(registry: ToolRegistry, name="run_sql", toolset="sql", **kwargs):
    return registry.register(
        name=name,
        toolset=toolset,
        schema={"description": f"run {name}", "parameters": {"type": "object"}},
        handler=kwargs.pop("handler", lambda args, **kw: json.dumps({"ok": True})),
        **kwargs,
    )


# ------------------------------------------------------------------ registration
def test_register_and_get():
    registry = ToolRegistry()
    register_dummy(registry)
    assert registry.get("run_sql") is not None
    assert registry.names() == ["run_sql"]


def test_re_registering_in_the_same_toolset_is_a_noop():
    registry = ToolRegistry()
    register_dummy(registry)
    entry = register_dummy(registry)
    assert registry.names() == ["run_sql"]
    assert entry is registry.get("run_sql")


def test_shadowing_another_toolset_is_rejected():
    registry = ToolRegistry()
    register_dummy(registry, name="x", toolset="sql")
    with pytest.raises(ValueError):
        register_dummy(registry, name="x", toolset="charts")


def test_shadowing_can_be_overridden():
    registry = ToolRegistry()
    register_dummy(registry, name="x", toolset="sql")
    register_dummy(registry, name="x", toolset="charts", override=True)
    assert registry.get("x").toolset == "charts"


# ------------------------------------------------------------------- availability
def test_check_fn_false_hides_the_tool():
    registry = ToolRegistry()
    register_dummy(registry, check_fn=lambda: False)
    assert registry.available_entries() == []
    assert registry.specs() == []


def test_check_fn_exception_means_unavailable():
    def broken():
        raise RuntimeError("nope")

    registry = ToolRegistry()
    register_dummy(registry, check_fn=broken)
    assert registry.available_entries() == []


def test_shared_check_fn_is_evaluated_once():
    calls = []

    def check():
        calls.append(1)
        return True

    registry = ToolRegistry()
    register_dummy(registry, name="a", check_fn=check)
    register_dummy(registry, name="b", check_fn=check)
    registry.available_entries()
    assert len(calls) == 1


# ---------------------------------------------------------------------- dispatch
def test_dispatch_returns_a_json_string():
    registry = ToolRegistry()
    register_dummy(registry, handler=lambda args, **kw: json.dumps({"value": 7}))
    assert json.loads(registry.dispatch("run_sql", {}))["value"] == 7


def test_dispatch_wraps_exceptions():
    def boom(args, **kw):
        raise ValueError("bad")

    registry = ToolRegistry()
    register_dummy(registry, handler=boom)
    result = json.loads(registry.dispatch("run_sql", {}))
    assert "error" in result
    assert "bad" in result["error"]


def test_dispatch_of_unknown_tool_is_an_error_not_a_crash():
    registry = ToolRegistry()
    result = json.loads(registry.dispatch("nope", {}))
    assert "unknown tool" in result["error"]


def test_dispatch_json_encodes_dict_returns():
    registry = ToolRegistry()
    register_dummy(registry, handler=lambda args, **kw: {"value": 1})
    assert json.loads(registry.dispatch("run_sql", {}))["value"] == 1


# ------------------------------------------------------------------- discovery
def test_ast_detection_finds_top_level_register_calls(tmp_path):
    module = tmp_path / "my_tool.py"
    module.write_text(
        "from tsql.tools.registry import registry\n"
        "\n"
        "registry.register(name='a', toolset='t', schema={}, handler=lambda a, **k: '')\n"
    )
    assert _module_registers_tools(module) is True


def test_ast_detection_ignores_registration_inside_a_function(tmp_path):
    module = tmp_path / "helper.py"
    module.write_text(
        "def setup():\n"
        "    registry.register(name='a', toolset='t', schema={}, handler=lambda a, **k: '')\n"
    )
    assert _module_registers_tools(module) is False


def test_ast_detection_ignores_other_objects(tmp_path):
    module = tmp_path / "other.py"
    module.write_text("other.register(name='a')\n")
    assert _module_registers_tools(module) is False


def test_discover_builtin_tools_is_safe_to_call():
    # Discovery must not raise even when no tool modules exist yet.
    assert isinstance(discover_builtin_tools(), list)


# -------------------------------------------------------------------- toolsets
def test_resolve_toolset_expands_to_tool_names():
    assert resolve_toolset("sql") == ["run_sql"]
    assert "add_panel" in resolve_toolset("charts")


def test_resolve_preset_expands_multiple_toolsets():
    names = resolve_preset("analyst")
    assert "run_sql" in names
    assert "add_panel" in names


def test_unknown_toolset_resolves_to_nothing():
    assert resolve_toolset("nope") == []
    assert resolve_preset("nope") == []


def test_get_tool_definitions_filters_by_enabled_toolsets():
    registry = ToolRegistry()
    register_dummy(registry, name="run_sql", toolset="sql")
    register_dummy(registry, name="add_panel", toolset="charts")
    specs = get_tool_definitions(enabled_toolsets=["sql"], registry=registry)
    assert [s.name for s in specs] == ["run_sql"]


def test_get_tool_definitions_subtracts_disabled_toolsets():
    registry = ToolRegistry()
    register_dummy(registry, name="run_sql", toolset="sql")
    register_dummy(registry, name="add_panel", toolset="charts")
    specs = get_tool_definitions(disabled_toolsets=["sql"], registry=registry)
    assert [s.name for s in specs] == ["add_panel"]


def test_get_tool_definitions_without_filters_returns_everything_available():
    registry = ToolRegistry()
    register_dummy(registry, name="run_sql", toolset="sql")
    register_dummy(registry, name="hidden", toolset="sql", check_fn=lambda: False)
    specs = get_tool_definitions(registry=registry)
    assert [s.name for s in specs] == ["run_sql"]


def test_preset_can_be_used_as_an_enabled_toolset():
    registry = ToolRegistry()
    register_dummy(registry, name="run_sql", toolset="sql")
    register_dummy(registry, name="add_panel", toolset="charts")
    specs = get_tool_definitions(enabled_toolsets=["analyst"], registry=registry)
    assert {s.name for s in specs} == {"run_sql", "add_panel"}


def test_every_declared_toolset_member_is_a_known_tool_name():
    """Toolsets may name tools that are registered later; the names must be stable."""
    known = {
        "run_sql", "add_panel", "describe_table", "list_tables",
        "profile_table", "todo_list",
    }
    for toolset in TOOLSETS.values():
        for name in toolset["tools"]:
            assert name in known, f"{name} is declared in a toolset but is not a known tool"


def test_toolsets_and_presets_reference_declared_toolsets():
    for preset, toolsets in PRESETS.items():
        for toolset in toolsets:
            assert toolset in TOOLSETS, f"preset {preset} references unknown toolset {toolset}"

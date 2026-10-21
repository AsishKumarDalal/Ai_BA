"""Toolsets — named bundles of tools, resolved per platform/session.

A toolset is a logical grouping. A preset is a named collection of toolsets.
Resolution order (mirrors Hermes):

1. explicit ``enabled`` list  -> only those toolsets
2. explicit ``disabled`` list -> everything except those
3. neither                    -> everything known
"""

from __future__ import annotations

from typing import Iterable

from .registry import ToolRegistry, registry as default_registry

#: name -> {"description": str, "tools": [str], "includes": [str]}
TOOLSETS: dict[str, dict] = {
    "sql": {
        "description": "Execute read-only SQL against the connected database.",
        "tools": ["run_sql"],
        "includes": [],
    },
    "charts": {
        "description": "Build chart panels and assemble dashboards.",
        "tools": ["add_panel"],
        "includes": [],
    },
    "database": {
        "description": "Inspect the database schema and profile table data.",
        "tools": ["describe_table", "list_tables", "profile_table"],
        "includes": [],
    },
    "planning": {
        "description": "Track a plan with a todo list.",
        "tools": ["todo_list"],
        "includes": [],
    },
}

#: Named collections of toolsets.
PRESETS: dict[str, list[str]] = {
    "analyst": ["sql", "charts"],
    "analyst-full": ["sql", "charts", "database", "planning"],
    "minimal": ["sql"],
    "safe": [],
}


def resolve_toolset(name: str, registry: ToolRegistry | None = None) -> list[str]:
    """Expand a toolset name into the list of tool names it contains."""
    registry = registry or default_registry
    definition = TOOLSETS.get(name)
    if definition is None:
        return []
    names: list[str] = list(definition.get("tools", []))
    for included in definition.get("includes", []):
        names.extend(resolve_toolset(included, registry))
    return names


def resolve_preset(name: str, registry: ToolRegistry | None = None) -> list[str]:
    """Expand a preset into the list of tool names it contains."""
    registry = registry or default_registry
    names: list[str] = []
    for toolset in PRESETS.get(name, []):
        names.extend(resolve_toolset(toolset, registry))
    return names


def get_tool_definitions(
    enabled_toolsets: Iterable[str] | None = None,
    disabled_toolsets: Iterable[str] | None = None,
    registry: ToolRegistry | None = None,
    context: object | None = None,
) -> list:
    """Build the model-facing tool list.

    * ``enabled_toolsets`` may name toolsets *or* presets *or* bare tool names.
    * ``disabled_toolsets`` subtracts from everything known.
    * Tools whose ``check_fn`` fails are omitted entirely, so the model is
      never offered a tool that cannot run. A check that accepts a context
      receives it, so a tool can require a live database connection.
    """
    registry = registry or default_registry

    if enabled_toolsets is not None:
        wanted: set[str] = set()
        for name in enabled_toolsets:
            if name in TOOLSETS:
                wanted.update(resolve_toolset(name, registry))
            elif name in PRESETS:
                wanted.update(resolve_preset(name, registry))
            else:
                wanted.add(name)  # treat a bare name as a tool name
        entries = [e for e in registry.available_entries(context) if e.name in wanted]
    elif disabled_toolsets is not None:
        excluded: set[str] = set()
        for name in disabled_toolsets:
            if name in TOOLSETS:
                excluded.update(resolve_toolset(name, registry))
            elif name in PRESETS:
                excluded.update(resolve_preset(name, registry))
            else:
                excluded.add(name)
        entries = [e for e in registry.available_entries(context) if e.name not in excluded]
    else:
        entries = registry.available_entries(context)

    return [entry.to_spec() for entry in entries]

"""Tool registry.

Self-registering, like Hermes: a tool module calls ``registry.register(...)`` at
import time and is auto-discovered by an AST scan of the package directory. New
tool files need no wiring anywhere else.

Contract with the model:

* A handler returns a **JSON string**. Never a dict, never an exception.
* Errors are returned as ``{"error": "..."}`` — the model must always receive
  a well-formed result it can reason about and repair.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import logging
import pkgutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..providers.base import ToolSpec

logger = logging.getLogger(__name__)

Handler = Callable[..., Any]
CheckFn = Callable[[], bool]


@dataclass
class ToolEntry:
    name: str
    toolset: str
    schema: dict
    handler: Handler
    check_fn: CheckFn | None = None
    requires_env: list[str] = field(default_factory=list)
    is_async: bool = False
    description: str = ""
    emoji: str = ""

    def to_spec(self) -> ToolSpec:
        parameters = self.schema.get("parameters") or {"type": "object", "properties": {}}
        description = self.schema.get("description") or self.description
        return ToolSpec(name=self.name, description=description, parameters=parameters)

    def available(self) -> bool:
        """Run the availability check. An exception means *unavailable* (fail safe)."""
        if self.check_fn is None:
            return True
        try:
            return bool(self.check_fn())
        except Exception:  # noqa: BLE001 - fail closed
            return False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolEntry] = {}

    # ------------------------------------------------------------ registration
    def register(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Handler,
        check_fn: CheckFn | None = None,
        requires_env: list[str] | None = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        override: bool = False,
    ) -> ToolEntry:
        existing = self._tools.get(name)
        if existing is not None and existing.toolset != toolset and not override:
            raise ValueError(
                f"tool '{name}' is already registered in toolset '{existing.toolset}'; "
                f"pass override=True to replace it"
            )
        if existing is not None and existing.toolset == toolset and not override:
            # Re-registration in the same toolset is a no-op, which keeps
            # module reloads (tests, hot reload) harmless.
            return existing

        entry = ToolEntry(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            requires_env=list(requires_env or []),
            is_async=is_async,
            description=description or schema.get("description", ""),
            emoji=emoji,
        )
        self._tools[name] = entry
        return entry

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def clear(self) -> None:
        self._tools.clear()

    # ------------------------------------------------------------------ access
    def get(self, name: str) -> ToolEntry | None:
        return self._tools.get(name)

    def entries(self) -> list[ToolEntry]:
        return list(self._tools.values())

    @property
    def tools(self) -> dict[str, ToolEntry]:
        return dict(self._tools)

    def names(self) -> list[str]:
        return list(self._tools)

    def toolsets(self) -> set[str]:
        return {entry.toolset for entry in self._tools.values()}

    def available_entries(self, context: Any = None) -> list[ToolEntry]:
        """Entries whose ``check_fn`` passes. Check results are cached per call.

        A ``check_fn`` may take a single argument, in which case it receives the
        call context — that is how a tool declares "I need a live database
        connection", which is only knowable at request time.
        """
        cache: dict[int, bool] = {}
        available: list[ToolEntry] = []
        for entry in self._tools.values():
            if entry.check_fn is None:
                available.append(entry)
                continue
            key = id(entry.check_fn)
            if key not in cache:
                cache[key] = _call_check(entry.check_fn, context)
            if cache[key]:
                available.append(entry)
        return available

    def specs(self, context: Any = None) -> list[ToolSpec]:
        return [entry.to_spec() for entry in self.available_entries(context)]

    # ---------------------------------------------------------------- dispatch
    def dispatch(self, name: str, args: dict, **kwargs: Any) -> str:
        """Execute a tool. Never raises — errors come back as JSON."""
        entry = self._tools.get(name)
        if entry is None:
            return json.dumps({"error": f"unknown tool: {name}"})
        try:
            result = entry.handler(args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - normalized for the model
            logger.exception("tool %s raised", name)
            return json.dumps({
                "error": f"Tool execution failed: {type(exc).__name__}: {exc}",
                "tool": name,
            })
        if isinstance(result, str):
            return result
        try:
            return json.dumps(result, default=str)
        except (TypeError, ValueError):
            return json.dumps({"error": "tool returned a value that is not JSON-serializable",
                               "tool": name})


#: The process-wide registry that ``registry.register(...)`` at module level binds to.
registry = ToolRegistry()


def _call_check(check_fn: CheckFn, context: Any) -> bool:
    """Call a check function, passing the context when it accepts one.

    An exception means *unavailable* — the check fails safe.
    """
    try:
        if context is not None:
            try:
                signature = inspect.signature(check_fn)
                if len(signature.parameters) >= 1:
                    return bool(check_fn(context))
            except (TypeError, ValueError):
                pass
        return bool(check_fn())
    except Exception:  # noqa: BLE001 - fail closed
        return False


def discover_builtin_tools(package: str = "tsql.tools", tools_dir: Path | None = None) -> list[str]:
    """Import every tool module that calls ``registry.register()`` at top level.

    Uses AST parsing rather than importing everything, so helper modules in the
    package are not imported for nothing. Modules that fail to import (a missing
    optional dependency, say) are logged and skipped — one broken tool must not
    take the registry down.
    """
    discovered: list[str] = []
    if tools_dir is None:
        try:
            module = importlib.import_module(package)
        except ImportError:
            return discovered
        tools_dir = Path(module.__file__).parent

    for path in sorted(tools_dir.glob("*.py")):
        if path.name in ("__init__.py", "registry.py"):
            continue
        if not _module_registers_tools(path):
            continue
        module_name = f"{package}.{path.stem}"
        try:
            importlib.import_module(module_name)
            discovered.append(module_name)
        except Exception:  # noqa: BLE001 - one bad tool must not break the rest
            logger.warning("failed to import tool module %s", module_name, exc_info=True)
    return discovered


def _module_registers_tools(path: Path) -> bool:
    """True when the file contains a top-level ``registry.register(...)`` call."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    for node in tree.body:
        if not isinstance(node, ast.Expr):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not isinstance(func, ast.Attribute) or func.attr != "register":
            continue
        if isinstance(func.value, ast.Name) and func.value.id == "registry":
            return True
    return False


def iter_tool_modules(package: str = "tsql.tools") -> list[str]:
    """All module names inside a tool package (used by tests)."""
    try:
        module = importlib.import_module(package)
    except ImportError:
        return []
    path = Path(module.__file__).parent
    return [f"{package}.{info.name}" for info in pkgutil.iter_modules([str(path)])]

"""The AST guard — the only path from SQL text to execution.

Design rules, borrowed from the SQL-guard projects and hardened further:

* **Validate on the AST, never on the string.** A query must be *understood*.
  ``SELEct/**/1`` and ``SELECT 'DROP TABLE'`` are data, not code.
* **Fail closed.** A parse error is a denial, not a pass.
* **One statement.** ``SELECT 1; DROP TABLE orders`` is refused.
* **Read-only shape.** The root must be a query and no write node may appear
  anywhere, including inside a CTE.
* **Allowlist, CTE-aware.** ``WITH x AS (...) SELECT * FROM x`` must not be
  read as a reference to a table named ``x``.
* **Forced, capped LIMIT.** A query that returns a million rows is a denial
  with a reason, not a bill.

The database's read-only role is still the real guarantee. This is the fast
second layer that turns a dangerous query into an actionable error *before* it
reaches the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from .policy import DANGEROUS_FUNCTIONS, Policy

#: Nodes that write, define, or otherwise change state.
WRITE_NODES: tuple[type, ...] = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge,
    exp.Create, exp.Drop, exp.Alter, exp.Command,
    exp.Grant, exp.Revoke,
    exp.Copy, exp.Attach, exp.Detach,
    exp.Use, exp.Pragma, exp.Set, exp.Transaction,
    exp.Commit, exp.Rollback, exp.Lock, exp.TruncateTable,
    exp.Into,
)

#: Cost bands.
CHEAP = "cheap"
MODERATE = "moderate"
EXPENSIVE = "expensive"


@dataclass
class GuardResult:
    ok: bool
    sql: str = ""
    original: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    cost: str = CHEAP
    estimated_rows: int = 0
    limit_applied: int | None = None
    denied_by: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "sql": self.sql,
            "errors": self.errors,
            "warnings": self.warnings,
            "tables": self.tables,
            "columns": self.columns,
            "cost": self.cost,
            "estimated_rows": self.estimated_rows,
            "limit_applied": self.limit_applied,
            "denied_by": self.denied_by,
        }


class SqlGuard:
    """Validates a statement against a :class:`Policy`."""

    def __init__(self, policy: Policy, row_estimates: dict[str, int] | None = None):
        self.policy = policy
        self.row_estimates = {k.lower(): v for k, v in (row_estimates or {}).items()}

    # ------------------------------------------------------------------ entry
    def validate(self, sql: str) -> GuardResult:
        original = sql or ""
        result = GuardResult(ok=False, original=original)

        if not original.strip():
            result.errors.append("the statement is empty")
            result.denied_by = "empty_statement"
            return result

        # 1. Parse. A parse error is a denial, never a pass.
        try:
            statements = sqlglot.parse(original, read=self.policy.dialect)
        except (ParseError, TokenError) as exc:
            result.errors.append(f"the statement could not be parsed: {exc}")
            result.denied_by = "parse_error"
            return result
        except Exception as exc:  # noqa: BLE001 - any parser failure is a denial
            result.errors.append(f"the statement could not be parsed: {exc}")
            result.denied_by = "parse_error"
            return result

        if len(statements) != 1:
            result.errors.append(
                "exactly one statement is allowed; "
                f"{len(statements)} were supplied. Split the query or use a single SELECT."
            )
            result.denied_by = "multiple_statements"
            return result

        tree = statements[0]
        if tree is None:
            result.errors.append("the statement is empty")
            result.denied_by = "empty_statement"
            return result

        # 2. Read-only shape.
        if not isinstance(tree, exp.Query):
            result.errors.append(
                f"only read queries are allowed; got {type(tree).__name__.lower()}. "
                "The agent may run SELECT statements only."
            )
            result.denied_by = "not_a_query"
            return result

        # 3. No write node anywhere, including inside CTEs and subqueries.
        writers = sorted({type(node).__name__ for node in tree.find_all(*WRITE_NODES)})
        if writers:
            result.errors.append(
                f"the statement contains write or side-effecting operations ({', '.join(writers)}); "
                "only read queries are allowed"
            )
            result.denied_by = "write_operation"
            return result

        # 4. Tables.
        tables, table_errors = self._collect_tables(tree)
        result.tables = tables
        if table_errors:
            result.errors.extend(table_errors)
            result.denied_by = "table_not_allowed"
            return result

        # 5. Functions.
        function_errors = self._check_functions(tree)
        if function_errors:
            result.errors.extend(function_errors)
            result.denied_by = "dangerous_function"
            return result

        # 6. SELECT *.
        if self.policy.forbid_select_star:
            star_errors = self._check_select_star(tree)
            if star_errors:
                result.errors.extend(star_errors)
                result.denied_by = "select_star"
                return result

        result.columns = self._collect_columns(tree)

        # 7. LIMIT.
        tree, limit_note, limit_error = self._apply_limit(tree)
        if limit_error:
            result.errors.append(limit_error)
            result.denied_by = "limit"
            return result
        if limit_note:
            result.warnings.append(limit_note)
        result.limit_applied = self._read_limit(tree)

        # 8. Cost.
        result.estimated_rows = self._estimate_rows(tables)
        result.cost = self._classify_cost(result.estimated_rows)
        if result.cost == EXPENSIVE and not self.policy.allow_expensive_queries:
            result.warnings.append(
                f"this query touches an estimated {result.estimated_rows:,} rows; "
                "narrow it, or run it against a read replica"
            )

        # 9. Normalize.
        try:
            result.sql = tree.sql(dialect=self.policy.dialect, pretty=False)
        except Exception:  # noqa: BLE001 - keep the original if normalization fails
            result.sql = original

        result.ok = True
        return result

    # ----------------------------------------------------------------- tables
    def _collect_tables(self, tree: exp.Expression) -> tuple[list[str], list[str]]:
        cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
        errors: list[str] = []
        tables: list[str] = []

        for node in tree.find_all(exp.Table):
            # A table-valued function (read_csv(...), range(...)) shows up here.
            if isinstance(node.this, exp.Func):
                name = _function_name(node.this)
                if name and name.lower() in DANGEROUS_FUNCTIONS:
                    errors.append(
                        f"the function '{name}' is not allowed: it can read files or reach "
                        "outside the database"
                    )
                continue

            schema = (node.db or "").lower()
            name = (node.name or "").lower()
            if not name:
                continue

            qualified = f"{schema}.{name}" if schema else name
            if schema and schema in self.policy.forbidden_schemas:
                errors.append(
                    f"the system schema '{schema}' may not be queried"
                )
                continue
            if name in cte_names and not schema:
                # A CTE alias, not a table.
                continue
            if not self.policy.is_table_allowed(qualified) and not self.policy.is_table_allowed(name):
                allowed = self.policy.allowed_tables_for_prompt()
                if allowed:
                    errors.append(
                        f"unknown table: '{qualified}'. Allowed tables: {', '.join(sorted(allowed))}"
                    )
                else:
                    errors.append(f"unknown table: '{qualified}'")
                continue
            if qualified not in tables:
                tables.append(qualified)

        return tables, errors

    # -------------------------------------------------------------- functions
    def _check_functions(self, tree: exp.Expression) -> list[str]:
        if not self.policy.forbid_dangerous_functions:
            return []
        errors: list[str] = []
        seen: set[str] = set()
        for name in _collect_function_names(tree):
            if name not in _normalized_denylist() or name in seen:
                continue
            seen.add(name)
            errors.append(
                f"the function '{name}' is not allowed: it can read files, open "
                "network connections, or execute code"
            )
        return errors

    # ------------------------------------------------------------- select star
    def _check_select_star(self, tree: exp.Expression) -> list[str]:
        errors: list[str] = []
        for select in tree.find_all(exp.Select):
            for projection in select.expressions:
                if isinstance(projection, exp.Star):
                    errors.append(
                        "SELECT * is not allowed: name the columns you need. "
                        "Explicit columns are faster and avoid pulling personal data."
                    )
                elif isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
                    errors.append(
                        "SELECT alias.* is not allowed: name the columns you need."
                    )
        return errors

    # ------------------------------------------------------------------ limit
    def _apply_limit(self, tree: exp.Expression) -> tuple[exp.Expression, str | None, str | None]:
        max_rows = max(1, int(self.policy.max_rows))
        limit = tree.args.get("limit")

        if limit is None:
            new_tree = tree.limit(max_rows)
            return new_tree, f"a LIMIT of {max_rows} was added", None

        expression = limit.expression if isinstance(limit, exp.Limit) else None
        if expression is None:
            return tree, None, "the LIMIT clause could not be read"

        if isinstance(expression, exp.Literal):
            try:
                value = int(expression.name)
            except (TypeError, ValueError):
                return tree, None, "the LIMIT must be an integer"
            if value > max_rows:
                new_tree = tree.limit(max_rows)
                return new_tree, f"the LIMIT was reduced from {value} to {max_rows}", None
            return tree, None, None

        if isinstance(expression, exp.Placeholder):
            return tree, None, (
                "the LIMIT must be a literal integer, not a parameter"
            )

        return tree, None, "the LIMIT must be a literal integer"

    def _read_limit(self, tree: exp.Expression) -> int | None:
        limit = tree.args.get("limit")
        if limit is None:
            return None
        expression = limit.expression if isinstance(limit, exp.Limit) else None
        if isinstance(expression, exp.Literal):
            try:
                return int(expression.name)
            except (TypeError, ValueError):
                return None
        return None

    # ------------------------------------------------------------------- cost
    def _estimate_rows(self, tables: list[str]) -> int:
        total = 0
        for table in tables:
            total += self.row_estimates.get(table.lower(), 0)
        return total

    def _classify_cost(self, estimated_rows: int) -> str:
        if estimated_rows <= 0:
            return CHEAP
        if estimated_rows <= 100_000:
            return MODERATE
        if estimated_rows <= self.policy.expensive_row_threshold:
            return MODERATE
        return EXPENSIVE

    # ---------------------------------------------------------------- columns
    def _collect_columns(self, tree: exp.Expression) -> list[str]:
        columns: list[str] = []
        for node in tree.find_all(exp.Column):
            name = node.name
            if name and name not in columns:
                columns.append(name)
        return columns


def _function_name(node: Any) -> str:
    """Best-effort name of a function node."""
    if isinstance(node, exp.Anonymous):
        return str(node.this or "")
    if isinstance(node, exp.Func):
        try:
            return node.sql_name()
        except Exception:  # noqa: BLE001
            return type(node).__name__
    return ""


def _collect_function_names(tree: exp.Expression) -> set[str]:
    """Every function called anywhere in the statement, normalized.

    Two sources are needed. An unknown function parses as ``exp.Anonymous``, whose
    real name is ``.this`` (its ``sql_name()`` is useless — it returns
    "ANONYMOUS"). A function the parser *knows* (``read_csv``, ``load_extension``
    in some dialects) is a dedicated class whose ``sql_name()`` gives the SQL
    name. Checking only one of the two misses real escapes.
    """
    names: set[str] = set()
    for node in tree.find_all(exp.Func):
        name = _function_name(node)
        if name and name.upper() != "ANONYMOUS":
            names.add(_normalize_function_name(name))
    return names


def _normalize_function_name(name: str) -> str:
    return name.strip().lower().replace("_", "")


def _normalized_denylist() -> set[str]:
    return {_normalize_function_name(name) for name in DANGEROUS_FUNCTIONS}


def validate_sql(sql: str, policy: Policy, row_estimates: dict[str, int] | None = None) -> GuardResult:
    """Convenience wrapper around :class:`SqlGuard`."""
    return SqlGuard(policy, row_estimates=row_estimates).validate(sql)

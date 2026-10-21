"""Schema catalog.

The catalog is the harness's view of the connected database: what tables exist,
what they contain, and roughly how big they are. It is cached, because
introspection on a wide schema is not free and the agent asks about the same
schema repeatedly.

The catalog's job in v0 is to *fill the system prompt* — the model needs to
know the column names to write correct SQL. A lookup tool is a v1 addition,
added only if the eval shows the prompt-injection approach is not enough.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .connection import Database


@dataclass
class TableInfo:
    name: str
    kind: str = "table"
    row_estimate: int = 0
    columns: list[dict] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "row_estimate": self.row_estimate,
            "columns": self.columns,
            "primary_key": self.primary_key,
            "foreign_keys": self.foreign_keys,
        }


@dataclass
class Catalog:
    tables: list[TableInfo] = field(default_factory=list)
    built_at: float = 0.0
    label: str = ""
    dialect: str = ""

    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]

    def get(self, name: str) -> TableInfo | None:
        for table in self.tables:
            if table.name.lower() == name.lower():
                return table
        return None

    def row_estimates(self) -> dict[str, int]:
        """Table -> row estimate, used by the guard to price a query."""
        return {t.name.lower(): t.row_estimate for t in self.tables}

    def to_prompt_block(self, max_columns_per_table: int = 40) -> str:
        """Render the schema as compact text for the system prompt."""
        if not self.tables:
            return "(no tables visible)"

        lines: list[str] = []
        for table in self.tables:
            header = f"{table.name} ({table.kind}"
            if table.row_estimate:
                header += f", ~{table.row_estimate:,} rows"
            header += ")"
            lines.append(header)
            for column in table.columns[:max_columns_per_table]:
                parts = [column.get("name", "")]
                if column.get("type"):
                    parts.append(column["type"])
                flags = []
                if column.get("is_pk"):
                    flags.append("pk")
                if column.get("is_fk") and column.get("fk_target"):
                    flags.append(f"fk->{column['fk_target']}")
                if not column.get("nullable", True):
                    flags.append("not null")
                if flags:
                    parts.append("(" + ", ".join(flags) + ")")
                lines.append("  - " + " ".join(parts))
            if len(table.columns) > max_columns_per_table:
                lines.append(f"  … and {len(table.columns) - max_columns_per_table} more columns")
            if table.foreign_keys:
                pairs = ", ".join(
                    f"{fk['column']} -> {fk['references']}" for fk in table.foreign_keys
                )
                lines.append(f"  foreign keys: {pairs}")
        return "\n".join(lines)


class CatalogBuilder:
    """Builds and caches the catalog for one database connection."""

    def __init__(self, db: Database, ttl_seconds: float = 300.0):
        self._db = db
        self._ttl = ttl_seconds
        self._catalog: Catalog | None = None
        self._built_at = 0.0

    def get(self, force: bool = False) -> Catalog:
        if not force and self._catalog is not None:
            if (time.time() - self._built_at) < self._ttl:
                return self._catalog
        self._catalog = self._build()
        self._built_at = time.time()
        return self._catalog

    def invalidate(self) -> None:
        self._catalog = None

    def _build(self) -> Catalog:
        catalog = Catalog(built_at=time.time(), dialect=self._db.dialect)
        try:
            catalog.label = self._db.info.label
        except Exception:  # noqa: BLE001
            catalog.label = self._db.dialect

        for entry in self._db.list_tables():
            table = TableInfo(
                name=entry.get("name", ""),
                kind=entry.get("kind", "table"),
                row_estimate=int(entry.get("row_estimate") or 0),
            )
            try:
                described = self._db.describe_table(table.name)
                table.columns = described.get("columns", [])
                table.primary_key = described.get("primary_key", [])
                table.foreign_keys = described.get("foreign_keys", [])
            except Exception:  # noqa: BLE001 - one unreadable table must not break the catalog
                table.columns = []
            catalog.tables.append(table)
        return catalog

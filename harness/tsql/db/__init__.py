"""Database access layer: read-only connections, introspection, catalog."""

from .catalog import Catalog, CatalogBuilder, TableInfo
from .connection import (
    ColumnInfo,
    ConnectionInfo,
    Database,
    QueryResult,
    connect_database,
)

__all__ = [
    "Catalog",
    "CatalogBuilder",
    "ColumnInfo",
    "ConnectionInfo",
    "Database",
    "QueryResult",
    "TableInfo",
    "connect_database",
]

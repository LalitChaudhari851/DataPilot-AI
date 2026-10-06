"""
SQLite Database Pool — Drop-in compatible with DatabasePool for SQLite databases.
Implements the same interface (get_tables, get_table_schema, get_foreign_keys,
execute_query, etc.) using SQLite PRAGMA commands instead of MySQL syntax.

Designed for Spider 2.0-Lite benchmark databases.
"""

import os
import re
import sqlite3
import time
import threading
from typing import Optional
from contextlib import contextmanager
import structlog

logger = structlog.get_logger()

# Only allow alphanumeric + underscore in identifiers
_SAFE_IDENTIFIER = re.compile(r'^[a-zA-Z0-9_]+$')


def _validate_identifier(name: str, label: str = "identifier"):
    """Validate that a SQL identifier contains only safe characters."""
    if not _SAFE_IDENTIFIER.match(name):
        raise ValueError(f"Invalid {label}: {name!r}")


class SQLitePool:
    """
    SQLite connection handler implementing the same interface as DatabasePool.

    SQLite is file-based and doesn't need connection pooling, but this class
    mirrors the DatabasePool API so SchemaEnricher, HybridRetriever, and the
    agent pipeline can use it interchangeably.

    Thread safety: sqlite3 connections with check_same_thread=False + a
    threading lock for write-like operations. Read queries are safe for
    concurrent access in WAL mode (which is the SQLite default for most
    Spider databases).
    """

    def __init__(
        self,
        db_path: str,
        query_timeout: int = 30,
    ):
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"SQLite database not found: {db_path}")

        self.db_path = os.path.abspath(db_path)
        self.db_name = os.path.splitext(os.path.basename(db_path))[0]
        self.query_timeout = query_timeout
        self.dialect = "sqlite"

        # In-memory schema cache (same pattern as DatabasePool)
        self._tables: Optional[list[str]] = None
        self._table_schemas: dict[str, list[dict]] = {}
        self._foreign_keys: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

        self._validate_connection()

    def _get_connection(self) -> sqlite3.Connection:
        """Create a new SQLite connection with standard settings."""
        conn = sqlite3.connect(
            self.db_path,
            timeout=self.query_timeout,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        # Enable foreign key enforcement
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _validate_connection(self):
        """Validate database connectivity on startup."""
        try:
            conn = self._get_connection()
            conn.execute("SELECT 1")
            conn.close()
            logger.info(
                "sqlite_database_connected",
                database=self.db_name,
                path=self.db_path,
            )
        except Exception as e:
            logger.error("sqlite_connection_failed", error=str(e), path=self.db_path)
            raise

    @contextmanager
    def get_connection(self):
        """Context manager for database connections (mirrors DatabasePool)."""
        conn = self._get_connection()
        try:
            yield conn
        finally:
            conn.close()

    def execute_query(self, query: str, params: Optional[dict] = None) -> list[dict]:
        """
        Execute a read-only query and return results as list of dicts.

        Mirrors DatabasePool.execute_query() — returns the same
        [{column_name: value, ...}, ...] format.
        """
        start_time = time.perf_counter()
        try:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                if params:
                    # Convert :name style params to sqlite3 style
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)

                # Check if this is a SELECT/PRAGMA (returns rows)
                if cursor.description is None:
                    conn.close()
                    return []

                columns = [desc[0] for desc in cursor.description]
                rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
                elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
                logger.info(
                    "sqlite_query_executed",
                    query=query[:200],
                    elapsed_ms=elapsed_ms,
                    row_count=len(rows),
                )
                return rows
            finally:
                conn.close()
        except Exception as e:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.error(
                "sqlite_query_failed",
                query=query[:200],
                elapsed_ms=elapsed_ms,
                error=str(e),
            )
            raise

    def get_tables(self) -> list[str]:
        """Returns all table names in the database."""
        if self._tables is None:
            rows = self.execute_query(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            self._tables = [row["name"] for row in rows]
        return self._tables

    def get_table_schema(self, table_name: str) -> list[dict]:
        """
        Returns column details for a specific table.

        Output format matches DatabasePool exactly:
            [{"name": ..., "type": ..., "null": ..., "key": ..., "default": ...}, ...]

        Maps SQLite PRAGMA table_info fields:
            (cid, name, type, notnull, dflt_value, pk)
        to the MySQL-compatible format used by SchemaEnricher.
        """
        if table_name not in self._table_schemas:
            _validate_identifier(table_name, "table_name")
            rows = self.execute_query(f'PRAGMA table_info("{table_name}")')

            # Pre-fetch foreign keys to identify FK columns
            fk_columns = set()
            fks = self.get_foreign_keys(table_name)
            for fk in fks:
                fk_columns.add(fk["COLUMN_NAME"])

            self._table_schemas[table_name] = [
                {
                    "name": row["name"],
                    "type": row["type"] or "TEXT",  # SQLite can have empty type
                    "null": "NO" if row["notnull"] else "YES",
                    "key": (
                        "PRI" if row["pk"]
                        else "MUL" if row["name"] in fk_columns
                        else ""
                    ),
                    "default": row["dflt_value"],
                }
                for row in rows
            ]
        return self._table_schemas[table_name]

    def get_foreign_keys(self, table_name: str) -> list[dict]:
        """
        Returns foreign key relationships for a table.

        Output format matches DatabasePool exactly:
            [{"COLUMN_NAME": ..., "REFERENCED_TABLE_NAME": ..., "REFERENCED_COLUMN_NAME": ...}, ...]

        Uses SQLite PRAGMA foreign_key_list which returns:
            (id, seq, table, from, to, on_update, on_delete, match)
        """
        if table_name not in self._foreign_keys:
            _validate_identifier(table_name, "table_name")
            rows = self.execute_query(f'PRAGMA foreign_key_list("{table_name}")')
            self._foreign_keys[table_name] = [
                {
                    "COLUMN_NAME": row["from"],
                    "REFERENCED_TABLE_NAME": row["table"],
                    "REFERENCED_COLUMN_NAME": row["to"],
                }
                for row in rows
            ]
        return self._foreign_keys[table_name]

    def clear_schema_cache(self):
        """Clears all schema caching to allow schema refreshes."""
        self._tables = None
        self._table_schemas.clear()
        self._foreign_keys.clear()
        logger.info("sqlite_schema_cache_cleared", database=self.db_name)

    def get_sample_values(self, table_name: str, column_name: str, limit: int = 5) -> list:
        """Returns sample distinct values for a column."""
        _validate_identifier(table_name, "table_name")
        _validate_identifier(column_name, "column_name")
        try:
            query = f'SELECT DISTINCT "{column_name}" FROM "{table_name}" LIMIT :lim'
            rows = self.execute_query(query, {"lim": limit})
            return [list(r.values())[0] for r in rows]
        except Exception:
            return []

    def get_row_count(self, table_name: str) -> int:
        """Returns row count for a table."""
        _validate_identifier(table_name, "table_name")
        try:
            rows = self.execute_query(f'SELECT COUNT(*) as cnt FROM "{table_name}"')
            return rows[0]["cnt"] if rows else 0
        except Exception:
            return 0

    def get_full_schema(self) -> str:
        """
        Generates a complete text representation of the database schema.
        Mirrors DatabasePool.get_full_schema() exactly.
        """
        tables = self.get_tables()
        schema_text = ""

        for table in tables:
            columns = self.get_table_schema(table)
            schema_text += f"Table: {table}\nColumns:\n"
            for col in columns:
                schema_text += f"  - {col['name']} ({col['type']})"
                if col['key'] == 'PRI':
                    schema_text += " [PRIMARY KEY]"
                if col['key'] == 'MUL':
                    schema_text += " [FOREIGN KEY]"
                schema_text += "\n"

            # Add foreign key relationships
            fks = self.get_foreign_keys(table)
            if fks:
                schema_text += "Relationships:\n"
                for fk in fks:
                    schema_text += f"  - {fk['COLUMN_NAME']} → {fk['REFERENCED_TABLE_NAME']}.{fk['REFERENCED_COLUMN_NAME']}\n"
            schema_text += "\n"

        return schema_text

    def get_pool_status(self) -> dict:
        """
        Returns connection status (no pool for SQLite).
        Mirrors DatabasePool.get_pool_status() shape.
        """
        return {
            "pool_size": 1,
            "checked_out": 0,
            "overflow": 0,
            "checked_in": 1,
            "dialect": "sqlite",
            "database": self.db_name,
        }

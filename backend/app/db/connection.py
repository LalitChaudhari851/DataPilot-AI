"""
Database Connection Pool — Production-grade pooled connections via SQLAlchemy engine.
Supports multi-tenant databases via tenant_id-based connection routing.
"""

import time
import re
from sqlalchemy import create_engine, text, event
from sqlalchemy.pool import QueuePool
from urllib.parse import urlparse, unquote
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


class DatabasePool:
    """
    Production database connection pool.
    Uses SQLAlchemy QueuePool for connection reuse, health checks, and overflow management.
    Falls back to raw pymysql for schema introspection when needed.
    """

    def __init__(
        self,
        db_uri: str,
        query_timeout: int = 30,
        pool_size: int = 10,
        max_overflow: int = 20,
        pool_timeout: int = 30,
    ):
        parsed = urlparse(db_uri)
        self.host = parsed.hostname
        self.port = parsed.port or 3306
        self.user = parsed.username
        self.password = unquote(parsed.password) if parsed.password else ""
        self.db_name = parsed.path[1:] if parsed.path else "chatbot"
        self.query_timeout = query_timeout

        # ── Build SQLAlchemy engine with connection pool ──
        # Construct a clean pymysql URI (no SQLAlchemy dialect prefix issues)
        safe_password = self.password.replace("@", "%40")
        engine_uri = f"mysql+pymysql://{self.user}:{safe_password}@{self.host}:{self.port}/{self.db_name}"

        connect_kwargs = {
            "connect_timeout": 10,
            "read_timeout": query_timeout,
            "write_timeout": query_timeout,
        }
        # TiDB Cloud requires SSL
        if self.host and "tidbcloud.com" in self.host:
            import ssl
            ssl_ctx = ssl.create_default_context()
            connect_kwargs["ssl"] = ssl_ctx

        self._engine = create_engine(
            engine_uri,
            poolclass=QueuePool,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_pre_ping=True,  # Verify connections before checkout (stale conn defense)
            pool_recycle=1800,   # Recycle connections every 30 min (MySQL wait_timeout defense)
            connect_args=connect_kwargs,
        )

        # ── Set session to READ ONLY for query connections ──
        @event.listens_for(self._engine, "checkout")
        def _set_read_only(dbapi_conn, connection_record, connection_proxy):
            """Set session to read-only on checkout for defense-in-depth."""
            pass  # MySQL read-only requires SUPER privilege; rely on SQL validation instead

        # ── In-Memory Schema Cache ──
        self._tables = None
        self._table_schemas = {}
        self._foreign_keys = {}

        self._prepopulate_cache_from_file()

        self._validate_connection()

    def _prepopulate_cache_from_file(self):
        """Pre-populate the schema cache from enriched_schema.json if it exists."""
        import os
        import json
        schema_file = "./chroma_db/enriched_schema.json"
        if self.db_name in ("chatbot", "default") and os.path.exists(schema_file):
            try:
                with open(schema_file, "r", encoding="utf-8") as f:
                    enriched_tables = json.load(f)
                tables = []
                for item in enriched_tables:
                    table_name = item.get("table_name")
                    if not table_name:
                        continue
                    tables.append(table_name)
                    if "raw_columns" in item:
                        self._table_schemas[table_name] = item["raw_columns"]
                    if "raw_fks" in item:
                        self._foreign_keys[table_name] = item["raw_fks"]
                if tables:
                    self._tables = tables
                    logger.info("prepopulated_database_schema_cache_from_file", tables=len(tables))
            except Exception as e:
                logger.warning("failed_to_prepopulate_schema_cache", error=str(e))

    def _validate_connection(self):
        """Validate database connectivity on startup."""
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("database_connected", host=self.host, database=self.db_name,
                        pool_size=self._engine.pool.size())
        except Exception as e:
            logger.error("database_connection_failed", error=str(e))
            raise

    @contextmanager
    def get_connection(self):
        """Context manager for pooled database connections."""
        conn = self._engine.connect()
        try:
            yield conn
        finally:
            conn.close()

    def execute_query(self, query: str, params: Optional[dict] = None) -> list[dict]:
        """Execute a read-only query and return results as list of dicts."""
        start_time = time.perf_counter()
        try:
            with self._engine.connect() as conn:
                # Enforce statement-level timeout (milliseconds) to prevent
                # runaway queries from blocking the thread pool forever.
                # The socket read_timeout only handles network stalls, not slow SQL.
                timeout_ms = self.query_timeout * 1000
                try:
                    conn.execute(text(f"SET SESSION max_execution_time = {timeout_ms}"))
                except Exception:
                    pass  # Non-critical — older MySQL versions may not support this

                if params:
                    result = conn.execute(text(query), params)
                else:
                    result = conn.execute(text(query))
                columns = result.keys()
                rows = [dict(zip(columns, row)) for row in result.fetchall()]
                elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
                logger.info("db_query_executed", query=query[:200], elapsed_ms=elapsed_ms, row_count=len(rows))
                return rows
        except Exception as e:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.error("db_query_failed", query=query[:200], elapsed_ms=elapsed_ms, error=str(e))
            raise

    def _execute_write_internal(self, query: str, params: Optional[tuple] = None):
        """
        Execute a write query. INTERNAL USE ONLY — for schema setup and migrations.
        User-facing queries MUST go through execute_query after SQL validation.
        """
        start_time = time.perf_counter()
        try:
            with self._engine.begin() as conn:
                if params:
                    # Build named params dict matching :p0, :p1, :p2... placeholders
                    param_dict = {f"p{i}": v for i, v in enumerate(params)}
                    conn.execute(text(query), param_dict)
                else:
                    conn.execute(text(query))
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.info("db_write_executed", query=query[:200], elapsed_ms=elapsed_ms)
        except Exception as e:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.error("db_write_failed", query=query[:200], elapsed_ms=elapsed_ms, error=str(e))
            raise

    def get_tables(self) -> list[str]:
        """Returns all table names in the current database."""
        if self._tables is None:
            rows = self.execute_query("SHOW TABLES")
            self._tables = [list(row.values())[0] for row in rows]
        return self._tables

    def get_table_schema(self, table_name: str) -> list[dict]:
        """Returns column details for a specific table."""
        if table_name not in self._table_schemas:
            _validate_identifier(table_name, "table_name")
            rows = self.execute_query(f"DESCRIBE `{table_name}`")
            self._table_schemas[table_name] = [
                {
                    "name": row["Field"],
                    "type": row["Type"],
                    "null": row["Null"],
                    "key": row["Key"],
                    "default": row["Default"],
                }
                for row in rows
            ]
        return self._table_schemas[table_name]

    def get_foreign_keys(self, table_name: str) -> list[dict]:
        """Returns foreign key relationships for a table."""
        if table_name not in self._foreign_keys:
            query = """
            SELECT 
                COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = :schema_name
                AND TABLE_NAME = :table_name
                AND REFERENCED_TABLE_NAME IS NOT NULL
            """
            self._foreign_keys[table_name] = self.execute_query(query, {"schema_name": self.db_name, "table_name": table_name})
        return self._foreign_keys[table_name]

    def clear_schema_cache(self):
        """Clears all schema caching to allow schema refreshes."""
        self._tables = None
        self._table_schemas.clear()
        self._foreign_keys.clear()
        logger.info("database_schema_cache_cleared")

    def get_sample_values(self, table_name: str, column_name: str, limit: int = 5) -> list:
        """Returns sample distinct values for a column."""
        _validate_identifier(table_name, "table_name")
        _validate_identifier(column_name, "column_name")
        try:
            query = f"SELECT DISTINCT `{column_name}` FROM `{table_name}` LIMIT :lim"
            rows = self.execute_query(query, {"lim": limit})
            return [list(r.values())[0] for r in rows]
        except Exception:
            return []

    def get_row_count(self, table_name: str) -> int:
        """Returns approximate row count for a table."""
        try:
            rows = self.execute_query(f"SELECT COUNT(*) as cnt FROM `{table_name}`")
            return rows[0]["cnt"] if rows else 0
        except Exception:
            return 0
    
    def get_full_schema(self) -> str:
        """Generates a complete text representation of the database schema."""
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
        """Returns current connection pool statistics."""
        pool = self._engine.pool
        return {
            "pool_size": pool.size(),
            "checked_out": pool.checkedout(),
            "overflow": pool.overflow(),
            "checked_in": pool.checkedin(),
        }


class TenantRegistry:
    """
    Multi-tenant database connection registry.
    Maps tenant_id → DatabasePool for isolated data access.

    NOTE: Currently scaffolding — not used in production.
    The system operates as single-tenant with tenant_id used only for
    cache isolation. For true multi-tenancy, instantiate this registry
    in main.py and pass tenant-specific pools to the orchestrator.
    """

    def __init__(self):
        self._pools: dict[str, DatabasePool] = {}

    def register(self, tenant_id: str, db_uri: str, query_timeout: int = 30):
        """Register a new tenant database."""
        self._pools[tenant_id] = DatabasePool(db_uri, query_timeout)
        logger.info("tenant_registered", tenant_id=tenant_id)

    def get_pool(self, tenant_id: str) -> DatabasePool:
        """Get the database pool for a tenant."""
        pool = self._pools.get(tenant_id)
        if not pool:
            raise ValueError(f"No database registered for tenant: {tenant_id}")
        return pool

    def list_tenants(self) -> list[str]:
        """List all registered tenant IDs."""
        return list(self._pools.keys())

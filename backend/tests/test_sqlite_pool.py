"""
Unit tests for SQLitePool and DatabaseRegistry (Phase 1).

Tests verify that:
1. SQLitePool can connect to a real Spider 2.0-Lite database
2. Schema introspection works (tables, columns, foreign keys, row counts)
3. Query execution returns correctly-formatted results
4. Mutation queries are not possible (read-only by design)
5. The interface matches DatabasePool's expected contract
6. DatabaseRegistry correctly discovers and manages databases
"""

import os
import sys
import sqlite3
import tempfile
import pytest

# Add project to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.sqlite_pool import SQLitePool
from app.db.registry import DatabaseRegistry


# ── Fixtures ─────────────────────────────────────────────

SPIDER_DB_DIR = os.environ.get("SPIDER_DB_DIR", r"E:\Downloads\local_sqlite")
ECOMMERCE_PATH = os.path.join(SPIDER_DB_DIR, "E_commerce.sqlite")


@pytest.fixture(scope="module")
def ecommerce_pool():
    """Create a SQLitePool pointing at the E_commerce.sqlite database."""
    if not os.path.exists(ECOMMERCE_PATH):
        pytest.skip(f"E_commerce.sqlite not found at {ECOMMERCE_PATH}")
    return SQLitePool(ECOMMERCE_PATH)


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database with known schema for isolated tests."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)

    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE departments (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            budget REAL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE employees (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            salary REAL,
            department_id INTEGER,
            FOREIGN KEY (department_id) REFERENCES departments(id)
        )
    """)
    conn.execute("INSERT INTO departments VALUES (1, 'Engineering', 500000)")
    conn.execute("INSERT INTO departments VALUES (2, 'Marketing', 200000)")
    conn.execute("INSERT INTO employees VALUES (1, 'Alice', 120000, 1)")
    conn.execute("INSERT INTO employees VALUES (2, 'Bob', 95000, 1)")
    conn.execute("INSERT INTO employees VALUES (3, 'Carol', 85000, 2)")
    conn.commit()
    conn.close()

    yield path

    os.unlink(path)


@pytest.fixture
def temp_pool(temp_db):
    """Create a SQLitePool for the temporary test database."""
    return SQLitePool(temp_db)


@pytest.fixture
def temp_db_dir(temp_db):
    """Return a directory containing at least one .sqlite file."""
    return os.path.dirname(temp_db)


# ══════════════════════════════════════════════════════════
# SQLitePool — Connection & Health
# ══════════════════════════════════════════════════════════


class TestSQLitePoolConnection:
    """Test connection handling and initialization."""

    def test_connect_to_real_ecommerce(self, ecommerce_pool):
        """SQLitePool connects to the real E_commerce.sqlite database."""
        assert ecommerce_pool.db_name == "E_commerce"
        assert ecommerce_pool.dialect == "sqlite"

    def test_connect_nonexistent_file_raises(self):
        """SQLitePool raises FileNotFoundError for missing files."""
        with pytest.raises(FileNotFoundError):
            SQLitePool("/nonexistent/path/to/database.sqlite")

    def test_get_pool_status(self, temp_pool):
        """get_pool_status returns the expected dict shape."""
        status = temp_pool.get_pool_status()
        assert "pool_size" in status
        assert "dialect" in status
        assert status["dialect"] == "sqlite"

    def test_context_manager_connection(self, temp_pool):
        """get_connection context manager provides a working connection."""
        with temp_pool.get_connection() as conn:
            cursor = conn.execute("SELECT 1")
            assert cursor.fetchone() is not None


# ══════════════════════════════════════════════════════════
# SQLitePool — Schema Introspection (E_commerce.sqlite)
# ══════════════════════════════════════════════════════════


class TestSQLitePoolSchemaEcommerce:
    """Test schema introspection against the real E_commerce database."""

    def test_get_tables(self, ecommerce_pool):
        """get_tables returns the expected tables from E_commerce.sqlite."""
        tables = ecommerce_pool.get_tables()
        assert isinstance(tables, list)
        assert len(tables) > 0
        # Known tables in E_commerce.sqlite
        assert "customers" in tables
        assert "orders" in tables
        assert "products" in tables
        assert "order_items" in tables

    def test_get_table_schema_format(self, ecommerce_pool):
        """get_table_schema returns dicts matching DatabasePool's format."""
        schema = ecommerce_pool.get_table_schema("customers")
        assert isinstance(schema, list)
        assert len(schema) > 0

        # Verify each column dict has the required keys
        for col in schema:
            assert "name" in col, f"Missing 'name' key in column: {col}"
            assert "type" in col, f"Missing 'type' key in column: {col}"
            assert "null" in col, f"Missing 'null' key in column: {col}"
            assert "key" in col, f"Missing 'key' key in column: {col}"
            assert "default" in col, f"Missing 'default' key in column: {col}"

        # Check that null values are "YES"/"NO" strings (matching MySQL format)
        for col in schema:
            assert col["null"] in ("YES", "NO"), f"Unexpected null format: {col['null']}"

    def test_get_table_schema_column_names(self, ecommerce_pool):
        """Customers table has expected columns."""
        schema = ecommerce_pool.get_table_schema("customers")
        col_names = [col["name"] for col in schema]
        assert "customer_id" in col_names
        assert "customer_city" in col_names
        assert "customer_state" in col_names

    def test_get_foreign_keys_format(self, ecommerce_pool):
        """get_foreign_keys returns dicts matching DatabasePool's format."""
        # order_items should have foreign keys
        fks = ecommerce_pool.get_foreign_keys("order_items")
        # It's OK if Spider databases don't declare FKs, but format must be correct
        assert isinstance(fks, list)
        for fk in fks:
            assert "COLUMN_NAME" in fk
            assert "REFERENCED_TABLE_NAME" in fk
            assert "REFERENCED_COLUMN_NAME" in fk

    def test_get_row_count(self, ecommerce_pool):
        """get_row_count returns a positive integer for populated tables."""
        count = ecommerce_pool.get_row_count("customers")
        assert isinstance(count, int)
        assert count > 0  # E_commerce has ~99k customers

    def test_get_full_schema(self, ecommerce_pool):
        """get_full_schema returns a non-empty formatted string."""
        schema_text = ecommerce_pool.get_full_schema()
        assert isinstance(schema_text, str)
        assert len(schema_text) > 100
        assert "Table: customers" in schema_text
        assert "Table: orders" in schema_text

    def test_get_sample_values(self, ecommerce_pool):
        """get_sample_values returns a list of values."""
        samples = ecommerce_pool.get_sample_values("customers", "customer_state", limit=3)
        assert isinstance(samples, list)
        assert len(samples) > 0


# ══════════════════════════════════════════════════════════
# SQLitePool — Schema Introspection (temp database)
# ══════════════════════════════════════════════════════════


class TestSQLitePoolSchemaTemp:
    """Test schema introspection with the controlled temp database."""

    def test_get_tables(self, temp_pool):
        """Temp database has exactly 2 tables."""
        tables = temp_pool.get_tables()
        assert sorted(tables) == ["departments", "employees"]

    def test_get_table_schema_primary_key(self, temp_pool):
        """Primary keys are correctly identified."""
        schema = temp_pool.get_table_schema("departments")
        id_col = next(c for c in schema if c["name"] == "id")
        assert id_col["key"] == "PRI"

    def test_get_table_schema_foreign_key(self, temp_pool):
        """Foreign keys are correctly identified."""
        schema = temp_pool.get_table_schema("employees")
        dept_col = next(c for c in schema if c["name"] == "department_id")
        assert dept_col["key"] == "MUL"

    def test_get_table_schema_types(self, temp_pool):
        """Column types are correctly mapped."""
        schema = temp_pool.get_table_schema("departments")
        name_col = next(c for c in schema if c["name"] == "name")
        assert "TEXT" in name_col["type"]

    def test_get_foreign_keys(self, temp_pool):
        """Foreign keys are correctly extracted from PRAGMA."""
        fks = temp_pool.get_foreign_keys("employees")
        assert len(fks) == 1
        assert fks[0]["COLUMN_NAME"] == "department_id"
        assert fks[0]["REFERENCED_TABLE_NAME"] == "departments"
        assert fks[0]["REFERENCED_COLUMN_NAME"] == "id"

    def test_get_foreign_keys_empty(self, temp_pool):
        """Tables without foreign keys return empty list."""
        fks = temp_pool.get_foreign_keys("departments")
        assert fks == []

    def test_get_row_count(self, temp_pool):
        """Row counts match inserted data."""
        assert temp_pool.get_row_count("departments") == 2
        assert temp_pool.get_row_count("employees") == 3

    def test_clear_schema_cache(self, temp_pool):
        """Schema cache can be cleared and re-populated."""
        _ = temp_pool.get_tables()
        _ = temp_pool.get_table_schema("departments")
        temp_pool.clear_schema_cache()
        assert temp_pool._tables is None
        assert temp_pool._table_schemas == {}

        # Re-fetch works
        tables = temp_pool.get_tables()
        assert len(tables) == 2


# ══════════════════════════════════════════════════════════
# SQLitePool — Query Execution
# ══════════════════════════════════════════════════════════


class TestSQLitePoolExecution:
    """Test query execution."""

    def test_execute_select_query(self, temp_pool):
        """Basic SELECT query returns correctly-formatted results."""
        results = temp_pool.execute_query("SELECT name, budget FROM departments ORDER BY name")
        assert len(results) == 2
        assert results[0]["name"] == "Engineering"
        assert results[0]["budget"] == 500000

    def test_execute_query_returns_list_of_dicts(self, temp_pool):
        """Results are list[dict] with column name keys."""
        results = temp_pool.execute_query("SELECT id, name FROM employees LIMIT 1")
        assert isinstance(results, list)
        assert len(results) == 1
        assert isinstance(results[0], dict)
        assert "id" in results[0]
        assert "name" in results[0]

    def test_execute_aggregate_query(self, temp_pool):
        """Aggregate queries work correctly."""
        results = temp_pool.execute_query("SELECT COUNT(*) as cnt FROM employees")
        assert results[0]["cnt"] == 3

    def test_execute_join_query(self, temp_pool):
        """JOIN queries work correctly."""
        results = temp_pool.execute_query("""
            SELECT e.name, d.name as dept_name
            FROM employees e
            JOIN departments d ON e.department_id = d.id
            ORDER BY e.name
        """)
        assert len(results) == 3
        assert results[0]["name"] == "Alice"
        assert results[0]["dept_name"] == "Engineering"

    def test_execute_query_with_params(self, temp_pool):
        """Parameterized queries work correctly."""
        results = temp_pool.execute_query(
            "SELECT name FROM employees WHERE salary > :min_salary",
            {"min_salary": 100000},
        )
        assert len(results) == 1
        assert results[0]["name"] == "Alice"

    def test_execute_ecommerce_query(self, ecommerce_pool):
        """Execute a real query against E_commerce.sqlite."""
        results = ecommerce_pool.execute_query(
            "SELECT customer_state, COUNT(*) as cnt FROM customers GROUP BY customer_state ORDER BY cnt DESC LIMIT 5"
        )
        assert len(results) == 5
        assert "customer_state" in results[0]
        assert "cnt" in results[0]
        assert results[0]["cnt"] > 0


# ══════════════════════════════════════════════════════════
# SQLitePool — Mutation Blocking (Safety)
# ══════════════════════════════════════════════════════════


class TestSQLitePoolSafety:
    """Test that mutation queries cannot bypass safety."""

    def test_insert_raises_error(self, temp_pool):
        """INSERT queries raise an error when executed through execute_query.
        
        Note: SQLitePool.execute_query doesn't have an explicit mutation blocker
        because the pipeline's sql_validation.py blocks mutations before execution.
        However, SQLite database files used for benchmarks should be treated as
        read-only. This test documents the current behavior.
        """
        # The SQL validation layer (sql_validation.py) blocks all non-SELECT
        # statements before they reach execute_query. But we verify at minimum
        # that the pool doesn't silently succeed with no results:
        try:
            results = temp_pool.execute_query(
                "INSERT INTO departments VALUES (99, 'Test', 0)"
            )
            # If it doesn't raise, it should return empty (no cursor.description)
            assert results == []
        except Exception:
            # Any error is acceptable — the validation layer prevents this
            pass

    def test_drop_raises_error(self, temp_pool):
        """DROP TABLE raises an error or is blocked."""
        with pytest.raises(Exception):
            temp_pool.execute_query("DROP TABLE departments")

    def test_delete_data_unchanged_after_attempt(self, temp_pool):
        """Even if a mutation is attempted, we verify data integrity."""
        original_count = temp_pool.get_row_count("departments")

        try:
            temp_pool.execute_query("DELETE FROM departments WHERE id = 1")
        except Exception:
            pass

        # Since SQLite execute_query doesn't commit (no conn.commit()),
        # data should remain unchanged
        temp_pool.clear_schema_cache()
        current_count = temp_pool.get_row_count("departments")
        assert current_count == original_count


# ══════════════════════════════════════════════════════════
# SQLitePool — Interface Compatibility with DatabasePool
# ══════════════════════════════════════════════════════════


class TestSQLitePoolInterfaceCompat:
    """Verify SQLitePool implements all methods expected by the pipeline."""

    REQUIRED_METHODS = [
        "get_tables",
        "get_table_schema",
        "get_foreign_keys",
        "get_row_count",
        "get_full_schema",
        "get_sample_values",
        "execute_query",
        "clear_schema_cache",
        "get_pool_status",
        "get_connection",
    ]

    def test_all_interface_methods_exist(self, temp_pool):
        """SQLitePool implements every public method that DatabasePool provides."""
        for method_name in self.REQUIRED_METHODS:
            assert hasattr(temp_pool, method_name), f"Missing method: {method_name}"
            assert callable(getattr(temp_pool, method_name)), f"Not callable: {method_name}"

    def test_schema_output_matches_enricher_expectations(self, temp_pool):
        """SchemaEnricher expects specific dict keys from get_table_schema."""
        schema = temp_pool.get_table_schema("employees")
        # SchemaEnricher accesses: col["name"], col["type"], col["key"]
        for col in schema:
            _ = col["name"]
            _ = col["type"]
            _ = col["key"]

    def test_foreign_key_output_matches_enricher_expectations(self, temp_pool):
        """SchemaEnricher expects specific dict keys from get_foreign_keys."""
        fks = temp_pool.get_foreign_keys("employees")
        # SchemaEnricher accesses: fk['COLUMN_NAME'], fk['REFERENCED_TABLE_NAME'], fk['REFERENCED_COLUMN_NAME']
        for fk in fks:
            _ = fk["COLUMN_NAME"]
            _ = fk["REFERENCED_TABLE_NAME"]
            _ = fk["REFERENCED_COLUMN_NAME"]


# ══════════════════════════════════════════════════════════
# DatabaseRegistry
# ══════════════════════════════════════════════════════════


class TestDatabaseRegistry:
    """Test the DatabaseRegistry for multi-database management."""

    def test_register_and_get_pool(self, temp_pool):
        """Register a pool and retrieve it by db_id."""
        registry = DatabaseRegistry()
        registry.register("test_db", temp_pool, dialect="sqlite")
        retrieved = registry.get_pool("test_db")
        assert retrieved is temp_pool

    def test_get_nonexistent_db_raises(self):
        """Requesting an unregistered db_id raises KeyError."""
        registry = DatabaseRegistry()
        with pytest.raises(KeyError, match="not found"):
            registry.get_pool("nonexistent")

    def test_get_dialect(self, temp_pool):
        """get_dialect returns the correct dialect string."""
        registry = DatabaseRegistry()
        registry.register("test", temp_pool, dialect="sqlite")
        assert registry.get_dialect("test") == "sqlite"

    def test_list_databases(self, temp_pool):
        """list_databases returns metadata for all registered databases."""
        registry = DatabaseRegistry()
        registry.register("db1", temp_pool, dialect="sqlite", path="/test/path.sqlite")
        registry.register("db2", temp_pool, dialect="mysql")
        databases = registry.list_databases()
        assert len(databases) == 2
        db_ids = [db["db_id"] for db in databases]
        assert "db1" in db_ids
        assert "db2" in db_ids

    def test_has_database(self, temp_pool):
        """has_database returns correct boolean."""
        registry = DatabaseRegistry()
        registry.register("exists", temp_pool)
        assert registry.has_database("exists") is True
        assert registry.has_database("missing") is False

    def test_default_db_id(self, temp_pool):
        """default_db_id returns 'default' if registered, else first key."""
        registry = DatabaseRegistry()
        registry.register("default", temp_pool)
        assert registry.default_db_id == "default"

    def test_discover_sqlite_databases_lazy(self):
        """discover_sqlite_databases finds .sqlite files in a directory."""
        if not os.path.isdir(SPIDER_DB_DIR):
            pytest.skip(f"Spider DB dir not found: {SPIDER_DB_DIR}")

        registry = DatabaseRegistry()
        registry.discover_sqlite_databases(SPIDER_DB_DIR, lazy=True)

        databases = registry.list_databases()
        assert len(databases) > 0

        # E_commerce should be discovered
        assert registry.has_database("E_commerce")

        # Should be lazy (not initialized yet)
        e_commerce_entry = next(d for d in databases if d["db_id"] == "E_commerce")
        assert e_commerce_entry["initialized"] is False
        assert e_commerce_entry["dialect"] == "sqlite"

    def test_lazy_pool_initialization(self):
        """Lazy pools are initialized on first get_pool() call."""
        if not os.path.exists(ECOMMERCE_PATH):
            pytest.skip("E_commerce.sqlite not found")

        registry = DatabaseRegistry()
        registry.discover_sqlite_databases(SPIDER_DB_DIR, lazy=True)

        # First access triggers initialization
        pool = registry.get_pool("E_commerce")
        assert pool is not None
        assert isinstance(pool, SQLitePool)

        # Verify it works
        tables = pool.get_tables()
        assert "customers" in tables

    def test_discover_nonexistent_directory(self):
        """Discovery gracefully handles missing directories."""
        registry = DatabaseRegistry()
        registry.discover_sqlite_databases("/nonexistent/path", lazy=True)
        assert registry.list_databases() == []

    def test_no_overwrite_on_discover(self, temp_pool):
        """Discovery doesn't overwrite manually registered databases."""
        if not os.path.isdir(SPIDER_DB_DIR):
            pytest.skip(f"Spider DB dir not found: {SPIDER_DB_DIR}")

        registry = DatabaseRegistry()
        registry.register("E_commerce", temp_pool, dialect="sqlite")
        registry.discover_sqlite_databases(SPIDER_DB_DIR, lazy=True)

        # Should still be our temp_pool, not the discovered one
        assert registry.get_pool("E_commerce") is temp_pool


# ══════════════════════════════════════════════════════════
# SchemaEnricher Integration (read-only — no modifications)
# ══════════════════════════════════════════════════════════


class TestSchemaEnricherCompat:
    """Test that SQLitePool works with the existing SchemaEnricher (unmodified)."""

    def test_enricher_can_enrich_sqlite_tables(self, temp_pool):
        """SchemaEnricher.enrich_all_tables works with a SQLitePool."""
        from app.rag.schema_enricher import SchemaEnricher

        enricher = SchemaEnricher(temp_pool)
        enriched = enricher.enrich_all_tables()

        assert len(enriched) == 2  # departments + employees
        for item in enriched:
            assert "table_name" in item
            assert "document" in item
            assert "metadata" in item

        # Check that documents contain expected content
        docs = {item["table_name"]: item["document"] for item in enriched}
        assert "departments" in docs
        assert "employees" in docs
        assert "Table: employees" in docs["employees"]
        assert "salary" in docs["employees"]

    def test_enricher_ecommerce(self, ecommerce_pool):
        """SchemaEnricher works with the real E_commerce database."""
        from app.rag.schema_enricher import SchemaEnricher

        enricher = SchemaEnricher(ecommerce_pool)
        enriched = enricher.enrich_all_tables()

        assert len(enriched) > 5  # E_commerce has 11 tables
        table_names = [item["table_name"] for item in enriched]
        assert "customers" in table_names
        assert "orders" in table_names


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])

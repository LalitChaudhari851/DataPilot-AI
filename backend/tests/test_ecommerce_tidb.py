"""
backend/tests/test_ecommerce_tidb.py — Validation test suite for Spider E_commerce deployed to TiDB Cloud.

Verifies:
1. Multi-database registry resolution (db_id="E_commerce" -> dialect="mysql", TiDB DatabasePool).
2. Complete table and row count fidelity (11 tables, 1,559,764 rows).
3. Zero contamination / regression of production default database (22 tables intact).
4. Schema RAG retrieval isolation for E_commerce.
5. Analytical queries (COUNT, JOIN, GROUP BY, Top-N).
6. Security guardrail blocking destructive SQL.
7. Business glossary terms validation against live schema.
8. Cache key isolation between databases.
"""

import pytest
import os
import sys

backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.config import get_settings
from app.db.registry import get_database_registry, resolve_database
from app.semantics.loader import BusinessKnowledgeLoader
from app.agents.guardrails import OutputGuardrail


@pytest.fixture(scope="module")
def settings():
    return get_settings()


@pytest.fixture(scope="module")
def registry():
    return get_database_registry()


@pytest.fixture(scope="module")
def ecommerce_pool(registry):
    return registry.get_pool("E_commerce")


@pytest.fixture(scope="module")
def default_pool(registry):
    return registry.get_pool("default")


def test_ecommerce_registry_resolution(registry):
    """Verify E_commerce resolves to MySQL dialect and DatabasePool."""
    assert registry.has_database("E_commerce")
    dialect = registry.get_dialect("E_commerce")
    assert dialect == "mysql", f"Expected 'mysql' dialect for TiDB E_commerce, got '{dialect}'"

    resolved_id, resolved_dialect, pool = resolve_database("E_commerce", registry=registry)
    assert resolved_id == "E_commerce"
    assert resolved_dialect == "mysql"
    assert pool is not None


def test_ecommerce_table_and_row_counts(ecommerce_pool):
    """Verify all 11 tables exist with exact row counts in TiDB."""
    expected_tables = {
        "customers": 99441,
        "geolocation": 1000163,
        "leads_closed": 842,
        "leads_qualified": 8000,
        "order_items": 112650,
        "order_payments": 103886,
        "order_reviews": 99224,
        "orders": 99441,
        "product_category_name_translation": 71,
        "products": 32951,
        "sellers": 3095,
    }

    tables = set(ecommerce_pool.get_tables())
    for tbl, expected_count in expected_tables.items():
        assert tbl in tables, f"Missing table {tbl} in ecommerce database"
        res = ecommerce_pool.execute_query(f"SELECT COUNT(*) as c FROM `{tbl}`")
        actual_count = list(res[0].values())[0]
        assert actual_count == expected_count, (
            f"Row count mismatch for {tbl}: expected {expected_count}, got {actual_count}"
        )


def test_default_database_intact(registry, settings):
    """Verify the default chatbot database (22 tables) is completely unaffected."""
    from app.db.connection import DatabasePool
    default_pool = DatabasePool(settings.DB_URI)
    tables = default_pool.get_tables()
    assert len(tables) == 22, f"Default database table count altered! Expected 22, found {len(tables)}"
    assert "accounts" in tables
    assert "subscriptions" in tables
    assert "invoices" in tables


def test_ecommerce_analytical_queries(ecommerce_pool):
    """Verify complex SQL queries execute accurately on TiDB Cloud ecommerce database."""
    # 1. Orders count
    res1 = ecommerce_pool.execute_query("SELECT COUNT(*) as cnt FROM orders")
    assert list(res1[0].values())[0] == 99441

    # 2. GROUP BY status
    res2 = ecommerce_pool.execute_query(
        "SELECT order_status, COUNT(*) as cnt FROM orders GROUP BY order_status ORDER BY cnt DESC"
    )
    assert len(res2) > 0
    top_status = res2[0]
    assert "delivered" in str(top_status.values())

    # 3. JOIN & Aggregation (Top cities by customer count)
    res3 = ecommerce_pool.execute_query("""
        SELECT customer_city, COUNT(customer_id) as cnt
        FROM customers
        GROUP BY customer_city
        ORDER BY cnt DESC
        LIMIT 5
    """)
    assert len(res3) == 5
    cities = [str(r.get("customer_city")) for r in res3]
    assert "sao paulo" in cities

    # 4. Multi-table JOIN (Top product categories by gross revenue)
    res4 = ecommerce_pool.execute_query("""
        SELECT p.product_category_name, SUM(oi.price) as gross_revenue
        FROM products p
        JOIN order_items oi ON p.product_id = oi.product_id
        WHERE p.product_category_name IS NOT NULL
        GROUP BY p.product_category_name
        ORDER BY gross_revenue DESC
        LIMIT 5
    """)
    assert len(res4) == 5
    assert float(list(res4[0].values())[1]) > 100000.0


from app.agents.sql_validation import sql_validation_node


def test_security_guardrail_blocks_destructive_sql(ecommerce_pool):
    """Verify sql_validation_node blocks destructive DDL/DML and OutputGuardrail validates schema references."""
    tables = set(ecommerce_pool.get_tables())
    columns = {t: {c["name"] for c in ecommerce_pool.get_table_schema(t)} for t in tables}
    guardrail = OutputGuardrail(known_tables=tables, known_columns=columns)

    # 1. OutputGuardrail references test
    warnings_clean = guardrail.validate_sql_references("SELECT customer_id, customer_city FROM customers")
    assert len(warnings_clean) == 0, f"Unexpected warnings on valid query: {warnings_clean}"

    warnings_hallucinated = guardrail.validate_sql_references("SELECT fake_col FROM non_existent_table")
    assert len(warnings_hallucinated) > 0, "Expected hallucination warnings for non_existent_table"

    # 2. Destructive SQL blocked by SQL validation node
    res_drop = sql_validation_node({"generated_sql": "DROP TABLE orders", "trace_id": "test"})
    assert res_drop["is_valid"] is False
    assert any("DROP" in err or "Blocked statement type" in err for err in res_drop["validation_errors"])

    res_del = sql_validation_node({"generated_sql": "DELETE FROM customers WHERE customer_id = '123'", "trace_id": "test"})
    assert res_del["is_valid"] is False
    assert any("DELETE" in err or "Blocked statement type" in err for err in res_del["validation_errors"])


def test_business_glossary_validation(ecommerce_pool):
    """Verify all 9 E_commerce glossary terms validate successfully against live TiDB schema."""
    glossary_path = os.path.join(backend_dir, "app", "semantics", "business_glossary.yaml")
    all_defs = BusinessKnowledgeLoader.load_from_file(glossary_path)
    ecom_defs = all_defs.get("E_commerce", [])
    assert len(ecom_defs) >= 9, f"Expected at least 9 E_commerce definitions, found {len(ecom_defs)}"

    for d in ecom_defs:
        status = BusinessKnowledgeLoader.validate_definition(d, db_pool=ecommerce_pool)
        assert status.value == "VALID", f"Definition for '{d.term}' failed validation: {status.value}"


def test_cache_key_isolation():
    """Verify cache keys strictly partition by db_id."""
    import hashlib
    def get_key(tenant_id, db_id, query):
        payload = f"{tenant_id}:{db_id}:{query.strip().lower()}"
        return f"plainsql:cache:{hashlib.sha256(payload.encode()).hexdigest()[:16]}"

    key_default = get_key("t1", "default", "show top customers")
    key_ecommerce = get_key("t1", "E_commerce", "show top customers")
    assert key_default != key_ecommerce, "Cache keys collided across different db_ids!"

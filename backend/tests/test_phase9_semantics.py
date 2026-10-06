"""
Phase 9 Tests — Business Semantic Layer & Semantic Column Disambiguation.
"""

import os
import pytest
from app.semantics.models import SemanticRole, ColumnSemanticMetadata, BusinessDefinition, DisambiguationResult
from app.semantics.analyzer import SemanticSchemaAnalyzer
from app.semantics.disambiguator import SemanticDisambiguator
from app.semantics.registry import SemanticRegistry, get_semantic_registry
from app.agents.state import AgentState
from app.agents.schema_retrieval import schema_retrieval_node
from app.agents.sql_generation import sql_generation_node
from app.rag.schema_enricher import SchemaEnricher
from app.db.sqlite_pool import SQLitePool

SPIDER_DIR = os.environ.get("SPIDER_DB_DIR", r"E:\Downloads\local_sqlite")
ECOMMERCE_DB_PATH = os.path.join(SPIDER_DIR, "E_commerce.sqlite")
BASEBALL_DB_PATH = os.path.join(SPIDER_DIR, "Baseball.sqlite")


# ── A & B: Semantic Role Detection & Metadata Generation ──────
def test_semantic_role_detection():
    analyzer = SemanticSchemaAnalyzer()

    # Identifiers & FKs
    assert analyzer.infer_role("orders", "order_id", "INT") == SemanticRole.IDENTIFIER
    assert analyzer.infer_role("orders", "customer_id", "INT") == SemanticRole.FOREIGN_KEY
    assert analyzer.infer_role("customers", "id", "INT") == SemanticRole.IDENTIFIER

    # Status
    assert analyzer.infer_role("orders", "order_status", "VARCHAR") == SemanticRole.STATUS
    assert analyzer.infer_role("salesorderheader", "Status", "INT") == SemanticRole.STATUS

    # Timestamps & Dates
    assert analyzer.infer_role("orders", "order_delivered_customer_date", "TIMESTAMP") == SemanticRole.TIMESTAMP
    assert analyzer.infer_role("orders", "order_purchase_timestamp", "TIMESTAMP") == SemanticRole.TIMESTAMP
    assert analyzer.infer_role("orders", "created_at", "DATETIME") == SemanticRole.TIMESTAMP
    assert analyzer.infer_role("sales", "order_date", "DATE") == SemanticRole.DATE

    # Monetary amounts
    assert analyzer.infer_role("order_items", "price", "FLOAT") == SemanticRole.AMOUNT
    assert analyzer.infer_role("order_items", "freight_value", "FLOAT") == SemanticRole.AMOUNT
    assert analyzer.infer_role("salesperson", "bonus", "DECIMAL") == SemanticRole.AMOUNT

    # Quantities & Counts
    assert analyzer.infer_role("inventory", "quantity", "INT") == SemanticRole.QUANTITY
    assert analyzer.infer_role("orders", "units_sold", "INT") == SemanticRole.QUANTITY
    assert analyzer.infer_role("team", "w", "INT") == SemanticRole.METRIC
    assert analyzer.infer_role("team", "l", "INT") == SemanticRole.METRIC

    # Percentages
    assert analyzer.infer_role("salesperson", "commissionpct", "DECIMAL") == SemanticRole.PERCENTAGE

    # Booleans
    assert analyzer.infer_role("products", "is_active", "BOOLEAN") == SemanticRole.BOOLEAN
    assert analyzer.infer_role("products", "finishedgoodsflag", "TINYINT(1)") == SemanticRole.BOOLEAN

    # Dimensions
    assert analyzer.infer_role("customers", "city", "VARCHAR") == SemanticRole.DIMENSION
    assert analyzer.infer_role("team", "year", "INT") == SemanticRole.DIMENSION


# ── C: Bounded Distinct Value Sampling ────────────────────────
def test_value_sampling():
    if not os.path.exists(ECOMMERCE_DB_PATH):
        pytest.skip("E_commerce.sqlite not found")

    analyzer = SemanticSchemaAnalyzer(db_path=ECOMMERCE_DB_PATH)
    sample_values = analyzer.sample_distinct_values("orders", "order_status", SemanticRole.STATUS)

    assert len(sample_values) > 0
    assert "delivered" in [v.lower() for v in sample_values]
    assert len(sample_values) <= 10

    # Ensure non-status columns return empty to protect performance and privacy
    item_samples = analyzer.sample_distinct_values("order_items", "price", SemanticRole.AMOUNT)
    assert item_samples == []


# ── D: Synonym Handling ──────────────────────────────────────
def test_synonym_handling():
    analyzer = SemanticSchemaAnalyzer()
    synonyms = analyzer.derive_synonyms("order_status", SemanticRole.STATUS)
    assert "state" in synonyms
    assert "status" in synonyms
    assert "order status" in synonyms

    delivery_synonyms = analyzer.derive_synonyms("order_delivered_customer_date", SemanticRole.TIMESTAMP)
    assert any("delivery" in s or "delivered" in s for s in delivery_synonyms)


# ── E & F: Column Disambiguation & Semantic Confidence ────────
def test_column_disambiguation_delivered_orders():
    status_col = ColumnSemanticMetadata(
        database_id="E_commerce",
        table_name="orders",
        column_name="order_status",
        data_type="VARCHAR",
        semantic_role=SemanticRole.STATUS,
        sample_values=["delivered", "shipped", "canceled", "processing"],
        confidence=0.95,
    )
    time_col = ColumnSemanticMetadata(
        database_id="E_commerce",
        table_name="orders",
        column_name="order_delivered_customer_date",
        data_type="TIMESTAMP",
        semantic_role=SemanticRole.TIMESTAMP,
        confidence=0.95,
    )

    cols = [status_col, time_col]

    # Query 1: Filter by condition "delivered orders"
    res1 = SemanticDisambiguator.disambiguate("Find average freight value and average price for delivered orders.", cols)
    assert len(res1) == 1
    assert res1[0].selected_column == "order_status"
    assert res1[0].confidence >= 0.85
    assert "order_delivered_customer_date" in res1[0].alternatives_considered

    # Query 2: Timestamp request "when was it delivered" or "delivery date"
    res2 = SemanticDisambiguator.disambiguate("What was the delivery date of order 123?", cols)
    assert len(res2) == 1
    assert res2[0].selected_column == "order_delivered_customer_date"
    assert res2[0].confidence >= 0.90


def test_column_disambiguation_revenue_vs_volume():
    price_col = ColumnSemanticMetadata(
        database_id="E_commerce",
        table_name="order_items",
        column_name="price",
        data_type="FLOAT",
        semantic_role=SemanticRole.AMOUNT,
    )
    qty_col = ColumnSemanticMetadata(
        database_id="E_commerce",
        table_name="order_items",
        column_name="order_item_id",
        data_type="INT",
        semantic_role=SemanticRole.QUANTITY,
    )
    cols = [price_col, qty_col]

    # Query asking for volume
    res_vol = SemanticDisambiguator.disambiguate("Top 3 products by item sales volume", cols)
    assert len(res_vol) == 1
    assert res_vol[0].concept == "sales_volume"

    # Query asking for revenue
    res_rev = SemanticDisambiguator.disambiguate("Top 3 products by total revenue", cols)
    assert len(res_rev) == 1
    assert res_rev[0].selected_column == "price"


# ── G: Database Isolation (db_id Isolation) ───────────────────
def test_db_id_isolation():
    registry = SemanticRegistry()

    col_ecom = ColumnSemanticMetadata(
        database_id="E_commerce",
        table_name="orders",
        column_name="order_status",
        data_type="VARCHAR",
        semantic_role=SemanticRole.STATUS,
    )
    col_baseball = ColumnSemanticMetadata(
        database_id="Baseball",
        table_name="team",
        column_name="name",
        data_type="VARCHAR",
        semantic_role=SemanticRole.NAME,
    )

    registry.register_column_metadata(col_ecom)
    registry.register_column_metadata(col_baseball)

    # Verify E_commerce metadata does not leak into Baseball
    ecom_meta = registry.get_table_metadata("E_commerce", "orders")
    assert len(ecom_meta) == 1
    assert ecom_meta[0].column_name == "order_status"

    baseball_ecom_meta = registry.get_table_metadata("Baseball", "orders")
    assert baseball_ecom_meta == []

    baseball_team_meta = registry.get_table_metadata("Baseball", "team")
    assert len(baseball_team_meta) == 1
    assert baseball_team_meta[0].column_name == "name"


# ── H: Hybrid RAG Document Enrichment ─────────────────────────
def test_schema_enricher_includes_semantics():
    if not os.path.exists(ECOMMERCE_DB_PATH):
        pytest.skip("E_commerce.sqlite not found")

    pool = SQLitePool(ECOMMERCE_DB_PATH)
    enricher = SchemaEnricher(pool)
    doc_dict = enricher._enrich_table("orders", db_id="E_commerce")
    doc_text = doc_dict["document"]

    # Verify role and synonyms are present
    assert "[role: status]" in doc_text
    assert "Searchable terms: orders" in doc_text
    assert "state" in doc_text or "status" in doc_text


# ── I: SQL Generation Integration & Schema Retrieval Node ─────
def test_schema_retrieval_and_sql_gen_integration():
    if not os.path.exists(ECOMMERCE_DB_PATH):
        pytest.skip("E_commerce.sqlite not found")

    pool = SQLitePool(ECOMMERCE_DB_PATH)

    class MockRetriever:
        def retrieve(self, query, top_k=5, db_id="default"):
            return [
                "Table: orders\nColumns:\n  - order_id (int) [PRIMARY KEY]\n  - order_status (varchar) [role: status]\n  - order_delivered_customer_date (timestamp) [role: timestamp]"
            ]

    state: AgentState = {
        "user_query": "Find average price for delivered orders",
        "db_id": "E_commerce",
        "sql_dialect": "sqlite",
        "route_intent": "data_query",
        "entities": ["orders"],
        "retrieval_top_k": 3,
    }

    res = schema_retrieval_node(state, MockRetriever(), pool)
    assert "relevant_schema" in res
    assert "semantic_context" in res
    assert "order_status" in res["semantic_context"]
    assert "delivered" in res["semantic_context"].lower()


# ── J: Business Definitions Registration & Integration ────────
def test_business_definitions():
    registry = SemanticRegistry()
    bdef = BusinessDefinition(
        term="Delivered Order",
        meaning="Order whose lifecycle status is delivered",
        database_id="E_commerce",
        preferred_columns=["order_status"],
        formula_or_hint="order_status = 'delivered'",
    )
    registry.register_business_definition(bdef)

    defs = registry.get_business_definitions("E_commerce")
    assert len(defs) == 1
    assert defs[0].term == "Delivered Order"

    # Verify context generation includes the definition
    ctx = registry.get_semantic_context("Show count of Delivered Orders", "E_commerce", ["orders"])
    assert "Delivered Order" in ctx
    assert "order_status = 'delivered'" in ctx


# ── K: SQL Generation Node Injects Semantic Context ───────────
def test_sql_generation_node_includes_semantic_context():
    captured_messages = []

    class MockRouter:
        default_provider = "mock"
        def generate(self, messages, **kwargs):
            captured_messages.extend(messages)
            return '{"sql": "SELECT AVG(price) FROM order_items JOIN orders ON order_items.order_id = orders.order_id WHERE orders.order_status = \'delivered\'", "explanation": "test", "friendly_message": "test"}'

    state: AgentState = {
        "user_query": "Find average freight value and average price for delivered orders",
        "relevant_schema": "TABLE orders (order_id INT PK, order_status VARCHAR, order_delivered_customer_date TIMESTAMP)",
        "semantic_context": "-- BUSINESS SEMANTIC GUIDANCE --\nConcept 'delivered_condition': Use `order_status`",
        "sql_dialect": "sqlite",
        "trace_id": "test_trace",
        "eval_mode": True,
        "eval_temperature": 0.0,
        "eval_seed": 42,
        "pinned_provider": "groq",
    }

    res = sql_generation_node(state, MockRouter())
    assert res["generated_sql"] != ""
    assert res["eval_mode"] is True
    # Verify semantic context made it into prompt messages (rendered in system prompt)
    system_prompt = next(m["content"] for m in captured_messages if m["role"] == "system")
    assert "-- BUSINESS SEMANTIC GUIDANCE --" in system_prompt
    assert "Concept 'delivered_condition': Use `order_status`" in system_prompt


# ── L: Safety & Deterministic Mode Validation ─────────────────
def test_sql_safety_and_deterministic_mode_preserved():
    from app.agents.sql_validation import sql_validation_node

    # Valid SELECT query
    res_valid = sql_validation_node({"generated_sql": "SELECT AVG(price) FROM order_items WHERE order_status = 'delivered'"})
    assert res_valid["is_valid"] is True
    assert len(res_valid["validation_errors"]) == 0

    # DDL / DML attempt must be blocked
    res_drop = sql_validation_node({"generated_sql": "DROP TABLE orders"})
    assert res_drop["is_valid"] is False
    assert any("blocked" in err.lower() or "drop" in err.lower() for err in res_drop["validation_errors"])

    res_update = sql_validation_node({"generated_sql": "UPDATE orders SET order_status = 'delivered'"})
    assert res_update["is_valid"] is False
    assert any("blocked" in err.lower() or "update" in err.lower() for err in res_update["validation_errors"])

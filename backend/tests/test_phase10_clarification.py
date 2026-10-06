"""
Phase 10 Tests — Confidence-Aware Interactive Query Disambiguation.

Tests:
A. No ambiguity: Query with clear direct intent does not prompt clarification.
B. High-confidence semantic match: State condition with verified value auto-executes.
C. Multiple timestamp ambiguity: Ambiguous delivery date triggers structured clarification.
D. Timestamp with explicit wording: Carrier vs customer wording auto-executes dominant candidate.
E. Revenue wording: Explicit revenue term auto-executes amount metric.
F. Volume wording: Explicit units/items sold auto-executes quantity/count metric.
G. Ambiguous sales: General 'sales' query triggers revenue vs volume clarification.
H. Explicit BusinessDefinition: Glossary definition takes highest precedence and overrides ambiguity.
I. Numeric clarification response: User replying '1' resolves to option 1.
J. Natural language clarification response: User replying 'Customer delivery date' resolves correctly.
K. Invalid clarification response: Unrelated text safely rejected and prompts re-clarification.
L. db_id isolation: Ambiguities and candidates strictly isolated per database.
M. Security & SQL safety regression: Clarification responses never bypass AST safety or guardrails.
"""

import pytest
from app.semantics.models import (
    SemanticRole,
    ColumnSemanticMetadata,
    BusinessDefinition,
    AmbiguityType,
    SemanticAmbiguity,
    ClarificationCandidate,
)
from app.semantics.ambiguity_detector import AmbiguityDetector
from app.semantics.registry import SemanticRegistry, get_semantic_registry
from app.agents.state import AgentState
from app.agents.schema_retrieval import schema_retrieval_node
from app.agents.sql_validation import sql_validation_node


# ── Fixtures & Mock Columns ────────────────────────────────────────────────

def get_ecommerce_test_columns():
    return [
        ColumnSemanticMetadata(
            database_id="E_commerce",
            table_name="orders",
            column_name="order_id",
            data_type="VARCHAR",
            semantic_role=SemanticRole.IDENTIFIER,
        ),
        ColumnSemanticMetadata(
            database_id="E_commerce",
            table_name="orders",
            column_name="order_status",
            data_type="VARCHAR",
            semantic_role=SemanticRole.STATUS,
            sample_values=["delivered", "shipped", "canceled"],
            confidence=0.95,
        ),
        ColumnSemanticMetadata(
            database_id="E_commerce",
            table_name="orders",
            column_name="order_delivered_customer_date",
            data_type="TIMESTAMP",
            semantic_role=SemanticRole.TIMESTAMP,
            confidence=0.90,
            synonyms=["delivery date", "customer received date"],
        ),
        ColumnSemanticMetadata(
            database_id="E_commerce",
            table_name="orders",
            column_name="order_delivered_carrier_date",
            data_type="TIMESTAMP",
            semantic_role=SemanticRole.TIMESTAMP,
            confidence=0.90,
            synonyms=["shipping date", "carrier delivery date"],
        ),
        ColumnSemanticMetadata(
            database_id="E_commerce",
            table_name="order_items",
            column_name="price",
            data_type="FLOAT",
            semantic_role=SemanticRole.AMOUNT,
            confidence=0.95,
            synonyms=["sales amount", "monetary price"],
        ),
        ColumnSemanticMetadata(
            database_id="E_commerce",
            table_name="order_items",
            column_name="order_item_id",
            data_type="INT",
            semantic_role=SemanticRole.QUANTITY,
            confidence=0.90,
            synonyms=["item volume", "units count"],
        ),
    ]


# ── Category A: No Ambiguity ────────────────────────────────────────────────

def test_category_a_no_ambiguity():
    """Unambiguous queries should NOT trigger clarification."""
    cols = get_ecommerce_test_columns()
    ambiguities = AmbiguityDetector.detect_ambiguities("How many orders are there in total?", cols)
    needing_clarif = [a for a in ambiguities if a.requires_clarification]
    assert len(needing_clarif) == 0


# ── Category B: High-Confidence Semantic Match ─────────────────────────────

def test_category_b_high_confidence_semantic_match():
    """Query filtering by lifecycle status with verified sample values auto-executes."""
    cols = get_ecommerce_test_columns()
    ambiguities = AmbiguityDetector.detect_ambiguities("Find the average price for delivered orders.", cols)
    # Status vs timestamp detected, but status has verified 'delivered' sample value
    assert any(a.concept == "delivered_condition" for a in ambiguities)
    delivered_amb = next(a for a in ambiguities if a.concept == "delivered_condition")
    assert delivered_amb.requires_clarification is False
    assert delivered_amb.top_confidence >= 0.85
    assert delivered_amb.candidates[0].column_name == "order_status"


# ── Category C: Multiple Timestamp Ambiguity ────────────────────────────────

def test_category_c_multiple_timestamp_ambiguity():
    """Unqualified delivery date request triggers structured clarification."""
    cols = get_ecommerce_test_columns()
    ambiguities = AmbiguityDetector.detect_ambiguities("When were the orders delivered?", cols)
    ts_amb = [a for a in ambiguities if a.ambiguity_type == AmbiguityType.MULTIPLE_TIMESTAMP]
    assert len(ts_amb) == 1
    amb = ts_amb[0]
    assert amb.requires_clarification is True
    assert len(amb.candidates) >= 2
    cand_names = [c.column_name for c in amb.candidates]
    assert "order_delivered_customer_date" in cand_names
    assert "order_delivered_carrier_date" in cand_names
    assert "Which one do you mean?" in amb.question


# ── Category D: Timestamp With Explicit Wording ────────────────────────────

def test_category_d_timestamp_with_explicit_wording():
    """Explicitly qualified carrier delivery auto-executes carrier timestamp."""
    cols = get_ecommerce_test_columns()
    ambiguities = AmbiguityDetector.detect_ambiguities("When did the carrier deliver the orders?", cols)
    ts_amb = [a for a in ambiguities if a.ambiguity_type == AmbiguityType.MULTIPLE_TIMESTAMP]
    assert len(ts_amb) == 1
    amb = ts_amb[0]
    assert amb.requires_clarification is False
    assert amb.candidates[0].candidate_id == "carrier_delivery"
    assert amb.candidates[0].column_name == "order_delivered_carrier_date"
    assert amb.top_confidence >= 0.90


# ── Category E: Explicit Revenue Metric ─────────────────────────────────────

def test_category_e_explicit_revenue_wording():
    """Explicit query for revenue auto-executes monetary amount."""
    cols = get_ecommerce_test_columns()
    ambiguities = AmbiguityDetector.detect_ambiguities("Top 5 products by revenue", cols)
    rev_amb = [a for a in ambiguities if a.ambiguity_type == AmbiguityType.REVENUE_VS_VOLUME]
    assert len(rev_amb) == 1
    amb = rev_amb[0]
    assert amb.requires_clarification is False
    assert amb.candidates[0].candidate_id == "revenue"
    assert amb.candidates[0].column_name == "price"


# ── Category F: Explicit Volume Metric ─────────────────────────────────────

def test_category_f_explicit_volume_wording():
    """Explicit query for units sold auto-executes quantity/count metric."""
    cols = get_ecommerce_test_columns()
    ambiguities = AmbiguityDetector.detect_ambiguities("Top 5 products by units sold", cols)
    rev_amb = [a for a in ambiguities if a.ambiguity_type == AmbiguityType.REVENUE_VS_VOLUME]
    assert len(rev_amb) == 1
    amb = rev_amb[0]
    assert amb.requires_clarification is False
    assert amb.candidates[0].candidate_id == "volume"


# ── Category G: Ambiguous Sales Query ───────────────────────────────────────

def test_category_g_ambiguous_sales_query():
    """General 'sales' query without monetary or volume qualification asks for clarification."""
    cols = get_ecommerce_test_columns()
    ambiguities = AmbiguityDetector.detect_ambiguities("What are the top 5 products by sales?", cols)
    rev_amb = [a for a in ambiguities if a.ambiguity_type == AmbiguityType.REVENUE_VS_VOLUME]
    assert len(rev_amb) == 1
    amb = rev_amb[0]
    assert amb.requires_clarification is True
    assert "revenue or number of units" in amb.question


# ── Category H: Explicit BusinessDefinition Precedence ─────────────────────

def test_category_h_business_definition_precedence():
    """Glossary definition takes highest precedence and overrides inferred ambiguity."""
    cols = get_ecommerce_test_columns()
    bdef = BusinessDefinition(
        term="sales",
        meaning="Total monetary revenue generated from completed order items",
        database_id="E_commerce",
        preferred_columns=["price"],
        formula_or_hint="SUM(price)",
    )
    # Query 'by sales' would be ambiguous without glossary definition
    ambiguities = AmbiguityDetector.detect_ambiguities(
        query="What are the top 5 products by sales?",
        columns_metadata=cols,
        business_definitions=[bdef],
    )
    rev_amb = [a for a in ambiguities if a.ambiguity_type == AmbiguityType.REVENUE_VS_VOLUME]
    assert len(rev_amb) == 1
    amb = rev_amb[0]
    # Business definition must override ambiguity
    assert amb.requires_clarification is False
    assert amb.top_confidence >= 0.95
    assert amb.candidates[0].column_name == "price"


# ── Category I & J: Clarification Response Resolution ──────────────────────

def test_category_i_numeric_clarification_response():
    """Numeric response '1' maps to first candidate option."""
    cols = get_ecommerce_test_columns()
    ambiguities = AmbiguityDetector.detect_ambiguities("When were the orders delivered?", cols)
    amb = next(a for a in ambiguities if a.ambiguity_type == AmbiguityType.MULTIPLE_TIMESTAMP)

    # Option 1
    res1 = AmbiguityDetector.resolve_clarification("1", amb)
    assert res1 is not None
    assert res1.candidate_id == amb.candidates[0].candidate_id

    # Option 2
    res2 = AmbiguityDetector.resolve_clarification("option 2", amb)
    assert res2 is not None
    assert res2.candidate_id == amb.candidates[1].candidate_id


def test_category_j_natural_language_clarification_response():
    """Natural language responses map safely to candidate options."""
    cols = get_ecommerce_test_columns()
    ambiguities = AmbiguityDetector.detect_ambiguities("When were the orders delivered?", cols)
    amb = next(a for a in ambiguities if a.ambiguity_type == AmbiguityType.MULTIPLE_TIMESTAMP)

    res_cust = AmbiguityDetector.resolve_clarification("I mean the customer delivery date", amb)
    assert res_cust is not None
    assert res_cust.candidate_id == "customer_delivery"

    res_carrier = AmbiguityDetector.resolve_clarification("carrier delivery", amb)
    assert res_carrier is not None
    assert res_carrier.candidate_id == "carrier_delivery"


# ── Category K: Invalid Response Handling ──────────────────────────────────

def test_category_k_invalid_clarification_response():
    """Unrecognized / unrelated inputs return None and prevent arbitrary SQL execution."""
    cols = get_ecommerce_test_columns()
    ambiguities = AmbiguityDetector.detect_ambiguities("When were the orders delivered?", cols)
    amb = next(a for a in ambiguities if a.ambiguity_type == AmbiguityType.MULTIPLE_TIMESTAMP)

    res_bad = AmbiguityDetector.resolve_clarification("blue elephant peanut butter", amb)
    assert res_bad is None

    res_empty = AmbiguityDetector.resolve_clarification("", amb)
    assert res_empty is None


# ── Category L: db_id Isolation ─────────────────────────────────────────────

def test_category_l_db_id_isolation():
    """Ambiguities and metadata from Database A never leak into Database B."""
    registry = SemanticRegistry()
    cols_ecom = get_ecommerce_test_columns()
    for col in cols_ecom:
        registry.register_column_metadata(col)

    # Database B: Baseball has no orders table or delivery dates
    baseball_ambiguities = registry.detect_ambiguities(
        query="When were orders delivered?",
        db_id="Baseball",
        relevant_tables=["team"],
    )
    assert len(baseball_ambiguities) == 0


# ── Category M: Security & SQL Safety Regression ───────────────────────────

def test_category_m_security_regression():
    """Clarification inputs with injection attacks are safely rejected and blocked."""
    cols = get_ecommerce_test_columns()
    ambiguities = AmbiguityDetector.detect_ambiguities("When were the orders delivered?", cols)
    amb = next(a for a in ambiguities if a.ambiguity_type == AmbiguityType.MULTIPLE_TIMESTAMP)

    malicious_inputs = [
        "1; DROP TABLE orders; --",
        "' OR '1'='1",
        "UNION SELECT password FROM users",
        "<script>alert(1)</script>",
    ]
    for attack in malicious_inputs:
        res = AmbiguityDetector.resolve_clarification(attack, amb)
        # Malicious string should NOT resolve to any arbitrary column
        assert res is None or res.candidate_id in ["customer_delivery", "carrier_delivery"]

    # Even if SQL generation is attempted with an attack string, AST validator must block it
    injection_sql = "SELECT * FROM orders; DROP TABLE orders;"
    val_res = sql_validation_node({"generated_sql": injection_sql})
    assert val_res["is_valid"] is False
    assert len(val_res["validation_errors"]) > 0


# ── End-to-End Node Integration: Pause & Resume ────────────────────────────

def test_end_to_end_clarification_pause_and_resume():
    """Verify schema_retrieval_node pauses on ambiguity, and resumes when clarification is provided."""
    registry = get_semantic_registry()
    cols = get_ecommerce_test_columns()
    for col in cols:
        registry.register_column_metadata(col)

    class MockPool:
        def get_tables(self):
            return ["orders", "order_items"]
        def get_table_schema(self, table):
            return []

    class MockRetriever:
        def retrieve(self, query, top_k=5, db_id="default"):
            return ["Table: orders\nTable: order_items"]

    # 1. Initial query: triggers pause
    state1: AgentState = {
        "user_query": "When were orders delivered?",
        "db_id": "E_commerce",
        "sql_dialect": "sqlite",
        "route_intent": "data_query",
        "entities": ["orders"],
        "retrieval_top_k": 3,
    }
    res1 = schema_retrieval_node(state1, MockRetriever(), MockPool())
    assert res1["requires_clarification"] is True
    assert res1["active_clarification"] is not None
    assert "friendly_message" in res1
    assert "1." in res1["friendly_message"]

    # 2. Follow-up: User responds "1"
    state2: AgentState = {
        **state1,
        "active_clarification": res1["active_clarification"],
        "clarification_response": "1",
    }
    res2 = schema_retrieval_node(state2, MockRetriever(), MockPool())
    assert res2["requires_clarification"] is False
    assert res2["clarification_resolved"] is True
    assert res2["selected_candidate"] is not None
    assert "RESOLVED BUSINESS SEMANTIC CLARIFICATION" in res2["semantic_context"]
    assert "order_delivered_customer_date" in res2["semantic_context"]

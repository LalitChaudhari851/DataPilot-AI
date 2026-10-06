"""
backend/tests/test_production_smoke.py — Production Smoke Test Suite.

Tests the complete end-to-end pipeline against the production TiDB Cloud database:
1. Simple COUNT query
2. Filtering query
3. JOIN query
4. GROUP BY query
5. Top-N query
6. Time-series query
7. Business metric query (ARR / NRR)
8. Ambiguous query (clarification / assumptions)
9. Invalid/destructive SQL attempt (blocked by guardrails)
10. Database switching (default TiDB Cloud)
"""

import pytest
import os
import sys

from app.config import get_settings
from app.db.connection import DatabasePool
from app.llm.router import ModelRouter
from app.rag.retriever import HybridRetriever
from app.agents.orchestrator import AgentOrchestrator
from app.agents.sql_validation import sql_validation_node


@pytest.fixture(scope="module")
def pipeline():
    settings = get_settings()
    pool = DatabasePool(settings.DB_URI)
    llm_config = {
        "default_provider": settings.DEFAULT_LLM_PROVIDER,
        "groq_api_key": settings.GROQ_API_KEY,
        "groq_model_primary": settings.GROQ_MODEL_PRIMARY,
        "groq_model_fast": settings.GROQ_MODEL_FAST,
        "groq_base_url": settings.GROQ_BASE_URL,
        "huggingface_token": settings.HUGGINGFACEHUB_API_TOKEN,
        "huggingface_model": settings.DEFAULT_MODEL,
        "openai_api_key": settings.OPENAI_API_KEY,
        "anthropic_api_key": settings.ANTHROPIC_API_KEY,
        "ollama_base_url": settings.OLLAMA_BASE_URL,
    }
    llm_router = ModelRouter(llm_config)
    rag_retriever = HybridRetriever(pool, chroma_persist_dir=settings.CHROMA_PERSIST_DIR)
    orchestrator = AgentOrchestrator(llm_router, rag_retriever, pool)
    return {
        "pool": pool,
        "retriever": rag_retriever,
        "orchestrator": orchestrator,
    }


def test_1_simple_count_query(pipeline):
    """1. Simple COUNT query."""
    res = pipeline["pool"].execute_query("SELECT COUNT(*) as cnt FROM accounts")
    assert len(res) == 1
    assert res[0]["cnt"] == 120


def test_2_filtering_query(pipeline):
    """2. Filtering query."""
    res = pipeline["pool"].execute_query("SELECT account_name, segment, arr_band FROM accounts WHERE segment = 'enterprise' LIMIT 5")
    assert len(res) > 0
    assert all(r["segment"] == "enterprise" for r in res)


def test_3_join_query(pipeline):
    """3. JOIN query across accounts and subscriptions."""
    sql = """
        SELECT a.account_name, s.status, s.contracted_arr
        FROM accounts a
        JOIN subscriptions s ON a.account_id = s.account_id
        WHERE s.status = 'active'
        LIMIT 5
    """
    res = pipeline["pool"].execute_query(sql)
    assert len(res) == 5
    assert all(r["status"] == "active" for r in res)


def test_4_group_by_query(pipeline):
    """4. GROUP BY query."""
    sql = """
        SELECT industry, COUNT(*) as account_count, AVG(health_score) as avg_health
        FROM accounts
        GROUP BY industry
        ORDER BY account_count DESC
    """
    res = pipeline["pool"].execute_query(sql)
    assert len(res) > 0
    assert "account_count" in res[0]


def test_5_top_n_query(pipeline):
    """5. Top-N query."""
    sql = """
        SELECT account_name, health_score
        FROM accounts
        ORDER BY health_score DESC
        LIMIT 5
    """
    res = pipeline["pool"].execute_query(sql)
    assert len(res) == 5
    scores = [r["health_score"] for r in res]
    assert scores == sorted(scores, reverse=True)


def test_6_time_series_query(pipeline):
    """6. Time-series query on daily product usage."""
    sql = """
        SELECT usage_date, SUM(active_users) as daily_users
        FROM product_usage_daily
        GROUP BY usage_date
        ORDER BY usage_date DESC
        LIMIT 7
    """
    res = pipeline["pool"].execute_query(sql)
    assert len(res) == 7
    assert all("daily_users" in r for r in res)


def test_7_business_metric_arr_nrr(pipeline):
    """7. Business metric query: Active ARR & NRR calculation."""
    sql = """
        SELECT 
            SUM(CASE WHEN status = 'active' THEN contracted_arr ELSE 0 END) as active_arr,
            SUM(contracted_arr) as total_arr,
            ROUND(SUM(CASE WHEN status = 'active' THEN contracted_arr ELSE 0 END) * 100.0 / SUM(contracted_arr), 2) as nrr_pct
        FROM subscriptions
    """
    res = pipeline["pool"].execute_query(sql)
    assert len(res) == 1
    assert float(res[0]["active_arr"]) > 0
    assert float(res[0]["nrr_pct"]) > 0


def test_8_ambiguous_query_handling(pipeline):
    """8. Ambiguous query handled safely via query understanding."""
    orchestrator = pipeline["orchestrator"]
    result = orchestrator.process_query("Tell me about revenue")
    assert result is not None
    # Either generates a valid query or asks clarification, but must not crash
    assert "error" not in result or result.get("sanitized_sql") or result.get("requires_clarification")


def test_9_destructive_sql_blocked(pipeline):
    """9. Invalid/destructive SQL attempt blocked by AST safety validator."""
    destructive_queries = [
        "DROP TABLE accounts",
        "DELETE FROM invoices WHERE invoice_id > 0",
        "TRUNCATE TABLE messages",
        "ALTER TABLE payments DROP COLUMN amount",
    ]
    for bad_sql in destructive_queries:
        state = {"generated_sql": bad_sql, "trace_id": "test_block", "retry_count": 0}
        val = sql_validation_node(state)
        assert not val.get("is_valid", False), f"Failed to block: {bad_sql}"
        assert len(val.get("validation_errors", [])) > 0


def test_10_database_registry_and_switching(pipeline):
    """10. Database registry provides default pool with TiDB tables."""
    from app.db.registry import get_database_registry
    registry = get_database_registry()
    default_pool = registry.get_pool("default")
    assert default_pool is not None
    tables = default_pool.get_tables()
    assert "accounts" in tables
    assert "subscriptions" in tables
    assert "invoices" in tables
    assert len(tables) >= 18

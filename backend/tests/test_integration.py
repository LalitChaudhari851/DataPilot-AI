"""
Integration Tests — API-level tests that verify the full request lifecycle.
Tests authentication, input validation, rate limiting, and error handling.
"""

import pytest
from unittest.mock import MagicMock


# ── Test Redis Client ────────────────────────────────────────


class TestRedisCache:
    """Test Redis cache with fallback behavior."""

    def test_fallback_to_inmemory_when_redis_unavailable(self):
        """When Redis URL is invalid, factory should return in-memory cache."""
        from app.cache.redis_client import create_cache
        cache = create_cache(redis_url="redis://invalid-host:9999", ttl_seconds=60)
        # Should fall back to QueryCache
        from app.api.middleware import QueryCache
        assert isinstance(cache, QueryCache)

    def test_fallback_to_inmemory_when_no_url(self):
        """When no Redis URL provided, factory should return in-memory cache."""
        from app.cache.redis_client import create_cache
        cache = create_cache(redis_url=None, ttl_seconds=60)
        from app.api.middleware import QueryCache
        assert isinstance(cache, QueryCache)

    def test_inmemory_cache_operations(self):
        """Verify the in-memory fallback cache works correctly."""
        from app.api.middleware import QueryCache
        cache = QueryCache(ttl_seconds=300)

        # SET and GET
        cache.set("test query", {"sql": "SELECT 1"}, tenant_id="t1")
        result = cache.get("test query", tenant_id="t1")
        assert result is not None
        assert result["sql"] == "SELECT 1"

        # Different tenant should not see the data
        result2 = cache.get("test query", tenant_id="t2")
        assert result2 is None

    def test_cache_tenant_isolation_on_invalidate(self):
        """Verify invalidating one tenant doesn't affect another."""
        from app.api.middleware import QueryCache
        cache = QueryCache(ttl_seconds=300)

        cache.set("q1", {"data": "tenant_a"}, tenant_id="a")
        cache.set("q2", {"data": "tenant_b"}, tenant_id="b")

        # Invalidate only tenant A
        cache.invalidate(tenant_id="a")

        assert cache.get("q1", tenant_id="a") is None
        assert cache.get("q2", tenant_id="b") is not None


class TestRedisRateLimiter:
    """Test rate limiter with fallback behavior."""

    def test_fallback_to_inmemory_when_redis_unavailable(self):
        from app.cache.redis_client import create_rate_limiter
        limiter = create_rate_limiter(redis_url="redis://invalid-host:9999", rpm=60)
        from app.api.middleware import InMemoryRateLimiter
        assert isinstance(limiter, InMemoryRateLimiter)

    def test_inmemory_rate_limiter_allows_under_limit(self):
        from app.api.middleware import InMemoryRateLimiter
        limiter = InMemoryRateLimiter(requests_per_minute=5)
        for _ in range(5):
            assert limiter.check("test_user") is True

    def test_inmemory_rate_limiter_blocks_over_limit(self):
        from app.api.middleware import InMemoryRateLimiter
        limiter = InMemoryRateLimiter(requests_per_minute=3)
        for _ in range(3):
            limiter.check("test_user")
        assert limiter.check("test_user") is False


# ── Test Execution Agent Cost Estimation ─────────────────


class TestQueryCostEstimation:
    """Test the EXPLAIN-based cost estimation in the execution agent."""

    def test_safe_query_passes(self):
        """A small query should pass cost estimation."""
        from app.agents.execution import _estimate_query_cost

        mock_pool = MagicMock()
        mock_pool.execute_query.return_value = [
            {"id": 1, "type": "ref", "rows": 5, "Extra": "Using index"}
        ]

        cost = _estimate_query_cost(mock_pool, "SELECT * FROM employees WHERE id = 1")
        assert cost["safe"] is True
        assert cost["estimated_rows"] == 5

    def test_expensive_full_scan_blocked(self):
        """A full table scan over the threshold should be blocked."""
        from app.agents.execution import _estimate_query_cost

        mock_pool = MagicMock()
        mock_pool.execute_query.return_value = [
            {"id": 1, "type": "ALL", "rows": 500000, "Extra": ""}
        ]

        cost = _estimate_query_cost(mock_pool, "SELECT * FROM huge_table")
        assert cost["safe"] is False
        assert cost["has_full_scan"] is True

    def test_explain_failure_fails_open(self):
        """If EXPLAIN itself fails, the query should be allowed (fail open)."""
        from app.agents.execution import _estimate_query_cost

        mock_pool = MagicMock()
        mock_pool.execute_query.side_effect = Exception("EXPLAIN failed")

        cost = _estimate_query_cost(mock_pool, "SELECT 1")
        assert cost["safe"] is True

    def test_execution_node_blocks_expensive_query(self):
        """The full execution node should return error for expensive queries."""
        from app.agents.execution import execution_node

        mock_pool = MagicMock()
        # EXPLAIN returns expensive result
        mock_pool.execute_query.return_value = [
            {"id": 1, "type": "ALL", "rows": 200000, "Extra": ""}
        ]

        state = {
            "sanitized_sql": "SELECT * FROM huge_table",
            "trace_id": "test123",
        }

        result = execution_node(state, mock_pool)
        assert result["error"] is not None
        assert "too expensive" in result["error"].lower() or "blocked" in result["error"].lower()
        assert result["row_count"] == 0


# ── Test Guardrail Integration ───────────────────────────


class TestGuardrailIntegration:
    """Test that the OutputGuardrail correctly catches hallucinated references."""

    def test_valid_sql_passes(self):
        from app.agents.guardrails import OutputGuardrail

        guardrail = OutputGuardrail(
            known_tables={"employees", "departments"},
            known_columns={
                "employees": {"id", "name", "salary", "department_id"},
                "departments": {"id", "name", "budget"},
            },
        )
        warnings = guardrail.validate_sql_references(
            "SELECT name, salary FROM employees WHERE salary > 80000"
        )
        assert len(warnings) == 0

    def test_hallucinated_table_detected(self):
        from app.agents.guardrails import OutputGuardrail

        guardrail = OutputGuardrail(
            known_tables={"employees"},
            known_columns={"employees": {"id", "name"}},
        )
        warnings = guardrail.validate_sql_references(
            "SELECT * FROM nonexistent_table"
        )
        assert len(warnings) > 0

    def test_confidence_score_valid_query(self):
        from app.agents.guardrails import OutputGuardrail

        guardrail = OutputGuardrail(
            known_tables={"employees"},
            known_columns={"employees": {"id", "name", "salary"}},
        )
        confidence = guardrail.score_confidence(
            "SELECT name, salary FROM employees"
        )
        assert 0.0 <= confidence <= 1.0


# ── Test Per-Agent Metrics ───────────────────────────────


class TestAgentMetrics:
    """Test that per-agent metrics are recorded correctly.

    When prometheus_client is installed, MetricsCollector uses module-level
    Prometheus singletons and only recognises pre-registered metric names.
    When it isn't installed, a plain in-memory dict collector is used.
    These tests cover both paths.
    """

    def test_metrics_collector_increment(self):
        """Increment a registered counter and verify it increases."""
        from app.observability.metrics import MetricsCollector
        m = MetricsCollector()
        # Use a real registered metric name so Prometheus path works
        m.increment("plainsql_queries_total", {"intent": "test", "status": "success"})
        m.increment("plainsql_queries_total", {"intent": "test", "status": "success"})
        count = m.get_counter("plainsql_queries_total", {"intent": "test", "status": "success"})
        # Prometheus counters are global singletons — value may include prior runs
        assert count >= 2

    def test_metrics_collector_observe(self):
        """Observe histogram values and verify stats are returned."""
        from app.observability.metrics import MetricsCollector
        m = MetricsCollector()
        m.observe("plainsql_query_latency_ms", 100.5, {"intent": "test_obs"})
        m.observe("plainsql_query_latency_ms", 200.0, {"intent": "test_obs"})
        stats = m.get_histogram_stats("plainsql_query_latency_ms", {"intent": "test_obs"})
        # Both backends return a dict with a "count" key
        assert isinstance(stats, dict)
        assert "count" in stats

    def test_metrics_all_metrics_export(self):
        """Verify get_all_metrics returns a well-formed structure."""
        from app.observability.metrics import MetricsCollector, _PROM_AVAILABLE
        m = MetricsCollector()
        m.increment("plainsql_queries_total", {"intent": "export", "status": "success"})
        m.set_gauge("plainsql_active_requests", 5)
        export = m.get_all_metrics()
        assert isinstance(export, dict)
        if _PROM_AVAILABLE:
            # Prometheus backend returns pointer to /metrics endpoint
            assert "format" in export or "endpoint" in export
        else:
            # Fallback backend returns raw counters/gauges/histograms
            assert "counters" in export
            assert "gauges" in export
            assert "histograms" in export


# ── Test Config Validation ───────────────────────────────


class TestConfigValidation:
    """Test that production secret validation works."""

    def test_default_jwt_blocked_in_production(self):
        """Production deployment with default JWT key should fail."""
        import os
        from pydantic import ValidationError

        env_backup = os.environ.get("ENV")
        db_backup = os.environ.get("DB_URI")
        os.environ["ENV"] = "production"
        os.environ["DB_URI"] = "mysql+pymysql://test:test@localhost/test"

        try:
            from importlib import reload
            import app.config as config_module
            reload(config_module)

            with pytest.raises(ValidationError) as exc_info:
                config_module.Settings()
            assert "JWT_SECRET_KEY" in str(exc_info.value)
        finally:
            if env_backup is not None:
                os.environ["ENV"] = env_backup
            else:
                os.environ.pop("ENV", None)
            if db_backup is not None:
                os.environ["DB_URI"] = db_backup
            else:
                os.environ.pop("DB_URI", None)
            import app.config as config_module
            reload(config_module)

    def test_development_allows_default_jwt(self):
        """Development mode should allow the default JWT key."""
        import os

        env_backup = os.environ.get("ENV")
        db_backup = os.environ.get("DB_URI")
        os.environ["ENV"] = "development"
        os.environ["DB_URI"] = "mysql+pymysql://test:test@localhost/test"

        try:
            from importlib import reload
            import app.config as config_module
            reload(config_module)

            settings = config_module.Settings()
            assert settings.ENV == "development"
        finally:
            if env_backup is not None:
                os.environ["ENV"] = env_backup
            else:
                os.environ.pop("ENV", None)
            if db_backup is not None:
                os.environ["DB_URI"] = db_backup
            else:
                os.environ.pop("DB_URI", None)
            import app.config as config_module
            reload(config_module)

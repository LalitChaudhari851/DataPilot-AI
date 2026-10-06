"""
Monitoring Routes — Prometheus-native metrics and dashboard JSON endpoint.
Exposes query volume, latency, success rates, and error breakdowns.

Uses prometheus_client when available for native Prometheus format.
Falls back to in-process metrics for local development.
"""

from fastapi import APIRouter, Response
from app.observability.metrics import (
    metrics,
    get_prometheus_metrics,
    get_prometheus_content_type,
)

router = APIRouter(prefix="/api/v1/metrics", tags=["Monitoring"])


class QueryMetricsRecorder:
    """
    Thin wrapper that records query-level metrics into the unified MetricsCollector.
    This replaces the old duplicate MetricsCollector that was in this file.
    """

    def record_query(
        self,
        latency_ms: float,
        intent: str = "unknown",
        success: bool = True,
        error_agent: str = None,
    ):
        """Record a single query execution into the unified metrics system."""
        status = "success" if success else "error"
        metrics.increment("plainsql_queries_total", {"intent": intent, "status": status})
        metrics.observe("plainsql_query_latency_ms", latency_ms, {"intent": intent})

        if not success and error_agent:
            metrics.increment("plainsql_agent_errors_total", {"agent": error_agent})


# Module-level singleton
_recorder = QueryMetricsRecorder()


def get_metrics_collector() -> QueryMetricsRecorder:
    """Get the global metrics recorder singleton."""
    return _recorder


def create_monitoring_router() -> APIRouter:
    """Factory to create monitoring router."""

    @router.get("")
    @router.get("/")
    @router.get("/prometheus")
    def prometheus_metrics_endpoint():
        """
        Expose metrics in Prometheus exposition format for scraping.
        Uses native prometheus_client output when available.
        """
        content = get_prometheus_metrics()
        return Response(
            content=content,
            media_type=get_prometheus_content_type(),
        )

    @router.get("/dashboard")
    def dashboard_metrics():
        """JSON metrics for the frontend or internal dashboard."""
        return metrics.get_all_metrics()

    return router

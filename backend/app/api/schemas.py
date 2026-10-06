"""
Pydantic Request/Response Schemas — Type-safe API contracts.
"""

from pydantic import BaseModel, Field
from typing import Optional, Any


# ── Chat / Query Schemas ─────────────────────────────────

class GenerateSQLRequest(BaseModel):
    """Request to generate SQL from natural language."""
    question: str = Field(..., min_length=1, max_length=1000, description="Natural language question")
    history: list[dict] = Field(default=[], description="Conversation history")
    execute: bool = Field(default=True, description="Execute the generated SQL immediately")
    db_id: Optional[str] = Field(default="default", description="Target database identifier")
    clarification_response: Optional[str] = Field(default=None, description="User response to a clarification prompt")
    active_clarification: Optional[dict] = Field(default=None, description="Pending clarification state")


class QueryResult(BaseModel):
    """Unified query response."""
    trace_id: str
    question: str
    intent: Optional[str] = None
    sql: Optional[str] = None
    sql_explanation: Optional[str] = None
    message: str
    data: list[dict] = []
    row_count: int = 0
    column_names: list[str] = []
    execution_time_ms: float = 0
    chart_config: Optional[dict] = None
    chart_type: Optional[str] = None
    insights: list[str] = []
    follow_ups: list[str] = []
    error: Optional[str] = None
    requires_clarification: bool = False
    clarification: Optional[dict] = None
    semantic_assumptions: list[dict] = []



class ExecuteQueryRequest(BaseModel):
    """Request to execute a specific SQL query."""
    sql: str = Field(..., min_length=1, max_length=5000, description="SQL query to execute")


# ── Auth Schemas ─────────────────────────────────────────

class LoginRequest(BaseModel):
    """Login credentials."""
    username: str = Field(..., min_length=2, max_length=50)
    password: str = Field(..., min_length=6, max_length=128)


class RegisterRequest(BaseModel):
    """New user registration."""
    username: str = Field(..., min_length=2, max_length=50)
    password: str = Field(..., min_length=6, max_length=128)
    email: str = Field(..., max_length=100)
    role: str = Field(default="viewer", description="viewer | analyst | admin")
    tenant_id: str = Field(default="default")


class TokenResponse(BaseModel):
    """JWT token response."""
    access_token: str
    token_type: str = "bearer"
    role: str
    tenant_id: str
    expires_in: int = 28800  # 8 hours in seconds


class APIKeyRequest(BaseModel):
    """Create API key request."""
    key_name: str = Field(..., min_length=2, max_length=50)
    role: str = Field(default="analyst")


class APIKeyResponse(BaseModel):
    """API key creation response."""
    key_name: str
    api_key: str
    role: str
    expires_in_days: int = 90


# ── Explain / Insights Schemas ───────────────────────────

class ExplainRequest(BaseModel):
    """Request to explain SQL in natural language."""
    sql: str = Field(..., min_length=1, max_length=5000)
    result_count: int = Field(default=0, ge=0)


class ExplainResponse(BaseModel):
    """SQL explanation response."""
    sql: str
    explanation: str


class InsightsRequest(BaseModel):
    """Request auto-insights for data."""
    data: list[dict] = Field(..., min_length=1)
    query: str = Field(default="")


class InsightsResponse(BaseModel):
    """Generated insights response."""
    insights: list[str]
    anomalies: list[dict] = []


# ── Schema / Analytics Schemas ───────────────────────────

class SchemaResponse(BaseModel):
    """Database schema response."""
    tables: list[str]
    schema_text: str
    table_count: int


class AnalyticsResponse(BaseModel):
    """Usage analytics response."""
    total_queries: int
    successful_queries: int
    failed_queries: int
    latency_stats: dict
    all_metrics: dict


# ── Health Check ─────────────────────────────────────────

class HealthResponse(BaseModel):
    """System health check response."""
    status: str
    version: str
    database: str
    llm_providers: dict[str, Any]
    rag_indexed_tables: int
    uptime_seconds: float

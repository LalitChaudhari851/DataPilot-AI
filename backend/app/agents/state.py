"""
Agent State — Shared TypedDict for the LangGraph multi-agent pipeline.
Every agent reads from and writes to this state object.
"""

from typing import TypedDict, Optional, Literal


class AgentState(TypedDict, total=False):
    """
    Shared state flowing through the LangGraph agent pipeline.
    Each agent enriches specific fields and passes state forward.
    """

    # ── Input ────────────────────────────────────────────
    user_query: str
    conversation_history: list[dict]
    tenant_id: str
    user_role: str
    trace_id: str
    db_id: Optional[str]                   # Database identifier (e.g., "default", "E_commerce")
    sql_dialect: Optional[Literal["mysql", "sqlite"]]  # SQL dialect for generation/execution

    # ── Query Understanding Agent Output ─────────────────
    intent: Literal[
        "chat",             # Natural conversation
        "sql",              # Database query that should continue to SQL generation
    ]
    route_intent: Literal[
        "chat",             # Hi, hello, thanks, capabilities, etc.
        "data_query",       # SELECT with filters
        "aggregation",      # COUNT, SUM, AVG, GROUP BY
        "comparison",       # Compare datasets
        "explanation",      # Explain a concept or previous query
        "meta_query",       # Questions about the schema itself
    ]
    entities: list[str]                # Extracted table/column names
    complexity: Literal["simple", "moderate", "complex"]
    retrieval_top_k: int               # How many schema docs to fetch (3=simple, 5=default, 8=complex)

    # ── Schema Retrieval Agent Output ────────────────────
    relevant_schema: str               # Formatted schema context for LLM
    relevant_tables: list[str]         # Table names retrieved
    retrieval_source: str              # "rag_top_k:{n}" | "full_schema_fallback" | "meta_query" — for tracing

    # ── SQL Generation Agent Output ──────────────────────
    generated_sql: str                 # The SQL query
    sql_explanation: str               # Technical explanation
    friendly_message: str              # User-friendly message

    # ── SQL Validation Agent Output ──────────────────────
    is_valid: bool                     # Safety check passed
    validation_errors: list[str]       # List of issues found
    sanitized_sql: str                 # SQL after safety modifications (LIMIT injection, etc.)
    retry_count: int                   # Number of regeneration attempts

    # ── Output Guardrail Results ─────────────────────────
    guardrail_warnings: list[str]      # Hallucinated reference warnings
    guardrail_confidence: float        # LLM output confidence score (0.0-1.0)

    # ── Execution Agent Output ───────────────────────────
    query_results: list[dict]          # Raw query results
    execution_time_ms: float           # Query execution duration
    row_count: int                     # Number of rows returned
    column_names: list[str]            # Column names in result set

    # ── Visualization Agent Output ───────────────────────
    chart_config: Optional[dict]       # Chart.js configuration
    chart_type: Optional[str]          # bar, line, pie, doughnut, scatter
    insights: list[str]                # Auto-generated insights
    follow_up_questions: list[str]     # Suggested follow-up queries

    # ── Error Handling ───────────────────────────────────
    error: Optional[str]               # Error message if any step failed
    error_agent: Optional[str]         # Which agent produced the error

    # ── Deterministic Evaluation (Phase 8) ───────────────
    eval_mode: bool                    # When True, enforces deterministic temperature & seed
    eval_temperature: float            # Deterministic temperature (e.g. 0.0)
    eval_seed: Optional[int]           # Seed for reproducible generation (e.g. 42)
    pinned_provider: Optional[str]     # Pin LLM provider (e.g. "groq") without fallback
    selected_few_shots: list[str]      # Selected few-shot example IDs/questions used

    # ── Business Semantic Layer (Phase 9) ────────────────
    semantic_context: Optional[str]        # Contextual semantic disambiguation guidance for SQL generation
    semantic_annotations: Optional[dict]   # Structured semantic mapping metadata

    # ── Interactive Query Disambiguation (Phase 10) ─────
    ambiguities: Optional[list[dict]]          # Detected semantic ambiguities
    active_clarification: Optional[dict]       # Pending clarification object when required
    clarification_resolved: Optional[bool]     # True if ambiguity was answered by user
    clarification_response: Optional[str]      # User's raw answer to clarification prompt
    selected_candidate: Optional[dict]         # Resolved candidate chosen by user or system
    semantic_assumptions: Optional[list[dict]] # Tracked assumptions when auto-executing
    requires_clarification: bool               # True if execution is paused pending user input



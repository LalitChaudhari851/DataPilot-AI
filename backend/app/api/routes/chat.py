"""
Chat & Query Routes — Core API endpoints for text-to-SQL.
Includes SSE streaming for real-time pipeline feedback.
"""

import json
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from app.api.schemas import (
    GenerateSQLRequest, QueryResult,
    ExecuteQueryRequest,
    ExplainRequest, ExplainResponse,
    InsightsRequest, InsightsResponse,
)

router = APIRouter(prefix="/api/v1", tags=["Query"])


def create_chat_router(orchestrator, auth_dep, cache, rate_limiter, tracer, explainer, insights_gen, anomaly_detector, safety_validator):
    """Factory to create chat router with injected dependencies."""

    @router.post("/generate-sql", response_model=QueryResult)
    def generate_sql(request: GenerateSQLRequest, current_user: dict = Depends(auth_dep)):
        """
        Generate SQL from natural language and optionally execute it.
        The full multi-agent pipeline runs here.
        """
        # Rate limiting
        user_key = f"rl:{current_user.get('sub', 'anon')}"
        if not rate_limiter.check(user_key):
            raise HTTPException(429, "Rate limit exceeded. Please wait a moment.")

        # Check cache
        cached = cache.get(request.question, current_user.get("tenant_id", "default"))
        if cached:
            cached["trace_id"] = "cached"
            return QueryResult(**cached)

        # Run multi-agent pipeline
        result = orchestrator.process_query(
            user_query=request.question,
            conversation_history=request.history,
            tenant_id=current_user.get("tenant_id", "default"),
            user_role=current_user.get("role", "viewer"),
            clarification_response=request.clarification_response,
            active_clarification=request.active_clarification,
        )

        # Trace the query
        tracer.trace_query(result)

        # Build response
        response_data = {
            "trace_id": result.get("trace_id", ""),
            "question": request.question,
            "intent": result.get("intent"),
            "sql": result.get("sanitized_sql") or result.get("generated_sql"),
            "sql_explanation": result.get("sql_explanation"),
            "message": result.get("friendly_message", ""),
            "data": result.get("query_results", []),
            "row_count": result.get("row_count", 0),
            "column_names": result.get("column_names", []),
            "execution_time_ms": result.get("execution_time_ms", 0),
            "chart_config": result.get("chart_config"),
            "chart_type": result.get("chart_type"),
            "insights": result.get("insights", []),
            "follow_ups": result.get("follow_up_questions", []),
            "error": result.get("error"),
            "requires_clarification": result.get("requires_clarification", False),
            "clarification": result.get("active_clarification"),
            "semantic_assumptions": result.get("semantic_assumptions", []),
        }

        # Cache successful results
        if not result.get("error") and result.get("query_results") and not result.get("requires_clarification"):
            cache.set(request.question, response_data, current_user.get("tenant_id", "default"))

        return QueryResult(**response_data)

    @router.post("/chat/stream")
    async def chat_stream(request: GenerateSQLRequest, current_user: dict = Depends(auth_dep)):
        """
        Stream chat responses via Server-Sent Events (SSE).
        Each agent stage emits an event as it completes.
        """
        user_key = f"rl:{current_user.get('sub', 'anon')}"
        if not rate_limiter.check(user_key):
            raise HTTPException(429, "Rate limit exceeded.")

        async def event_generator():
            async for event in orchestrator.aprocess_query_streaming(
                user_query=request.question,
                conversation_history=request.history,
                tenant_id=current_user.get("tenant_id", "default"),
                user_role=current_user.get("role", "viewer"),
                clarification_response=request.clarification_response,
                active_clarification=request.active_clarification,
            ):
                event_type = event.get("type", "message")
                yield _sse_event(event_type, event)


        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post("/execute-query", response_model=QueryResult)
    def execute_query(request: ExecuteQueryRequest, current_user: dict = Depends(auth_dep)):
        """Execute a user-provided SQL query (must pass safety validation)."""
        from app.agents.sql_validation import sql_validation_node

        # Validate the SQL
        validation_state = {"generated_sql": request.sql, "retry_count": 0, "trace_id": "manual"}
        validation_result = sql_validation_node(validation_state)

        if not validation_result.get("is_valid"):
            errors = validation_result.get("validation_errors", ["Unknown validation error"])
            raise HTTPException(400, f"SQL blocked by safety layer: {', '.join(errors)}")

        # Execute
        from app.agents.execution import execution_node
        exec_state = {**validation_result, "trace_id": "manual"}
        exec_result = execution_node(exec_state, orchestrator.db_pool)

        if exec_result.get("error"):
            raise HTTPException(400, exec_result["error"])

        return QueryResult(
            trace_id="manual",
            question="Manual SQL execution",
            sql=request.sql,
            message=f"Query executed successfully. {exec_result.get('row_count', 0)} rows returned.",
            data=exec_result.get("query_results", []),
            row_count=exec_result.get("row_count", 0),
            column_names=exec_result.get("column_names", []),
            execution_time_ms=exec_result.get("execution_time_ms", 0),
        )

    @router.post("/explain", response_model=ExplainResponse)
    def explain_sql(request: ExplainRequest, current_user: dict = Depends(auth_dep)):
        """Explain a SQL query in natural language."""
        explanation = explainer.explain(request.sql, request.result_count)
        return ExplainResponse(sql=request.sql, explanation=explanation)

    @router.post("/insights", response_model=InsightsResponse)
    def get_insights(request: InsightsRequest, current_user: dict = Depends(auth_dep)):
        """Generate auto-insights and anomaly detection for data."""
        insights = insights_gen.generate(request.data, request.query)
        anomalies = anomaly_detector.detect(request.data)
        return InsightsResponse(insights=insights, anomalies=anomalies)

    return router


def _sse_event(event_type: str, data: dict) -> str:
    """Format a Server-Sent Event."""
    payload = json.dumps({"type": event_type, **data}, default=str)
    return f"event: {event_type}\ndata: {payload}\n\n"

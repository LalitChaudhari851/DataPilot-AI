"""
LangGraph Orchestrator — Wires all 7 agents into a DAG with conditional routing.
This is the core of the multi-agent architecture.
Includes pipeline timeout protection, output guardrails, and graceful degradation.
"""

import uuid
import time
import asyncio
import threading
import structlog
from langgraph.graph import StateGraph, END

from app.agents.state import AgentState
from app.agents.query_understanding import query_understanding_node
from app.agents.schema_retrieval import schema_retrieval_node
from app.agents.sql_generation import sql_generation_node
from app.agents.sql_validation import sql_validation_node, route_validation
from app.agents.execution import execution_node
from app.agents.visualization import visualization_node
from app.agents.result_summary import result_summary_node
from app.agents.guardrails import OutputGuardrail
from app.db.registry import DatabaseRegistry, get_database_registry, resolve_database

logger = structlog.get_logger()

# Maximum time (seconds) the full pipeline is allowed to run
PIPELINE_TIMEOUT_SECONDS = 60


class AgentOrchestrator:
    """
    Multi-agent orchestrator using LangGraph StateGraph.
    
    Flow:
        understand_query → [chat? → END]
                         → retrieve_schema → generate_sql → validate_sql
                         → [valid? → execute → visualize → END]
                         → [invalid? → retry generate_sql (max 3)]
                         → [blocked? → END with error]
    """

    def __init__(self, llm_router, rag_retriever, db_pool, registry: DatabaseRegistry | None = None):
        self.llm_router = llm_router
        self.rag_retriever = rag_retriever
        self.db_pool = db_pool
        self.registry = registry or get_database_registry()
        if self.db_pool is not None and not self.registry.has_database("default"):
            self.registry.register("default", self.db_pool, dialect="mysql")

        # ── Cache OutputGuardrail per database pool instance ──
        self._guardrails: dict[int, OutputGuardrail] = {}
        self.guardrail = self._get_guardrail(db_pool)

        # ── Initialize Semantic Cache ──
        self.semantic_cache = self._init_semantic_cache()

        self.graph = self._build_graph()

    def _get_guardrail(self, pool) -> OutputGuardrail:
        """Get or lazily initialize OutputGuardrail for a given pool."""
        if pool is None:
            return OutputGuardrail()
        pool_key = id(pool)
        if pool_key not in self._guardrails:
            self._guardrails[pool_key] = self._init_guardrail(pool)
        return self._guardrails[pool_key]

    def _resolve_pool_and_dialect(self, state: AgentState):
        """
        Resolve (db_id, sql_dialect, pool) from state using DatabaseRegistry as source of truth.
        """
        db_id = state.get("db_id")
        resolved_id, dialect, pool = resolve_database(db_id, default_pool=self.db_pool, registry=self.registry)
        state["db_id"] = resolved_id
        state["sql_dialect"] = dialect
        return resolved_id, dialect, pool

    @staticmethod
    def _init_semantic_cache():
        """Initialize the semantic cache for query deduplication."""
        try:
            from app.cache.semantic_cache import SemanticCache
            cache = SemanticCache(similarity_threshold=0.95, ttl_seconds=300)
            if cache.available:
                logger.info("semantic_cache_initialized")
                return cache
            logger.info("semantic_cache_disabled", reason="encoder_unavailable")
        except Exception as e:
            logger.info("semantic_cache_disabled", reason=str(e))
        return None

    @staticmethod
    def _init_guardrail(db_pool) -> OutputGuardrail:
        """Build the OutputGuardrail from the live database schema."""
        try:
            tables = db_pool.get_tables()
            known_columns = {}
            for table in tables:
                cols = db_pool.get_table_schema(table)
                known_columns[table] = {c["name"] for c in cols}
            guardrail = OutputGuardrail(
                known_tables=set(tables),
                known_columns=known_columns,
            )
            logger.info("guardrail_initialized", tables=len(tables))
            return guardrail
        except Exception as e:
            logger.warning("guardrail_init_failed", error=str(e))
            return OutputGuardrail()

    def _build_graph(self) -> StateGraph:
        """Construct the LangGraph agent pipeline."""
        graph = StateGraph(AgentState)

        # ── Register agent nodes ─────────────────────────
        graph.add_node("understand_query", self._understand_query)
        graph.add_node("handle_chat", self._handle_chat)
        graph.add_node("handle_meta", self._handle_meta)
        graph.add_node("retrieve_schema", self._retrieve_schema)
        graph.add_node("generate_sql", self._generate_sql)
        graph.add_node("guardrail_check", self._guardrail_check)
        graph.add_node("validate_sql", self._validate_sql)
        graph.add_node("execute_query", self._execute_query)
        graph.add_node("ground_summary", self._ground_summary)
        graph.add_node("visualize", self._visualize)
        graph.add_node("handle_blocked", self._handle_blocked)
        graph.add_node("handle_clarification", self._handle_clarification)

        # ── Entry point ──────────────────────────────────
        graph.set_entry_point("understand_query")

        # ── Conditional routing after intent classification ──
        graph.add_conditional_edges(
            "understand_query",
            self._route_by_intent,
            {
                "chat": "handle_chat",
                "ambiguous": "handle_chat",
                "meta_query": "retrieve_schema",
                "data_query": "retrieve_schema",
                "aggregation": "retrieve_schema",
                "comparison": "retrieve_schema",
                "explanation": "retrieve_schema",
            },
        )

        # ── Linear flow ─────────────────────────────────
        graph.add_conditional_edges(
            "retrieve_schema",
            self._route_after_schema,
            {
                "meta_query": "handle_meta",
                "clarification": "handle_clarification",
                "sql": "generate_sql",
            },
        )
        graph.add_edge("generate_sql", "guardrail_check")
        graph.add_edge("guardrail_check", "validate_sql")

        # ── Conditional routing after validation ─────────
        graph.add_conditional_edges(
            "validate_sql",
            route_validation,
            {
                "valid": "execute_query",
                "retry": "generate_sql",
                "blocked": "handle_blocked",
            },
        )

        # ── Conditional routing after execution (self-repair) ───
        graph.add_conditional_edges(
            "execute_query",
            self._route_after_execution,
            {
                "continue": "ground_summary",
                "retry": "generate_sql",
            },
        )

        graph.add_edge("ground_summary", "visualize")

        # ── Terminal nodes ───────────────────────────────
        graph.add_edge("visualize", END)
        graph.add_edge("handle_chat", END)
        graph.add_edge("handle_meta", END)
        graph.add_edge("handle_blocked", END)
        graph.add_edge("handle_clarification", END)

        return graph.compile()

    @staticmethod
    def _route_after_execution(state: AgentState) -> str:
        """
        Self-repair router after execution.
        If execution failed with a database error and retry_count < 3,
        routes back to generate_sql for LLM correction.
        """
        has_error = bool(state.get("error")) and state.get("error_agent") == "execution"
        retry_count = state.get("retry_count", 0)
        if has_error and retry_count < 3:
            logger.info("execution_failure_triggers_retry", retry_count=retry_count, error=state.get("error"))
            return "retry"
        return "continue"

    # ── Node Wrappers (inject dependencies + error isolation) ──

    def _understand_query(self, state: AgentState) -> dict:
        return self._safe_execute("query_understanding", query_understanding_node, state, self.llm_router)

    def _retrieve_schema(self, state: AgentState) -> dict:
        _, _, pool = self._resolve_pool_and_dialect(state)
        return self._safe_execute("schema_retrieval", schema_retrieval_node, state, self.rag_retriever, pool)

    def _generate_sql(self, state: AgentState) -> dict:
        self._resolve_pool_and_dialect(state)
        return self._safe_execute("sql_generation", sql_generation_node, state, self.llm_router)

    def _guardrail_check(self, state: AgentState) -> dict:
        """Run output guardrails to catch hallucinated table/column references."""
        return self._safe_execute("guardrail_check", self._run_guardrail, state)

    def _run_guardrail(self, state: AgentState) -> dict:
        """Execute guardrail validation on the generated SQL."""
        sql = state.get("generated_sql", "")
        if not sql:
            return {}

        _, _, pool = self._resolve_pool_and_dialect(state)
        guardrail = self._get_guardrail(pool)

        warnings = guardrail.validate_sql_references(sql)
        confidence = guardrail.score_confidence(sql)

        if warnings:
            logger.warning(
                "guardrail_warnings",
                trace_id=state.get("trace_id"),
                warnings=warnings,
                confidence=confidence,
            )

        # If many hallucinations are detected, mark as invalid.
        # Do NOT bump retry_count here — only sql_validation_node manages
        # the retry counter. Double-counting caused infinite loops.
        if len(warnings) >= 3:
            return {
                "is_valid": False,
                "validation_errors": [f"Schema grounding failed: {w}" for w in warnings],
            }

        return {
            "guardrail_warnings": warnings,
            "guardrail_confidence": confidence,
        }

    def _validate_sql(self, state: AgentState) -> dict:
        return self._safe_execute("sql_validation", sql_validation_node, state)

    def _execute_query(self, state: AgentState) -> dict:
        _, _, pool = self._resolve_pool_and_dialect(state)
        return self._safe_execute("execution", execution_node, state, pool)

    def _visualize(self, state: AgentState) -> dict:
        return self._safe_execute("visualization", visualization_node, state)

    def _ground_summary(self, state: AgentState) -> dict:
        """Replace the LLM's pre-execution hallucinated summary with a grounded one."""
        return self._safe_execute("result_summary", result_summary_node, state, self.llm_router)

    def _safe_execute(self, agent_name: str, func, *args) -> dict:
        """
        Wrapper that catches per-agent exceptions for graceful degradation.
        Records per-agent latency metrics and OpenTelemetry spans.
        Non-critical agents (visualization) failing won't crash the pipeline.
        """
        from app.observability.metrics import metrics
        from app.observability.tracing import trace_span

        trace_id = args[0].get("trace_id", "unknown") if args else "unknown"

        with trace_span(f"agent.{agent_name}", {"trace_id": trace_id, "agent": agent_name}):
            start = time.perf_counter()
            try:
                result = func(*args)
                elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
                metrics.observe("plainsql_agent_latency_ms", elapsed_ms, {"agent": agent_name})
                logger.info("agent_completed", agent=agent_name, elapsed_ms=elapsed_ms,
                            trace_id=trace_id)
                return result
            except Exception as e:
                elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
                metrics.observe("plainsql_agent_latency_ms", elapsed_ms, {"agent": agent_name})
                metrics.increment("plainsql_agent_errors_total", {"agent": agent_name})
                logger.error(
                    "agent_failed",
                    agent=agent_name,
                    error=str(e),
                    elapsed_ms=elapsed_ms,
                    trace_id=trace_id,
                )
                # For non-critical agents, return empty results
                non_critical = {"visualization", "result_summary"}
                if agent_name in non_critical:
                    return {
                        "chart_config": None,
                        "chart_type": None,
                        "insights": [f"Visualization skipped due to error: {str(e)[:80]}"],
                        "follow_up_questions": [],
                    }
                # For critical agents, propagate the error state.
                # MUST preserve retry_count to prevent infinite loops
                # when _safe_execute catches an exception.
                return {
                    "error": f"{agent_name} failed: {str(e)}",
                    "error_agent": agent_name,
                    "retry_count": args[0].get("retry_count", 0) if args else 0,
                }

    def _handle_chat(self, state: AgentState) -> dict:
        """Terminal node for conversational responses."""
        return {
            "friendly_message": state.get(
                "friendly_message",
                "Hello. I can help you query your database in plain English.",
            ),
            "query_results": [],
            "row_count": 0,
            "follow_up_questions": [
                "Show top 5 employees by salary",
                "Total sales revenue by region",
                "List all products with low stock",
            ],
        }

    def _handle_meta(self, state: AgentState) -> dict:
        """Terminal node for schema/meta queries."""
        schema = state.get("relevant_schema", "")
        tables = state.get("relevant_tables", [])
        
        # Format schema info as friendly message
        table_list = ", ".join(tables) if tables else "No tables found"
        return {
            "friendly_message": f"Your database contains these tables: **{table_list}**\n\n```\n{schema}\n```",
            "query_results": [],
            "row_count": 0,
            "follow_up_questions": [f"Show data from {t}" for t in tables[:3]],
        }

    def _handle_blocked(self, state: AgentState) -> dict:
        """Terminal node when SQL validation fails after max retries."""
        errors = state.get("validation_errors", [])
        return {
            "error": "Query blocked by safety layer",
            "error_agent": "sql_validation",
            "friendly_message": (
                "🛡️ **Security Alert**: Your query was blocked by the safety system.\n\n"
                f"Reasons: {', '.join(errors)}\n\n"
                "I can only perform safe, read-only (SELECT) operations."
            ),
            "query_results": [],
            "query_results": [],
            "row_count": 0,
        }

    def _handle_clarification(self, state: AgentState) -> dict:
        """Terminal node when clarification is required before SQL generation."""
        active = state.get("active_clarification") or {}
        candidates = active.get("candidates", [])
        follow_ups = [c.get("label", "") for c in candidates]
        return {
            "friendly_message": state.get(
                "friendly_message",
                "Could you clarify your request?",
            ),
            "query_results": [],
            "row_count": 0,
            "requires_clarification": True,
            "active_clarification": active,
            "follow_up_questions": follow_ups,
        }

    # ── Routing Functions ────────────────────────────────

    @staticmethod
    def _route_by_intent(state: AgentState) -> str:
        """Route to appropriate handler based on classified intent."""
        route_intent = state.get("route_intent", state.get("intent", "data_query"))
        valid_routes = {"chat", "ambiguous", "meta_query", "data_query", "aggregation", "comparison", "explanation"}
        return route_intent if route_intent in valid_routes else "data_query"

    @staticmethod
    def _route_after_schema(state: AgentState) -> str:
        """Route after schema retrieval: clarification, meta query, or SQL generation."""
        if state.get("requires_clarification"):
            return "clarification"
        if state.get("route_intent") == "meta_query":
            return "meta_query"
        return "sql"

    # ── Public API ───────────────────────────────────────

    def process_query(
        self,
        user_query: str,
        conversation_history: list[dict] = None,
        tenant_id: str = "default",
        user_role: str = "analyst",
        db_id: str | None = None,
        sql_dialect: str | None = None,
        clarification_response: str | None = None,
        active_clarification: dict | None = None,
    ) -> AgentState:
        """
        Process a natural language query through the full agent pipeline (sync).
        Returns the final AgentState with all results.
        Enforces a pipeline-level timeout to prevent runaway processing.
        """
        trace_id = str(uuid.uuid4())[:8]

        # Resolve database and dialect
        resolved_db_id, resolved_dialect, _ = resolve_database(
            db_id, default_pool=self.db_pool, registry=self.registry
        )

        initial_state: AgentState = {
            "user_query": user_query,
            "conversation_history": conversation_history or [],
            "tenant_id": tenant_id,
            "user_role": user_role,
            "trace_id": trace_id,
            "retry_count": 0,
            "validation_errors": [],
            "db_id": resolved_db_id,
            "sql_dialect": resolved_dialect,
        }
        if clarification_response:
            initial_state["clarification_response"] = clarification_response
        if active_clarification:
            initial_state["active_clarification"] = active_clarification

        logger.info(
            "pipeline_started",
            trace_id=trace_id,
            query=user_query,
            tenant_id=tenant_id,
        )

        # ── Semantic cache check ─────────────────────────
        if self.semantic_cache:
            cached = self.semantic_cache.get(user_query, tenant_id=tenant_id)
            if cached:
                cached["trace_id"] = trace_id
                cached["cache_hit"] = True
                logger.info("semantic_cache_hit", trace_id=trace_id, query=user_query[:60])
                return cached

        start_time = time.perf_counter()

        # ── Real timeout: interrupt the blocking thread after deadline ──
        # threading.Timer fires on the calling thread and raises TimeoutError,
        # which propagates out of graph.invoke() without leaving zombie threads.
        timeout_fired = threading.Event()

        def _timeout_interrupt():
            timeout_fired.set()
            # Raise into the calling thread via ctypes — safely interrupts
            # the blocking LangGraph call.
            import ctypes
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(threading.main_thread().ident),
                ctypes.py_object(TimeoutError),
            )

        timer = threading.Timer(PIPELINE_TIMEOUT_SECONDS, _timeout_interrupt)
        timer.daemon = True
        timer.start()

        try:
            final_state = self.graph.invoke(
                initial_state,
                config={"recursion_limit": 50},
            )
            timer.cancel()  # Disarm if pipeline completed in time

            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            final_state["execution_time_ms"] = elapsed_ms

            # Cache successful data query results
            if (
                self.semantic_cache
                and not final_state.get("error")
                and final_state.get("query_results")
            ):
                self.semantic_cache.set(user_query, dict(final_state), tenant_id=tenant_id)

            logger.info(
                "pipeline_completed",
                trace_id=trace_id,
                total_time_ms=elapsed_ms,
                intent=final_state.get("intent"),
                row_count=final_state.get("row_count", 0),
                has_error=bool(final_state.get("error")),
            )
            return final_state

        except TimeoutError:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.error(
                "pipeline_timeout",
                trace_id=trace_id,
                elapsed_ms=elapsed_ms,
                timeout_seconds=PIPELINE_TIMEOUT_SECONDS,
            )
            return {
                **initial_state,
                "error": f"Pipeline timed out after {PIPELINE_TIMEOUT_SECONDS}s",
                "error_agent": "orchestrator",
                "friendly_message": "The query took too long to process. Try a simpler question.",
                "query_results": [],
                "row_count": 0,
                "execution_time_ms": elapsed_ms,
            }

        except Exception as e:
            timer.cancel()
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.error("pipeline_failed", trace_id=trace_id, error=str(e), elapsed_ms=elapsed_ms)
            return {
                **initial_state,
                "error": f"Pipeline error: {str(e)}",
                "error_agent": "orchestrator",
                "friendly_message": "An unexpected error occurred. Please try again.",
                "query_results": [],
                "row_count": 0,
                "execution_time_ms": elapsed_ms,
            }

    async def aprocess_query(
        self,
        user_query: str,
        conversation_history: list[dict] = None,
        tenant_id: str = "default",
        user_role: str = "analyst",
        db_id: str | None = None,
        sql_dialect: str | None = None,
    ) -> AgentState:
        """
        Async version of process_query.
        Runs the synchronous LangGraph pipeline in a thread pool
        to avoid blocking the FastAPI event loop.
        asyncio.wait_for() enforces a hard deadline: if the thread
        doesn't complete within PIPELINE_TIMEOUT_SECONDS the coroutine
        is cancelled and a timeout error state is returned.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self.process_query,
                    user_query=user_query,
                    conversation_history=conversation_history,
                    tenant_id=tenant_id,
                    user_role=user_role,
                    db_id=db_id,
                    sql_dialect=sql_dialect,
                ),
                timeout=PIPELINE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error(
                "async_pipeline_timeout",
                query=user_query[:80],
                timeout_seconds=PIPELINE_TIMEOUT_SECONDS,
            )
            return {
                "user_query": user_query,
                "error": f"Pipeline timed out after {PIPELINE_TIMEOUT_SECONDS}s",
                "error_agent": "orchestrator",
                "friendly_message": "The query took too long to process. Try a simpler question.",
                "query_results": [],
                "row_count": 0,
                "execution_time_ms": PIPELINE_TIMEOUT_SECONDS * 1000.0,
            }

    async def aprocess_query_parallel(
        self,
        user_query: str,
        conversation_history: list[dict] = None,
        tenant_id: str = "default",
        user_role: str = "analyst",
        db_id: str | None = None,
        sql_dialect: str | None = None,
    ) -> AgentState:
        """
        Optimized async pipeline that runs independent stages concurrently.

        Instead of the sequential flow:
            intent (200ms) → schema (400ms) → generate (800ms)

        This runs:
            intent + schema (400ms concurrent) → generate (800ms)

        Saving ~200ms per query by overlapping independent stages.

        If the intent is 'chat', the schema retrieval result is discarded.
        Falls back to the sequential aprocess_query on any error.
        """
        trace_id = str(uuid.uuid4())[:8]

        try:
            start_time = time.perf_counter()

            # Semantic cache check
            if self.semantic_cache:
                cached = self.semantic_cache.get(user_query, tenant_id=tenant_id)
                if cached:
                    cached["trace_id"] = trace_id
                    cached["cache_hit"] = True
                    logger.info("parallel_cache_hit", trace_id=trace_id)
                    return cached

            resolved_db_id, resolved_dialect, resolved_pool = resolve_database(
                db_id, default_pool=self.db_pool, registry=self.registry
            )

            initial_state: AgentState = {
                "user_query": user_query,
                "conversation_history": conversation_history or [],
                "tenant_id": tenant_id,
                "user_role": user_role,
                "trace_id": trace_id,
                "retry_count": 0,
                "validation_errors": [],
                "db_id": resolved_db_id,
                "sql_dialect": resolved_dialect,
            }

            # ── Run intent + schema retrieval in parallel ──
            intent_task = asyncio.to_thread(
                query_understanding_node, initial_state, self.llm_router
            )
            schema_task = asyncio.to_thread(
                schema_retrieval_node, initial_state, self.rag_retriever, resolved_pool
            )

            # Both start immediately; gather waits for both
            intent_result, schema_result = await asyncio.wait_for(
                asyncio.gather(intent_task, schema_task),
                timeout=PIPELINE_TIMEOUT_SECONDS,
            )

            # Merge results
            initial_state.update(intent_result)
            intent = initial_state.get("intent", "data_query")

            # For chat/ambiguous intents, skip SQL pipeline entirely
            if intent in ("chat", "ambiguous"):
                initial_state.update(
                    self._safe_execute("handle_chat", self._handle_chat, initial_state)
                )
                elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
                initial_state["execution_time_ms"] = elapsed_ms
                return initial_state

            # For data queries, use schema results and continue with full pipeline
            initial_state.update(schema_result)

            # Fall back to sequential pipeline for the remaining stages
            # (generate → guardrail → validate → execute → summarize → visualize)
            # This ensures circuit breakers, retry loops, and guardrails all run correctly
            return await self.aprocess_query(
                user_query=user_query,
                conversation_history=conversation_history,
                tenant_id=tenant_id,
                user_role=user_role,
            )

        except asyncio.TimeoutError:
            logger.error("parallel_pipeline_timeout", trace_id=trace_id)
            return {
                "user_query": user_query,
                "trace_id": trace_id,
                "error": f"Pipeline timed out after {PIPELINE_TIMEOUT_SECONDS}s",
                "error_agent": "orchestrator",
                "friendly_message": "The query took too long. Try a simpler question.",
                "query_results": [],
                "row_count": 0,
                "execution_time_ms": PIPELINE_TIMEOUT_SECONDS * 1000.0,
            }

        except Exception as e:
            # Fallback to sequential pipeline
            logger.warning("parallel_pipeline_fallback", error=str(e), trace_id=trace_id)
            return await self.aprocess_query(
                user_query=user_query,
                conversation_history=conversation_history,
                tenant_id=tenant_id,
                user_role=user_role,
                db_id=db_id,
                sql_dialect=sql_dialect,
            )

    # ── Progressive Streaming Pipeline ───────────────────────

    async def aprocess_query_streaming(
        self,
        user_query: str,
        conversation_history: list[dict] = None,
        tenant_id: str = "default",
        user_role: str = "analyst",
        db_id: str | None = None,
        sql_dialect: str | None = None,
        clarification_response: str | None = None,
        active_clarification: dict | None = None,
    ):
        """
        Async generator that yields SSE-ready event dicts after each pipeline stage.

        Instead of running the full LangGraph graph.invoke() and returning everything
        at once, this method manually walks the pipeline nodes and yields intermediate
        state as each stage completes. This allows the SSE endpoint to progressively
        stream intent, schema context, SQL, data results, and summary tokens to the
        frontend — eliminating 5+ seconds of dead air.

        The LangGraph DAG (self.graph) is NOT used here — this is a parallel code path
        optimized for real-time streaming. The DAG remains for batch/evaluation callers.

        Yields dicts with a 'type' key:
            - {'type': 'intent', 'intent': ..., 'complexity': ...}
            - {'type': 'stage', 'stage': 'retrieval', 'message': ...}
            - {'type': 'clarification_required', 'clarification': ..., 'message': ...}
            - {'type': 'sql', 'sql': ..., 'explanation': ...}
            - {'type': 'results', 'data': [...], 'row_count': ..., ...}
            - {'type': 'summary_token', 'token': ...}
            - {'type': 'message', 'message': ..., 'insights': [...], ...}
            - {'type': 'done', 'total_time_ms': ...}
        """
        from app.agents.query_understanding import query_understanding_node
        from app.agents.schema_retrieval import schema_retrieval_node
        from app.agents.sql_generation import sql_generation_node
        from app.agents.sql_validation import sql_validation_node, route_validation
        from app.agents.execution import execution_node
        from app.agents.visualization import visualization_node

        trace_id = str(uuid.uuid4())[:8]
        start_time = time.perf_counter()

        resolved_db_id, resolved_dialect, resolved_pool = resolve_database(
            db_id, default_pool=self.db_pool, registry=self.registry
        )

        state: AgentState = {
            "user_query": user_query,
            "conversation_history": conversation_history or [],
            "tenant_id": tenant_id,
            "user_role": user_role,
            "trace_id": trace_id,
            "retry_count": 0,
            "validation_errors": [],
            "db_id": resolved_db_id,
            "sql_dialect": resolved_dialect,
        }
        if clarification_response:
            state["clarification_response"] = clarification_response
        if active_clarification:
            state["active_clarification"] = active_clarification

        logger.info("streaming_pipeline_started", trace_id=trace_id, query=user_query[:80])

        # ── Semantic cache check ─────────────────────────
        if self.semantic_cache:
            cached = self.semantic_cache.get(user_query, tenant_id=tenant_id)
            if cached:
                cached["trace_id"] = trace_id
                cached["cache_hit"] = True
                logger.info("streaming_cache_hit", trace_id=trace_id)
                yield {"type": "stage", "stage": "cache_hit", "message": "Retrieved from cache..."}
                yield {"type": "intent", "intent": cached.get("intent", ""), "complexity": cached.get("complexity", "")}
                sql = cached.get("sanitized_sql") or cached.get("generated_sql", "")
                if sql:
                    yield {"type": "sql", "sql": sql, "explanation": cached.get("sql_explanation", "")}
                yield {
                    "type": "results",
                    "data": cached.get("query_results", [])[:100],
                    "row_count": cached.get("row_count", 0),
                    "execution_time_ms": 0,
                }
                yield {
                    "type": "message",
                    "message": cached.get("friendly_message", ""),
                    "insights": cached.get("insights", []),
                    "follow_ups": cached.get("follow_up_questions", []),
                }
                elapsed = round((time.perf_counter() - start_time) * 1000, 2)
                yield {"type": "done", "total_time_ms": elapsed, "cached": True}
                return

        try:
            # ── Stage 1: Intent Classification (~5ms heuristic) ──
            yield {"type": "stage", "stage": "classifying", "message": "Understanding your question..."}
            intent_result = await asyncio.to_thread(
                query_understanding_node, state, self.llm_router
            )
            state.update(intent_result)

            intent = state.get("intent", "data_query")
            route_intent = state.get("route_intent", intent)

            yield {
                "type": "intent",
                "intent": intent,
                "complexity": state.get("complexity", ""),
                "route_intent": route_intent,
            }

            # ── Chat fast-path ───────────────────────────────
            if intent in ("chat", "ambiguous"):
                chat_result = self._handle_chat(state)
                state.update(chat_result)
                yield {
                    "type": "message",
                    "message": state.get("friendly_message", ""),
                    "insights": [],
                    "follow_ups": state.get("follow_up_questions", []),
                }
                elapsed = round((time.perf_counter() - start_time) * 1000, 2)
                yield {"type": "done", "total_time_ms": elapsed, "chat_mode": True}
                return

            # ── Stage 2: Schema Retrieval (~100ms) ───────────
            yield {"type": "stage", "stage": "retrieving", "message": "Retrieving schema context..."}
            schema_result = await asyncio.to_thread(
                schema_retrieval_node, state, self.rag_retriever, resolved_pool
            )
            state.update(schema_result)

            # Phase 10: Check if clarification is required before proceeding to SQL generation
            if state.get("requires_clarification"):
                active_clarif = state.get("active_clarification") or {}
                yield {
                    "type": "clarification_required",
                    "clarification": active_clarif,
                    "message": state.get("friendly_message", ""),
                }
                yield {
                    "type": "message",
                    "message": state.get("friendly_message", ""),
                    "insights": [],
                    "follow_ups": [c.get("label", "") for c in active_clarif.get("candidates", [])],
                }
                elapsed = round((time.perf_counter() - start_time) * 1000, 2)
                yield {"type": "done", "total_time_ms": elapsed, "clarification_needed": True}
                return

            if state.get("clarification_resolved") and state.get("selected_candidate"):
                yield {
                    "type": "stage",
                    "stage": "clarification_resolved",
                    "message": f"Resolved: Using {state['selected_candidate'].get('label', '')}",
                }

            # Handle meta_query
            if route_intent == "meta_query":
                meta_result = self._handle_meta(state)
                state.update(meta_result)
                yield {
                    "type": "message",
                    "message": state.get("friendly_message", ""),
                    "insights": [],
                    "follow_ups": state.get("follow_up_questions", []),
                }
                elapsed = round((time.perf_counter() - start_time) * 1000, 2)
                yield {"type": "done", "total_time_ms": elapsed}
                return

            # ── Stage 3: SQL Generation (~2s LLM) ────────────
            yield {"type": "stage", "stage": "Generating", "message": "Generating SQL..."}

            max_retries = 3
            for attempt in range(max_retries):
                gen_result = await asyncio.to_thread(
                    sql_generation_node, state, self.llm_router
                )
                state.update(gen_result)

                sql = state.get("generated_sql", "")
                if not sql:
                    break

                # ── Guardrail check ──────────────────────────
                guardrail_result = self._run_guardrail(state)
                state.update(guardrail_result)

                # ── Validation ───────────────────────────────
                yield {"type": "stage", "stage": "Validating", "message": "Validating SQL safety..."}
                val_result = await asyncio.to_thread(sql_validation_node, state)
                state.update(val_result)

                route = route_validation(state)
                if route == "valid":
                    break
                elif route == "blocked":
                    blocked = self._handle_blocked(state)
                    state.update(blocked)
                    yield {
                        "type": "message",
                        "message": state.get("friendly_message", ""),
                        "insights": [],
                        "follow_ups": [],
                    }
                    elapsed = round((time.perf_counter() - start_time) * 1000, 2)
                    yield {"type": "done", "total_time_ms": elapsed, "blocked": True}
                    return
                else:  # retry
                    state["retry_count"] = state.get("retry_count", 0) + 1
                    yield {"type": "stage", "stage": "Generating", "message": f"Regenerating SQL (attempt {attempt + 2})..."}

            # Stream SQL to frontend immediately
            sql = state.get("sanitized_sql") or state.get("generated_sql", "")
            if sql:
                yield {
                    "type": "sql",
                    "sql": sql,
                    "explanation": state.get("sql_explanation", ""),
                }

            # ── Stage 4: Database Execution (~100ms) ─────────
            yield {"type": "stage", "stage": "Executing", "message": "Executing query..."}
            exec_result = await asyncio.to_thread(
                execution_node, state, resolved_pool
            )
            state.update(exec_result)

            # Stream data results IMMEDIATELY — don't wait for summary
            yield {
                "type": "results",
                "data": state.get("query_results", [])[:100],
                "row_count": state.get("row_count", 0),
                "execution_time_ms": round((time.perf_counter() - start_time) * 1000, 2),
            }

            # ── Stage 5: Summary with Token Streaming (~2s, progressive) ──
            yield {"type": "stage", "stage": "Preparing response", "message": "Generating insights..."}

            results = state.get("query_results", [])
            columns = state.get("column_names", [])

            if results and columns and self.llm_router:
                try:
                    from app.agents.result_summary import astream_summary
                    summary_text = ""
                    async for token in astream_summary(state, self.llm_router):
                        summary_text += token
                        yield {"type": "summary_token", "token": token}

                    state["friendly_message"] = summary_text
                except Exception as e:
                    logger.warning("streaming_summary_failed", error=str(e), trace_id=trace_id)
                    # Fallback to deterministic summary
                    from app.agents.result_summary import result_summary_node
                    summary_result = result_summary_node(state, self.llm_router)
                    state.update(summary_result)

            # ── Stage 6: Visualization (non-blocking) ────────
            try:
                viz_result = await asyncio.to_thread(visualization_node, state)
                state.update(viz_result)
            except Exception:
                pass  # Non-critical

            # ── Final message event ──────────────────────────
            yield {
                "type": "message",
                "message": state.get("friendly_message", ""),
                "insights": state.get("insights", []),
                "follow_ups": state.get("follow_up_questions", []),
            }

            elapsed = round((time.perf_counter() - start_time) * 1000, 2)
            state["execution_time_ms"] = elapsed

            # Cache successful results
            if self.semantic_cache and not state.get("error") and state.get("query_results"):
                self.semantic_cache.set(user_query, dict(state), tenant_id=tenant_id)

            yield {"type": "done", "total_time_ms": elapsed}

            logger.info(
                "streaming_pipeline_completed",
                trace_id=trace_id,
                total_time_ms=elapsed,
                intent=intent,
                row_count=state.get("row_count", 0),
            )

        except Exception as e:
            elapsed = round((time.perf_counter() - start_time) * 1000, 2)
            logger.error("streaming_pipeline_failed", trace_id=trace_id, error=str(e), elapsed_ms=elapsed)
            yield {"type": "error", "error": f"Pipeline error: {str(e)[:200]}"}
            yield {"type": "done", "total_time_ms": elapsed, "error": True}

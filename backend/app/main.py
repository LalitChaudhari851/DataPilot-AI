"""
DataPilot Enterprise — FastAPI Application Factory.
Wires all components: agents, LLM router, RAG, auth, observability, and API routes.
Also serves the frontend at / so everything runs from one URL.
"""

import sys
import os
import time
import uuid
import traceback

# Ensure backend is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import structlog

from app.config import get_settings
from app.observability.logger import setup_logging

# ── Conversational fast-path (extracted to app/api/fast_path.py) ──
from app.api.fast_path import detect_conversational as _detect_conversational
from app.startup import ensure_feedback_table as _ensure_feedback_table

# ── Global state ─────────────────────────────────────────
# NOTE: _app_state is written once at startup and read-only during requests.
# Thread-safe for reads under Python GIL. Do NOT mutate during request handling.
_app_state = {}
START_TIME = time.time()

# Path to the frontend — serve the Vite build output (dist/)
_FRONTEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONTEND_DIST = os.path.join(_FRONTEND_ROOT, "frontend", "dist")
# Fall back to legacy frontend if dist hasn't been built yet
FRONTEND_DIR = FRONTEND_DIST if os.path.isdir(FRONTEND_DIST) else os.path.join(_FRONTEND_ROOT, "frontend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    settings = get_settings()

    # ── Setup Logging ────────────────────────────────────
    setup_logging(
        log_level=settings.LOG_LEVEL,
        json_output=(settings.ENV == "production"),
    )
    logger = structlog.get_logger()
    logger.info("startup_begin", app=settings.APP_NAME, version=settings.APP_VERSION, env=settings.ENV)

    try:
        # ── Database ─────────────────────────────────────
        from app.db.connection import DatabasePool
        logger.info("connecting_database")
        db_pool = DatabasePool(
            settings.DB_URI,
            query_timeout=settings.DB_QUERY_TIMEOUT,
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_timeout=settings.DB_POOL_TIMEOUT,
        )
        _app_state["db_pool"] = db_pool
        logger.info("database_connected", tables=len(db_pool.get_tables()))

        # ── LLM Router ───────────────────────────────────
        from app.llm.router import ModelRouter
        logger.info("initializing_llm_router")
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
        _app_state["llm_router"] = llm_router

        # ── Database Registry ────────────────────────────
        from app.db.registry import get_database_registry
        db_registry = get_database_registry()
        if not db_registry.has_database("default"):
            db_registry.register("default", db_pool, dialect="mysql", path="default")
        _app_state["db_registry"] = db_registry

        # ── RAG Retriever ────────────────────────────────
        from app.rag.retriever import HybridRetriever
        logger.info("initializing_rag")
        rag_retriever = HybridRetriever(db_pool, chroma_persist_dir=settings.CHROMA_PERSIST_DIR, registry=db_registry)
        if db_registry.has_database("E_commerce"):
            try:
                ecom_pool = db_registry.get_pool("E_commerce")
                rag_retriever.index_database("E_commerce", db_pool=ecom_pool)
                logger.info("ecommerce_schema_rag_indexed", tables=len(ecom_pool.get_tables()))
            except Exception as e:
                logger.warning("ecommerce_rag_index_warning", error=str(e))
        _app_state["rag_retriever"] = rag_retriever

        # ── Agent Orchestrator ───────────────────────────
        from app.agents.orchestrator import AgentOrchestrator
        logger.info("building_agent_graph")
        orchestrator = AgentOrchestrator(llm_router, rag_retriever, db_pool, registry=db_registry)
        _app_state["orchestrator"] = orchestrator

        # ── Auto-migrate persistence tables ────────────
        _ensure_feedback_table(db_pool)
        from app.db.persistence import ensure_tables, ConversationManager
        ensure_tables(db_pool)
        conversation_manager = ConversationManager(db_pool)
        _app_state["conversation_manager"] = conversation_manager

        # ── Auth Service ─────────────────────────────────
        from app.auth.jwt_auth import AuthService
        auth_service = AuthService(
            secret_key=settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
            expiry_hours=settings.JWT_EXPIRY_HOURS,
        )
        _app_state["auth_service"] = auth_service

        # ── Persistent User Store (MySQL-backed) ─────────
        from app.db.user_repository import UserRepository
        user_repo = UserRepository(db_pool)
        _app_state["user_repo"] = user_repo

        # Seed default users on first startup (idempotent via INSERT IGNORE)
        admin_password = os.environ.get("ADMIN_DEFAULT_PASSWORD", "admin123")
        analyst_password = os.environ.get("ANALYST_DEFAULT_PASSWORD", "analyst123")

        if settings.ENV == "production" and admin_password == "admin123":
            raise ValueError(
                "ADMIN_DEFAULT_PASSWORD must be changed in production. "
                "Set the ADMIN_DEFAULT_PASSWORD environment variable."
            )

        user_repo.create(
            user_id="user_1", username="admin", email="admin@plainsql.io",
            password_hash=auth_service.hash_password(admin_password),
            role="admin", tenant_id="default",
        )
        user_repo.create(
            user_id="user_2", username="analyst", email="analyst@plainsql.io",
            password_hash=auth_service.hash_password(analyst_password),
            role="analyst", tenant_id="default",
        )

        # Backward-compatible dict interface for routes that still use user_store
        user_store = {}
        for u in user_repo.list_users():
            user_store[u["username"]] = u
        _app_state["user_store"] = user_store

        # ── Observability ────────────────────────────────
        from app.observability.tracing import QueryTracer
        tracer = QueryTracer(langsmith_api_key=settings.LANGSMITH_API_KEY, project=settings.LANGSMITH_PROJECT)
        _app_state["tracer"] = tracer

        # ── AI Features ──────────────────────────────────
        from app.ai_features.explainer import SQLExplainer
        from app.ai_features.insights import InsightsGenerator
        from app.ai_features.anomaly import AnomalyDetector
        _app_state["explainer"] = SQLExplainer(llm_router)
        _app_state["insights_gen"] = InsightsGenerator()
        _app_state["anomaly_detector"] = AnomalyDetector()

        # ── Input Validator ──────────────────────────────
        from app.security.input_validator import InputValidator
        _app_state["input_validator"] = InputValidator(max_length=1000)

        # ── Cache & Rate Limiting (Redis with in-memory fallback) ──
        from app.cache.redis_client import create_cache, create_rate_limiter
        from app.api.middleware import create_auth_dependency
        _app_state["rate_limiter"] = create_rate_limiter(
            redis_url=settings.REDIS_URL, rpm=settings.RATE_LIMIT_RPM,
        )
        _app_state["cache"] = create_cache(
            redis_url=settings.REDIS_URL, ttl_seconds=settings.CACHE_TTL_SECONDS,
        )
        _app_state["auth_dep"] = create_auth_dependency(auth_service)

        # ── Register API Routes ──────────────────────────
        from app.api.routes.chat import create_chat_router
        from app.api.routes.system import create_system_router

        chat_router = create_chat_router(
            orchestrator=orchestrator, auth_dep=_app_state["auth_dep"],
            cache=_app_state["cache"], rate_limiter=_app_state["rate_limiter"],
            tracer=tracer, explainer=_app_state["explainer"],
            insights_gen=_app_state["insights_gen"], anomaly_detector=_app_state["anomaly_detector"],
            safety_validator=_app_state["input_validator"],
        )
        app.include_router(chat_router)

        auth_router, schema_router, analytics_router, health_router = create_system_router(
            auth_service=auth_service, auth_dep=_app_state["auth_dep"],
            db_pool=db_pool, rag_retriever=rag_retriever, llm_router=llm_router,
            tracer=tracer, user_repo=user_repo, start_time=START_TIME,
        )
        app.include_router(auth_router)
        app.include_router(schema_router)
        app.include_router(analytics_router)
        app.include_router(health_router)

        # ── Monitoring ───────────────────────────────────
        from app.api.routes.monitoring import create_monitoring_router, get_metrics_collector
        monitoring_router = create_monitoring_router()
        app.include_router(monitoring_router)
        _app_state["metrics_collector"] = get_metrics_collector()

        @app.get("/metrics", tags=["Monitoring"])
        def root_metrics_endpoint():
            """Standard root Prometheus metrics endpoint."""
            from app.observability.metrics import get_prometheus_metrics, get_prometheus_content_type
            from fastapi import Response
            return Response(content=get_prometheus_metrics(), media_type=get_prometheus_content_type())

        # ── Conversations API ─────────────────────────────
        from app.api.routes.conversations import create_conversations_router
        conv_router = create_conversations_router(conversation_manager)
        app.include_router(conv_router)

        # ── Admin API ────────────────────────────────────
        from app.api.routes.admin import create_admin_router
        admin_router = create_admin_router(
            rag_retriever=rag_retriever,
            cache=_app_state["cache"],
            auth_dep=_app_state["auth_dep"],
            db_pool=db_pool,
            llm_router=llm_router,
            orchestrator=orchestrator,
        )
        app.include_router(admin_router)

        # ── Request Deduplicator ──────────────────────────
        from app.security.dedup import RequestDeduplicator
        _app_state["dedup"] = RequestDeduplicator()

        # ── Legacy /chat endpoint ────────────────────────
        is_production = settings.ENV == "production"
        _register_legacy_chat(app, orchestrator, tracer, _app_state["rate_limiter"], _app_state["input_validator"], _app_state["metrics_collector"], conversation_manager, _app_state["dedup"], require_auth=is_production, auth_service=auth_service)

        logger.info("startup_complete",
            providers=llm_router.list_providers(),
            tables=db_pool.get_tables(),
            rag_docs=rag_retriever.collection.count(),
        )

        # ── Startup Smoke Test ────────────────────────────
        from app.startup import run_smoke_test
        run_smoke_test(db_pool, rag_retriever, llm_router)

        yield

    except Exception as e:
        logger.error("startup_failed", error=str(e))
        raise
    finally:
        logger.info("shutdown_complete")


# ── Startup utilities (extracted to app/startup.py) ──


def _register_legacy_chat(app: FastAPI, orchestrator, tracer, rate_limiter, input_validator, metrics_collector, conversation_manager=None, dedup=None, require_auth: bool = False, auth_service=None):
    """Backward-compatible /chat endpoint for the frontend — now async with metrics."""
    from pydantic import BaseModel, Field
    from typing import List, Optional
    import json as json_mod

    class LegacyChatRequest(BaseModel):
        question: str = Field(..., min_length=1, max_length=1000)
        history: Optional[List[dict]] = []
        conversation_id: Optional[str] = None
        db_id: Optional[str] = "default"

    class FeedbackRequest(BaseModel):
        message_id: str = Field(..., min_length=1, max_length=64)
        user_query: str = Field(..., min_length=1, max_length=1000)
        generated_sql: Optional[str] = ""
        rating: str = Field(..., pattern="^(up|down)$")
        comment: Optional[str] = ""

    @app.post("/api/v1/feedback")
    async def submit_feedback(request: FeedbackRequest):
        """Store user feedback on generated SQL for RLHF data collection."""
        try:
            db_pool = _app_state.get("db_pool")
            if not db_pool:
                return JSONResponse(status_code=503, content={"error": "Database unavailable"})
            db_pool._execute_write_internal(
                """INSERT INTO query_feedback (message_id, user_query, generated_sql, rating, comment)
                   VALUES (:p0, :p1, :p2, :p3, :p4)""",
                (request.message_id, request.user_query, request.generated_sql or "", request.rating, request.comment or ""),
            )
            structlog.get_logger().info(
                "feedback_recorded",
                message_id=request.message_id,
                rating=request.rating,
            )
            return {"status": "ok", "message": "Feedback recorded. Thank you!"}
        except Exception as e:
            structlog.get_logger().error("feedback_failed", error=str(e))
            return JSONResponse(status_code=500, content={"error": "Failed to save feedback"})

    @app.post("/chat")
    async def legacy_chat(request: LegacyChatRequest, req: Request):
        # ── Authentication ──
        auth_header = req.headers.get("Authorization", "")
        has_token = auth_header.startswith("Bearer ")

        if has_token:
            try:
                if auth_service:
                    auth_service.verify_token(auth_header[7:])
            except Exception:
                return JSONResponse(
                    status_code=401,
                    content={"error": "Invalid or expired authentication token."},
                )
        elif require_auth:
            # In production, reject unauthenticated requests
            return JSONResponse(
                status_code=401,
                content={"error": "Authentication required. Please log in."},
            )

        # Rate limiting by IP
        client_ip = req.client.host if req.client else "unknown"
        if not rate_limiter.check(f"legacy:{client_ip}"):
            return JSONResponse(
                status_code=429,
                content={"error": "Rate limit exceeded. Please wait a moment."},
            )

        # Input validation
        is_safe, rejection_reason, sanitized = input_validator.validate(request.question)
        if not is_safe:
            return JSONResponse(
                status_code=400,
                content={"error": f"Query blocked: {rejection_reason}"},
            )

        target_db_id = getattr(request, "db_id", None) or "default"
        result = await orchestrator.aprocess_query(
            user_query=sanitized,
            conversation_history=input_validator.sanitize_history(request.history or []),
            db_id=target_db_id,
        )
        tracer.trace_query(result)

        # Record metrics
        metrics_collector.record_query(
            latency_ms=result.get("execution_time_ms", 0),
            intent=result.get("intent", "unknown"),
            success=not bool(result.get("error")),
            error_agent=result.get("error_agent"),
        )

        return {
            "answer": result.get("query_results", []),
            "sql": result.get("sanitized_sql") or result.get("generated_sql", ""),
            "explanation": result.get("sql_explanation", ""),
            "message": result.get("friendly_message", ""),
            "follow_ups": result.get("follow_up_questions", []),
            "insights": result.get("insights", []),
            "intent": result.get("intent", ""),
            "execution_time_ms": result.get("execution_time_ms", 0),
            "row_count": result.get("row_count", 0),
            "chart_config": result.get("chart_config"),
        }

    @app.post("/chat/stream")
    async def legacy_chat_stream(request: LegacyChatRequest, req: Request):
        """SSE streaming endpoint for the frontend."""
        # ── Authentication ──
        auth_header = req.headers.get("Authorization", "")
        has_token = auth_header.startswith("Bearer ")

        if has_token:
            try:
                if auth_service:
                    auth_service.verify_token(auth_header[7:])
            except Exception:
                return JSONResponse(
                    status_code=401,
                    content={"error": "Invalid or expired authentication token."},
                )
        elif require_auth:
            return JSONResponse(
                status_code=401,
                content={"error": "Authentication required. Please log in."},
            )

        client_ip = req.client.host if req.client else "unknown"
        if not rate_limiter.check(f"stream:{client_ip}"):
            return JSONResponse(
                status_code=429,
                content={"error": "Rate limit exceeded."},
            )

        is_safe, rejection_reason, sanitized = input_validator.validate(request.question)
        if not is_safe:
            return JSONResponse(
                status_code=400,
                content={"error": f"Query blocked: {rejection_reason}"},
            )

        # ── Request deduplication ─────────────────────────
        is_new_request = True
        query_hash = ""
        if dedup:
            is_new_request, query_hash = dedup.try_acquire(sanitized)
            if not is_new_request:
                # Another request is already processing this query — wait for it
                dedup_result = dedup.wait_for_result(query_hash)
                if dedup_result:
                    async def dedup_generator():
                        yield f"data: {json_mod.dumps({'type': 'stage', 'stage': 'dedup', 'message': 'Using result from concurrent request...'})}\n\n"
                        yield f"data: {json_mod.dumps(dedup_result, default=str)}\n\n"
                        yield f"data: {json_mod.dumps({'type': 'done', 'total_time_ms': 0, 'deduplicated': True})}\n\n"
                    return StreamingResponse(
                        dedup_generator(),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
                    )

        target_db_id = getattr(request, "db_id", None) or "default"
        # ── Redis cache check ─────────────────────────────
        cache = _app_state.get("cache")
        cached_result = None
        if cache:
            try:
                cached_result = cache.get(sanitized, db_id=target_db_id)
            except Exception:
                pass

        # ── Sanitize conversation history ─────────────────
        safe_history = input_validator.sanitize_history(request.history or [])

        async def event_generator():
            import time as time_mod
            start = time_mod.perf_counter()

            # ── Conversational fast-path (server-side) ────
            fast_response = _detect_conversational(sanitized)
            if fast_response:
                elapsed_ms = round((time_mod.perf_counter() - start) * 1000, 2)
                yield f"data: {json_mod.dumps({'type': 'message', 'message': fast_response, 'insights': [], 'follow_ups': []})}\n\n"
                yield f"data: {json_mod.dumps({'type': 'done', 'total_time_ms': elapsed_ms, 'chat_mode': True})}\n\n"
                structlog.get_logger().info("conversational_fast_path", query=sanitized[:50], elapsed_ms=elapsed_ms)
                if dedup and query_hash:
                    dedup.release(query_hash)
                return

            # ── Cache HIT: return immediately ─────────────
            if cached_result:
                elapsed_ms = round((time_mod.perf_counter() - start) * 1000, 2)
                yield f"data: {json_mod.dumps({'type': 'stage', 'stage': 'cache_hit', 'message': 'Retrieved from cache...'})}\n\n"
                yield f"data: {json_mod.dumps({'type': 'intent', 'intent': cached_result.get('intent', ''), 'complexity': cached_result.get('complexity', '')}, default=str)}\n\n"
                sql = cached_result.get('sql', '')
                if sql:
                    yield f"data: {json_mod.dumps({'type': 'sql', 'sql': sql, 'explanation': cached_result.get('explanation', '')}, default=str)}\n\n"
                yield f"data: {json_mod.dumps({'type': 'results', 'data': cached_result.get('answer', [])[:100], 'row_count': cached_result.get('row_count', 0), 'execution_time_ms': elapsed_ms}, default=str)}\n\n"
                yield f"data: {json_mod.dumps({'type': 'message', 'message': cached_result.get('message', ''), 'insights': cached_result.get('insights', []), 'follow_ups': cached_result.get('follow_ups', [])}, default=str)}\n\n"
                yield f"data: {json_mod.dumps({'type': 'done', 'total_time_ms': elapsed_ms, 'cached': True})}\n\n"
                structlog.get_logger().info("cache_hit_served", query=sanitized[:50], elapsed_ms=elapsed_ms)
                # Release dedup slot
                if dedup and query_hash:
                    dedup.release(query_hash)
                return

            # ── Progressive streaming pipeline ─────────────
            # Uses aprocess_query_streaming() which yields events as each
            # pipeline stage completes, instead of waiting for everything.
            sql = ""
            last_event = {}
            try:
                async for event in orchestrator.aprocess_query_streaming(
                    user_query=sanitized,
                    conversation_history=safe_history,
                    db_id=target_db_id,
                ):
                    last_event = event
                    event_type = event.get("type", "")

                    # Track SQL for caching/persistence
                    if event_type == "sql":
                        sql = event.get("sql", "")

                    # Forward every event to the frontend as SSE
                    yield f"data: {json_mod.dumps(event, default=str)}\n\n"

                # ── Post-pipeline: metrics, cache, persistence ──
                elapsed_ms = last_event.get("total_time_ms", round((time_mod.perf_counter() - start) * 1000, 2))
                has_error = last_event.get("error", False)

                metrics_collector.record_query(
                    latency_ms=elapsed_ms,
                    intent="unknown",
                    success=not has_error,
                    error_agent="pipeline" if has_error else None,
                )

                # Write to Redis cache
                if cache and sql and not has_error:
                    try:
                        cache_payload = {
                            "sql": sql, "explanation": "", "message": "",
                            "answer": [], "intent": "", "complexity": "",
                            "row_count": 0, "insights": [], "follow_ups": [],
                        }
                        cache.set(sanitized, cache_payload, db_id=target_db_id)
                    except Exception:
                        pass

                # Persist messages
                if conversation_manager and request.conversation_id:
                    try:
                        conversation_manager.save_user_message(request.conversation_id, sanitized)
                        conversation_manager.save_assistant_message(
                            conversation_id=request.conversation_id,
                            content="", generated_sql=sql, explanation="",
                            friendly_message="", intent="",
                            execution_time_ms=elapsed_ms, row_count=0, result_data=[],
                        )
                    except Exception:
                        pass

            except Exception as pipeline_err:
                elapsed_ms = round((time_mod.perf_counter() - start) * 1000, 2)
                structlog.get_logger().error(
                    "sse_pipeline_crash",
                    error=str(pipeline_err),
                    query=sanitized[:80],
                    elapsed_ms=elapsed_ms,
                )
                metrics_collector.record_query(
                    latency_ms=elapsed_ms, intent="unknown",
                    success=False, error_agent="pipeline",
                )
                yield f"data: {json_mod.dumps({'type': 'error', 'error': 'An internal error occurred. Please try again.'})}\n\n"
                yield f"data: {json_mod.dumps({'type': 'done', 'total_time_ms': elapsed_ms, 'error': True})}\n\n"

            finally:
                if dedup and query_hash:
                    try:
                        dedup.complete(query_hash, {'type': 'results', 'sql': sql, 'row_count': 0})
                    except Exception:
                        dedup.release(query_hash)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )


def create_app() -> FastAPI:
    settings = get_settings()
    is_production = settings.ENV == "production"
    app = FastAPI(
        title="DataPilot Enterprise API",
        description="DataPilot — Agentic AI Data Analyst API",
        version=settings.APP_VERSION,
        lifespan=lifespan,
        # Disable API docs in production to prevent schema disclosure
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
    )

    # ── CORS — use configured origins, not wildcard ──────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_origin_regex=r"https://.*\.vercel\.app",
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-ID"],
    )

    # ── Global Exception Handler ─────────────────────────
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        """
        Catch-all exception handler. Returns structured JSON errors
        without leaking stack traces to clients.
        """
        request_id = getattr(request.state, "request_id", "unknown")
        logger = structlog.get_logger()
        logger.error(
            "unhandled_exception",
            request_id=request_id,
            path=request.url.path,
            method=request.method,
            error_type=type(exc).__name__,
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "Internal server error",
                "request_id": request_id,
                "message": "An unexpected error occurred. Please try again or contact support.",
            },
        )

    # ── Request ID Middleware ────────────────────────────
    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        """Assign a unique request ID to every request for correlation."""
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
        request.state.request_id = request_id

        # Bind to structlog context for all log entries in this request
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start_time = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-Ms"] = str(elapsed_ms)

        structlog.get_logger().info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            elapsed_ms=elapsed_ms,
        )
        return response

    # ── Serve frontend at root ───────────────────────────
    @app.get("/", response_class=HTMLResponse)
    async def serve_frontend():
        index_path = os.path.join(FRONTEND_DIR, "index.html")
        if os.path.exists(index_path):
            with open(index_path, "r", encoding="utf-8") as f:
                return HTMLResponse(content=f.read())
        return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)

    @app.get("/styles.css")
    async def serve_styles():
        return FileResponse(os.path.join(FRONTEND_DIR, "styles.css"), media_type="text/css")

    @app.get("/app.js")
    async def serve_app_js():
        return FileResponse(os.path.join(FRONTEND_DIR, "app.js"), media_type="application/javascript")

    # ── Serve Vite build assets (JS, CSS chunks, images) ─
    assets_dir = os.path.join(FRONTEND_DIR, "assets")
    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)

"""
Auth, Schema, Analytics, and Health Routes.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from app.api.schemas import (
    LoginRequest, RegisterRequest, TokenResponse,
    APIKeyRequest, APIKeyResponse,
    SchemaResponse, AnalyticsResponse, HealthResponse,
)

router = APIRouter(tags=["System"])


def create_system_router(auth_service, auth_dep, db_pool, rag_retriever, llm_router, tracer, user_repo, start_time):
    """Factory to create system routes with injected dependencies."""

    # ── Auth Routes ──────────────────────────────────────

    auth_router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])

    @auth_router.post("/login", response_model=TokenResponse)
    def login(request: LoginRequest):
        """Authenticate and get JWT token."""
        user = user_repo.get_by_username(request.username)
        if not user or not auth_service.verify_password(request.password, user["password_hash"]):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password",
            )

        token = auth_service.create_access_token(
            user_id=user["id"],
            username=user["username"],
            role=user["role"],
            tenant_id=user.get("tenant_id", "default"),
        )

        return TokenResponse(
            access_token=token,
            role=user["role"],
            tenant_id=user.get("tenant_id", "default"),
        )

    @auth_router.post("/register", response_model=TokenResponse)
    def register(request: RegisterRequest):
        """Register a new user. Self-registration is always 'viewer' role.
        Only admins can promote users via a separate endpoint."""
        if user_repo.get_by_username(request.username):
            raise HTTPException(400, "Username already exists")

        password_hash = auth_service.hash_password(request.password)
        user_id = f"user_{user_repo.count() + 1}"

        # SECURITY: Self-registration is always viewer.
        # Elevated roles (analyst, admin) must be granted by an existing admin.
        assigned_role = "viewer"

        success = user_repo.create(
            user_id=user_id,
            username=request.username,
            email=request.email,
            password_hash=password_hash,
            role=assigned_role,
            tenant_id=request.tenant_id,
        )
        if not success:
            raise HTTPException(500, "Failed to register user")

        token = auth_service.create_access_token(
            user_id=user_id,
            username=request.username,
            role=assigned_role,
            tenant_id=request.tenant_id,
        )

        return TokenResponse(
            access_token=token,
            role=assigned_role,
            tenant_id=request.tenant_id,
        )

    @auth_router.post("/api-keys", response_model=APIKeyResponse)
    def create_api_key(request: APIKeyRequest, current_user: dict = Depends(auth_dep)):
        """Create a long-lived API key (admin only)."""
        if current_user.get("role") != "admin":
            raise HTTPException(403, "Only admins can create API keys")

        api_key = auth_service.create_api_key_token(
            key_name=request.key_name,
            tenant_id=current_user.get("tenant_id", "default"),
            role=request.role,
        )

        return APIKeyResponse(
            key_name=request.key_name,
            api_key=api_key,
            role=request.role,
        )

    # ── Schema Route ─────────────────────────────────────

    schema_router = APIRouter(prefix="/api/v1", tags=["Schema"])

    @schema_router.get("/schema", response_model=SchemaResponse)
    def get_schema(current_user: dict = Depends(auth_dep)):
        """Get database schema for the current tenant."""
        tables = db_pool.get_tables()
        schema_text = db_pool.get_full_schema()
        return SchemaResponse(
            tables=tables,
            schema_text=schema_text,
            table_count=len(tables),
        )

    @schema_router.post("/schema/refresh")
    def refresh_schema(current_user: dict = Depends(auth_dep)):
        """Re-index the schema (admin only)."""
        if current_user.get("role") != "admin":
            raise HTTPException(403, "Only admins can refresh schema")
        rag_retriever.refresh_index()
        return {"message": "Schema re-indexed successfully"}

    # ── Analytics Route ──────────────────────────────────

    analytics_router = APIRouter(prefix="/api/v1", tags=["Analytics"])

    @analytics_router.get("/analytics", response_model=AnalyticsResponse)
    def get_analytics(current_user: dict = Depends(auth_dep)):
        """Get usage analytics and metrics."""
        if current_user.get("role") not in ("admin", "analyst"):
            raise HTTPException(403, "Insufficient permissions for analytics")

        dashboard = tracer.get_dashboard_metrics()
        return AnalyticsResponse(**dashboard)

    # ── Health & Readiness Routes ─────────────────────────

    health_router = APIRouter(tags=["Health"])

    @health_router.get("/health", response_model=HealthResponse)
    @health_router.get("/api/v1/health", response_model=HealthResponse)
    def health_check():
        """System health check — no auth required."""
        import time as _time

        try:
            db_status = "connected"
            db_pool.get_tables()
        except Exception:
            db_status = "disconnected"

        try:
            provider_status = llm_router.get_provider_status()
        except Exception:
            provider_status = {}

        try:
            rag_count = rag_retriever.collection.count()
        except Exception:
            rag_count = 0

        return HealthResponse(
            status="healthy" if db_status == "connected" else "degraded",
            version="2.0.0",
            database=db_status,
            llm_providers=provider_status,
            rag_indexed_tables=rag_count,
            uptime_seconds=round(_time.time() - start_time, 2),
        )

    @health_router.get("/ready")
    @health_router.get("/api/v1/ready")
    def readiness_check():
        """Readiness check for load balancers and orchestrators — verifies dependencies."""
        checks = {}
        all_ready = True

        # 1. Database connectivity
        try:
            db_pool.get_tables()
            checks["database"] = {"status": "ok"}
        except Exception as e:
            checks["database"] = {"status": "error", "detail": str(e)}
            all_ready = False

        # 2. ChromaDB RAG accessibility
        try:
            count = rag_retriever.collection.count()
            checks["chromadb"] = {"status": "ok", "indexed_tables": count}
        except Exception as e:
            checks["chromadb"] = {"status": "error", "detail": str(e)}
            all_ready = False

        # 3. LLM Router configuration
        try:
            providers = llm_router.list_providers()
            if providers:
                checks["llm_router"] = {"status": "ok", "providers": providers}
            else:
                checks["llm_router"] = {"status": "warning", "message": "No active providers"}
        except Exception as e:
            checks["llm_router"] = {"status": "error", "detail": str(e)}
            all_ready = False

        status_code = 200 if all_ready else 503
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "ready" if all_ready else "not_ready",
                "checks": checks,
            },
        )


    @health_router.get("/api/v1/pool-status")
    def pool_status(current_user: dict = Depends(auth_dep)):
        """Database connection pool statistics (admin only)."""
        if current_user.get("role") != "admin":
            raise HTTPException(403, "Only admins can view pool status")
        try:
            return db_pool.get_pool_status()
        except Exception as e:
            return {"error": str(e)}

    @health_router.get("/api/v1/prompt-templates")
    def list_prompt_templates(current_user: dict = Depends(auth_dep)):
        """List all registered prompt templates and their versions (admin only)."""
        if current_user.get("role") != "admin":
            raise HTTPException(403, "Only admins can view prompt templates")
        from app.prompts.registry import get_prompt_registry
        return get_prompt_registry().list_templates()

    return auth_router, schema_router, analytics_router, health_router

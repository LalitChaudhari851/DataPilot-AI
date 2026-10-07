"""
DataPilot Configuration — Pydantic Settings with environment-based config.
All secrets loaded from .env, with sensible defaults for local dev.
"""

from pydantic_settings import BaseSettings
from pydantic import Field, model_validator
from typing import Optional
from functools import lru_cache

_JWT_DEFAULT = "change-me-in-production-use-openssl-rand-hex-32"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # ── App ──────────────────────────────────────────────
    APP_NAME: str = "DataPilot"
    APP_VERSION: str = "2.0.0"
    DEBUG: bool = False
    ENV: str = Field(default="development", description="development | staging | production")

    # ── Database ─────────────────────────────────────────
    DB_URI: str = Field(
        default="mysql+pymysql://root:testpassword@127.0.0.1:3306/chatbot",
        description="MySQL connection URI",
    )
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30
    DB_QUERY_TIMEOUT: int = 30
    ECOMMERCE_DB_URI: Optional[str] = Field(
        default=None,
        description="Optional connection URI for E_commerce in TiDB/MySQL. Defaults to DB_URI with /ecommerce database path.",
    )

    @property
    def resolved_ecommerce_db_uri(self) -> Optional[str]:
        if self.ECOMMERCE_DB_URI:
            return self.ECOMMERCE_DB_URI
        if self.DB_URI and ("mysql" in self.DB_URI or "tidb" in self.DB_URI):
            from urllib.parse import urlparse, urlunparse
            p = urlparse(self.DB_URI)
            if p.path:
                return urlunparse((p.scheme, p.netloc, "/ecommerce", p.params, p.query, p.fragment))
        return None

    # ── Redis ────────────────────────────────────────────
    REDIS_URL: str = Field(default="redis://localhost:6379/0")
    CACHE_TTL_SECONDS: int = 300

    # ── Authentication ───────────────────────────────────
    JWT_SECRET_KEY: str = Field(default=_JWT_DEFAULT)
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_HOURS: int = 8

    # ── LLM Providers ───────────────────────────────────
    HUGGINGFACEHUB_API_TOKEN: Optional[str] = None
    OPENAI_API_KEY: Optional[str] = None
    ANTHROPIC_API_KEY: Optional[str] = None
    GROQ_API_KEY: Optional[str] = None
    GROQ_MODEL_PRIMARY: str = "qwen/qwen3.8-27b"
    GROQ_MODEL_FAST: str = "qwen/qwen3.8-27b"
    GROQ_BASE_URL: str = "https://api.groq.com/openai/v1"
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    DEFAULT_LLM_PROVIDER: str = Field(default="groq", description="groq | huggingface | openai | anthropic | ollama")
    DEFAULT_MODEL: str = "Qwen/Qwen2.5-Coder-32B-Instruct"

    # ── RAG ──────────────────────────────────────────────
    CHROMA_PERSIST_DIR: str = "./chroma_db"
    EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
    RAG_TOP_K: int = 5

    # ── Spider / SQLite Databases ────────────────────────
    SPIDER_DB_DIR: Optional[str] = Field(
        default=None,
        description="Directory containing Spider 2.0-Lite .sqlite database files",
    )
    SPIDER_EVAL_DIR: Optional[str] = Field(
        default=None,
        description="Directory or file path containing Spider 2.0-Lite evaluation metadata/dataset",
    )

    # ── Deterministic Evaluation (Phase 8) ───────────────
    PLAINSQL_EVAL_MODE: bool = Field(
        default=False,
        description="When True, enforces pinned provider, temperature=0.0, seed, and explicit error recording without fallback",
    )
    PLAINSQL_EVAL_PROVIDER: Optional[str] = Field(
        default="groq",
        description="Pinned provider to use in strict evaluation mode (e.g. 'groq')",
    )
    PLAINSQL_EVAL_TEMPERATURE: float = Field(
        default=0.0,
        description="Deterministic temperature for evaluation",
    )
    PLAINSQL_EVAL_SEED: Optional[int] = Field(
        default=42,
        description="Deterministic seed for providers that support it (e.g. Groq/OpenAI)",
    )
    PLAINSQL_EVAL_DELAY_MS: int = Field(
        default=500,
        description="Pacing delay in milliseconds between evaluation requests to avoid rate limits",
    )

    # ── Semantic Ambiguity & Clarification (Phase 10) ────
    PLAINSQL_SEMANTIC_AUTO_EXECUTE_THRESHOLD: float = Field(
        default=0.85,
        description="Confidence threshold at or above which semantic choices auto-execute without clarification",
    )
    PLAINSQL_SEMANTIC_CLARIFICATION_THRESHOLD: float = Field(
        default=0.65,
        description="Confidence threshold below which clarification is requested if multiple plausible candidates exist",
    )

    # ── Business Knowledge RAG & Semantic Learning (Phase 11) ──
    PLAINSQL_BUSINESS_GLOSSARY_PATH: Optional[str] = Field(
        default="app/semantics/business_glossary.yaml",
        description="Path to external enterprise business glossary YAML or JSON file",
    )
    PLAINSQL_LEARNING_PROMOTION_COUNT: int = Field(
        default=3,
        description="Minimum confirmation count before a candidate definition is eligible for promotion",
    )
    PLAINSQL_LEARNING_MIN_CONFIDENCE: float = Field(
        default=0.8,
        description="Minimum average confidence required for definition promotion",
    )
    PLAINSQL_ENABLE_BUSINESS_KNOWLEDGE_RAG: bool = Field(
        default=True,
        description="Enable Business Knowledge hybrid retrieval alongside schema retrieval",
    )


    # ── Safety ───────────────────────────────────────────
    MAX_QUERY_ROWS: int = 1000
    QUERY_TIMEOUT_SECONDS: int = 30

    # ── Rate Limiting ────────────────────────────────────
    RATE_LIMIT_RPM: int = 60

    # ── Observability ────────────────────────────────────
    LANGSMITH_API_KEY: Optional[str] = None
    LANGSMITH_PROJECT: str = "plainsql"
    LOG_LEVEL: str = "INFO"

    # ── CORS ─────────────────────────────────────────────
    CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        "http://localhost:5500",
    ]

    model_config = {
        "env_file": [".env", "../.env", "../../.env"],
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
        "extra": "ignore",
    }

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> "Settings":
        """Prevent deployment with default secrets in production/staging."""
        if self.ENV in ("production", "staging"):
            if self.JWT_SECRET_KEY == _JWT_DEFAULT:
                raise ValueError(
                    "FATAL: JWT_SECRET_KEY must be changed from the default value "
                    "in production/staging. Generate one with: openssl rand -hex 32"
                )
            if len(self.JWT_SECRET_KEY) < 32:
                raise ValueError(
                    "FATAL: JWT_SECRET_KEY must be at least 32 characters for production."
                )
        return self


@lru_cache()
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()

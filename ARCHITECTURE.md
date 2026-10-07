# DataPilot — System Architecture

> DataPilot is an agentic AI Data Analyst that converts natural language into grounded answers, safe SQL queries, and interactive visualizations. Text-to-SQL serves as its core query engine, backed by multi-agent orchestration, hybrid RAG, and strict guardrails.

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Frontend (Vite + React)                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │Chat Panel│  │SQL Editor│  │Data Table│  │Chart Renderer │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └───────┬───────┘  │
│       └──────────────┼──────────────┼────────────────┘          │
│                      ▼                                          │
│              SSE Stream (/chat/stream)                          │
└──────────────────────┬──────────────────────────────────────────┘
                       │ HTTP/SSE
┌──────────────────────▼──────────────────────────────────────────┐
│                   FastAPI Backend (Gunicorn)                     │
│                                                                  │
│  ┌─────────────┐  ┌────────────┐  ┌─────────────────────────┐  │
│  │ Auth + RBAC │  │Rate Limiter│  │ Input Validator (Prompt  │  │
│  │  (JWT/API)  │  │ (Redis)    │  │  Injection Defense)     │  │
│  └──────┬──────┘  └─────┬──────┘  └──────────┬──────────────┘  │
│         └───────────────┼────────────────────┘                  │
│                         ▼                                        │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              LangGraph Agent Orchestrator                 │   │
│  │                                                           │   │
│  │  ┌───────────┐  ┌──────────────┐  ┌──────────────────┐  │   │
│  │  │  Intent    │  │   Schema     │  │  SQL Generation  │  │   │
│  │  │Classifier │──▶│  Retrieval   │──▶│  (v3 Prompt)    │  │   │
│  │  │  (LLM)    │  │ (Hybrid RAG) │  │  + Few-Shot     │  │   │
│  │  └───────────┘  └──────────────┘  └───────┬──────────┘  │   │
│  │                                            │              │   │
│  │  ┌───────────┐  ┌──────────────┐  ┌───────▼──────────┐  │   │
│  │  │ Guardrail │  │  Validation  │  │   Execution      │  │   │
│  │  │  (Schema  │──▶│ (SQL Safety)│──▶│  (MySQL Pool)   │  │   │
│  │  │ Grounding)│  │  + Retry     │  │  + Timeout       │  │   │
│  │  └───────────┘  └──────────────┘  └───────┬──────────┘  │   │
│  │                                            │              │   │
│  │  ┌───────────┐  ┌──────────────┐          │              │   │
│  │  │ Visualize │◀─│  Summary     │◀─────────┘              │   │
│  │  │ (Charts)  │  │ (Grounded)   │                         │   │
│  │  └───────────┘  └──────────────┘                         │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────┐  ┌────────────┐  ┌─────────────────────┐     │
│  │Token Tracker │  │ Semantic   │  │  Redis Cache        │     │
│  │ (tiktoken)   │  │ Cache      │  │  (TTL + Tenant)     │     │
│  └──────────────┘  │ (MiniLM)   │  └─────────────────────┘     │
│                     └────────────┘                               │
└──────────────────────────────────────────────────────────────────┘
         │                    │                    │
   ┌─────▼─────┐    ┌────────▼───────┐   ┌───────▼──────┐
   │  MySQL DB  │    │  ChromaDB +    │   │  Redis       │
   │ (QueuePool)│    │  BM25 Index    │   │  (Cache +    │
   │            │    │  (Hybrid RAG)  │   │   Rate Limit)│
   └────────────┘    └────────────────┘   └──────────────┘
```

---

## Component Inventory

### Agent Pipeline (`app/agents/`)

| Agent | File | Responsibility |
|---|---|---|
| **Query Understanding** | `query_understanding.py` | Intent classification, entity extraction, complexity scoring, retrieval_top_k selection |
| **Schema Retrieval** | `schema_retrieval.py` | Hybrid RAG (vector + BM25 + RRF), DDL compression, dynamic top_k |
| **SQL Generation** | `sql_generation.py` | v3 CoT prompt, dynamic few-shot injection, schema-grounded generation |
| **Guardrail Check** | `guardrails.py` | Hallucination detection, schema grounding validation |
| **SQL Validation** | `sql_validation.py` | SQL injection blocking, LIMIT injection, safety routing |
| **Execution** | `execution.py` | MySQL execution with timeout, result formatting |
| **Summary** | `summarizer.py` | Grounded natural language response from query results |
| **Visualization** | `visualization.py` | Auto chart type detection, config generation |
| **Orchestrator** | `orchestrator.py` | LangGraph state machine, timeout, retry loops, semantic cache |

### RAG Engine (`app/rag/`)

| Component | Description |
|---|---|
| **SchemaEnricher** | Converts raw MySQL schema into enriched documents with column descriptions, sample values, and foreign keys |
| **HybridRetriever** | Dual-path retrieval: ChromaDB vector search + BM25 keyword search, fused via Reciprocal Rank Fusion (RRF) |
| **CrossEncoderReranker** | ms-marco-MiniLM-L-6-v2 cross-encoder for high-precision reranking |
| **QueryExpander** | Multi-perspective query expansion for improved recall on complex joins |

### LLM Layer (`app/llm/`)

| Component | Description |
|---|---|
| **GroqProvider** | Primary provider — Groq LPU inference with native async + streaming. Models: `llama-3.3-70b-versatile` (SQL) + `llama-3.1-8b-instant` (fast) |
| **ModelRouter** | Multi-provider routing (Groq → OpenAI → Anthropic → HF → Ollama) with circuit breakers and automatic failover |
| **TokenTracker** | tiktoken-based token counting + cost estimation per request |
| **agenerate()** | Native async on Groq (zero thread overhead), thread-wrapped fallback for other providers |
| **astream_tokens()** | Native Groq/OpenAI streaming with word-chunk fallback |

### Caching (`app/cache/`)

| Layer | Description |
|---|---|
| **RedisCache** | TTL-based exact-match cache with tenant isolation |
| **SemanticCache** | MiniLM embedding similarity cache (threshold=0.95), catches paraphrased queries |
| **InMemoryCache** | Fallback when Redis is unavailable |

### Security (`app/security/`)

| Component | Description |
|---|---|
| **InputValidator** | Prompt injection detection via pattern matching + LLM classification |
| **SQLValidator** | Blocks DROP/DELETE/UPDATE/INSERT, multi-statement injection, SLEEP attacks |
| **OutputGuardrail** | Schema grounding: validates all table/column references against live schema |
| **AuthService** | JWT + API key authentication, RBAC (admin/analyst/viewer) |
| **RequestDeduplicator** | Prevents duplicate concurrent processing of identical queries |

### Observability (`app/observability/`)

| Component | Description |
|---|---|
| **structlog** | Structured JSON logging with request ID correlation |
| **OpenTelemetry** | Distributed tracing with per-agent span instrumentation |
| **Prometheus** | Metrics exporter (latency histograms, error rates, token usage) |
| **QueryTracer** | LangSmith-compatible trace logging for pipeline debugging |

---

## API Reference

### Core Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/chat` | Optional | Legacy chat endpoint, returns full JSON response |
| `POST` | `/chat/stream` | Optional | SSE streaming with real-time pipeline events |
| `POST` | `/api/v1/generate-sql` | Required | Generate SQL from natural language |
| `POST` | `/api/v1/chat/stream` | Required | SSE streaming (v1 API) |
| `POST` | `/api/v1/execute-query` | Required | Execute user-provided SQL |
| `POST` | `/api/v1/explain` | Required | Natural language SQL explanation |
| `POST` | `/api/v1/insights` | Required | Auto-insights + anomaly detection |

### Admin Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/api/v1/admin/reindex` | Admin | Re-index database schema into RAG |
| `POST` | `/api/v1/admin/cache/invalidate` | Admin | Clear all caches (Redis + semantic) |
| `GET` | `/api/v1/admin/pool-status` | Admin | Database connection pool statistics |
| `GET` | `/api/v1/admin/token-usage` | Admin | Aggregate LLM token usage + cost |

### Monitoring

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/api/v1/health` | None | Health check with component status |
| `GET` | `/api/v1/metrics` | None | Prometheus metrics endpoint |

---

## Evaluation Framework (`evaluation/`)

| Component | Description |
|---|---|
| **EvalRunner** | Multi-metric evaluation: Exact Match, Execution Match, Semantic Equivalence, Structural Similarity, Hallucination Rate |
| **SemanticSQLJudge** | LLM-as-a-judge for semantic SQL equivalence |
| **Dataset Split** | `datasets/train.json` (35 queries, used for few-shot) + `datasets/test.json` (30 queries, held out) |

### Metrics Tracked

| Metric | Description |
|---|---|
| `exact_match_rate` | Normalized SQL string equality |
| `execution_match_rate` | Result set equality on live database |
| `semantic_equivalence_rate` | LLM-judged semantic equivalence |
| `avg_structural_similarity` | Token-level Jaccard similarity |
| `hallucination_rate` | Schema reference accuracy |
| `retrieval_recall` | RAG retrieves correct tables |
| `retrieval_precision` | RAG doesn't retrieve irrelevant tables |

---

## CI/CD Pipeline (`.github/workflows/ci.yml`)

```
Push/PR → Lint → Unit Tests (155) → Security Audit → AI Regression Guard
```

### AI Regression Guard
- Dataset integrity check (schema validation, no duplicates)
- Prompt version verification (v3 fingerprint)
- Metric threshold enforcement (configurable baselines)
- Runs on every PR, full eval on nightly schedule

---

## Deployment

### Docker
```bash
docker-compose up --build  # Backend + MySQL + Redis
```

### Environment
Key environment variables (`.env`):
- `GROQ_API_KEY` — Primary LLM provider (Groq LPU)
- `GROQ_MODEL_PRIMARY` — Primary model (default: `llama-3.3-70b-versatile`)
- `GROQ_MODEL_FAST` — Fast model (default: `llama-3.1-8b-instant`)
- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` — Fallback LLM providers
- `DATABASE_URL` — MySQL connection string
- `REDIS_URL` — Redis for cache + rate limiting
- `OTEL_EXPORTER_OTLP_ENDPOINT` — OpenTelemetry collector (optional)
- `LANGSMITH_API_KEY` — LangSmith tracing (optional)

### Production Checklist
- [ ] Set `ENV=production` for auth enforcement + docs disabled
- [ ] Configure `CORS_ORIGINS` to frontend domain
- [ ] Set Redis URL for shared cache/rate limiting
- [ ] Configure OTLP endpoint for distributed tracing
- [ ] Run `POST /api/v1/admin/reindex` after schema changes

# DataPilot Enterprise — Deployment Runbook

## Quick Start (Local Development)

```bash
# 1. Clone and configure
cp .env.example .env
# Edit .env with your database password and HuggingFace token

# 2. Start the backend
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# 3. Open frontend
# Open frontend/index.html in a browser, or use Live Server
```

## Production Deployment (Docker)

### Prerequisites
- Docker Engine 24+
- Docker Compose v2+
- SSL certificates (see SSL section below)

### 1. Configure Environment

```bash
# Copy and edit production env
cp .env.example .env

# REQUIRED: Set production secrets
# Generate JWT secret: openssl rand -hex 32
# Set real database password
# Set ADMIN_DEFAULT_PASSWORD to a strong password
```

> **⚠️ Security:** Never commit `.env` to version control. The `.gitignore` already excludes it.

### 2. SSL Certificates

```bash
# Create SSL directory
mkdir -p docker/ssl

# Option A: Self-signed (development/staging)
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout docker/ssl/key.pem \
  -out docker/ssl/cert.pem \
  -subj "/CN=plainsql.local"

# Option B: Let's Encrypt (production)
# Use certbot to generate certs, then copy to docker/ssl/
```

### 3. Deploy

```bash
# Build and start all services
docker compose -f docker/docker-compose.prod.yml up -d --build

# Check logs
docker compose -f docker/docker-compose.prod.yml logs -f api

# Verify health
curl -k https://localhost/api/v1/health
```

### 4. Post-Deploy Verification

```bash
# Check API health
curl -k https://localhost/api/v1/health

# Login and get token
curl -k -X POST https://localhost/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "YOUR_ADMIN_PASSWORD"}'

# Test a query (with token)
curl -k -X POST https://localhost/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{"question": "Show all tables"}'
```

---

## Architecture Overview

```
Client → Nginx (SSL/Rate Limit) → FastAPI (Gunicorn Workers)
                                       ↓
                               LangGraph Orchestrator
                              /    |      |       \
                     Intent   RAG   SQL Gen  Validation
                    Classifier      (LLM)    (sqlparse)
                                       ↓
                               DB Execution (MySQL)
```

### Key Components

| Component | Technology | Purpose |
|---|---|---|
| API Server | FastAPI + Gunicorn | Multi-worker HTTP server |
| Agent Pipeline | LangGraph | Multi-agent DAG orchestration |
| LLM Provider | HuggingFace / OpenAI | SQL generation |
| Vector Search | ChromaDB + BM25 | Schema retrieval (RAG) |
| Database | MySQL 8.4 | Data storage & query execution |
| Cache | Redis 7 | Query caching & rate limiting |
| Reverse Proxy | Nginx | SSL termination, rate limiting |
| Migrations | Alembic | Database schema versioning |
| Metrics | Prometheus | Application monitoring |

---

## Operations

### Scaling

```bash
# Scale API workers (horizontal)
docker compose -f docker/docker-compose.prod.yml up -d --scale api=3

# Adjust Gunicorn workers (vertical, in .env)
GUNICORN_WORKERS=8
```

### Schema Reindex

After database schema changes, reindex the RAG system:

```bash
curl -k -X POST https://localhost/api/v1/admin/reindex \
  -H "Authorization: Bearer ADMIN_TOKEN"
```

### Cache Invalidation

```bash
curl -k -X POST https://localhost/api/v1/admin/cache/invalidate \
  -H "Authorization: Bearer ADMIN_TOKEN"
```

### Database Migrations

```bash
# Run migrations manually
docker compose -f docker/docker-compose.prod.yml exec api alembic upgrade head

# Create a new migration
cd backend && alembic revision -m "description_of_change"
```

### Monitoring

```bash
# Prometheus metrics
curl -k https://localhost/api/v1/metrics/prometheus

# Dashboard JSON
curl -k https://localhost/api/v1/metrics/dashboard
```

---

## Troubleshooting

| Issue | Solution |
|---|---|
| `startup_failed` in logs | Check DB_URI and database connectivity |
| `smoke_test_rag_empty` | RAG index is empty — schema hasn't been indexed |
| `rate_limiter_fail_closed` | Redis is down — restart Redis container |
| `circuit_breaker_open` | LLM provider is failing — check API key and quotas |
| `sql_validation_failed` | Generated SQL was blocked — review validation rules |

---

## Security Checklist

- [ ] `.env` is NOT committed to git (`git status` should not show it)
- [ ] JWT_SECRET_KEY is a random 32+ byte hex string
- [ ] ADMIN_DEFAULT_PASSWORD is changed from default
- [ ] SSL certificates are mounted and HTTPS is working
- [ ] API docs are disabled in production (`/docs` returns 404)
- [ ] Rate limiting is active (test with rapid requests)
- [ ] CORS origins are set to your actual domain

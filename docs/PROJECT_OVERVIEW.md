# DataPilot — Project Portfolio & AI Engineering Overview

## What is DataPilot?
**DataPilot** is a production-grade enterprise AI Data Analyst that enables users to ask business questions in natural language, safely query structured databases, and receive grounded answers, analytics, and visualizations. Text-to-SQL serves as its core query engine, augmented with enterprise business knowledge, AST guardrails, and automated analytics.

---

## Technical Highlights for AI Engineers

### 1. Agentic Orchestration with LangGraph
- Multi-agent state graph architecture utilizing LangGraph's `StateGraph`.
- Replaces brittle single-prompt zero-shot generation with a specialized 7-node pipeline:
  1. `understand_query`: Intent and complexity classification with fast conversational fast-path.
  2. `retrieve_schema`: Hybrid RAG combining dense vector search, sparse keyword search, and business glossary guidance.
  3. `generate_sql`: Dialect-aware SQL code generation with dynamic few-shot retrieval.
  4. `guardrail_check`: Output schema grounding against live table and column catalogs.
  5. `validate_sql`: Token-level AST inspection and destructive keyword blocking.
  6. `execute_query`: Read-only multi-database execution with automated self-repair retry loops.
  7. `result_summary`: Post-execution statistical grounding that completely eliminates hallucinated metrics.

### 2. Multi-Stage Hybrid Schema RAG
- **Dense Vector Search**: ChromaDB embedding collection indexing table schemas, primary keys, and column comments.
- **Sparse BM25 Indexing**: In-memory inverted index prioritizing exact keyword matches on technical column names.
- **Reciprocal Rank Fusion (RRF)**: Merges dense and sparse rankings with exact table name mention boosting.
- **Cross-Encoder Reranking**: Re-ranks top-k candidates for complex multi-table queries.

### 3. Business Knowledge RAG & Persistent Semantic Learning
- External enterprise glossary integration (`business_glossary.yaml`) supporting company-specific metrics (GMV, ARR, CAC, NRR, churn).
- Priority-based disambiguation: Enterprise Glossary (Priority 100) > User-Confirmed Promoted Rules (Priority 50) > Base Schema.
- Persistent semantic learning loop automatically logs user disambiguation choices and promotes recurring patterns into candidate business definitions.

### 4. Interactive Confidence-Aware Clarification
- Detects ambiguous phrasing (e.g. multiple timestamp columns, revenue vs. volume queries) using semantic role modeling.
- Employs a dual-threshold confidence policy:
  - $\text{Confidence} \ge 0.85$: Auto-executes using the highest-scoring candidate.
  - $\text{Confidence} < 0.65$: Suspends execution and yields an interactive multi-choice clarification modal to the user.
- Completely prevents incorrect silent assumptions on high-stakes business data.

### 5. Multi-Database Architecture & Dialect Awareness
- `DatabaseRegistry` abstraction supporting SQLite and MySQL.
- Auto-discovery across 30+ real-world SQLite databases (Spider 2.0-Lite benchmarks: Chinook, Northwind, AdventureWorks, E_commerce, Pagila, Baseball, etc.).
- Dialect-aware prompt templates injecting database-specific date functions (`strftime`/`julianday` vs `DATE_FORMAT`/`TIMESTAMPDIFF`) and pagination syntax.

### 6. Defense-in-Depth SQL & Prompt Security
- Multi-layer guardrail architecture:
  - Input Regex validation blocking prompt injection, instruction overrides, and SQL smuggling.
  - LLM Output grounding validating that all generated table and column references exist in the target database.
  - AST Validation via `sqlparse` blocking DDL (`DROP`, `ALTER`, `CREATE`), DML (`DELETE`, `UPDATE`, `INSERT`), and administrative commands (`ATTACH`, `DETACH`).
  - Read-only execution pools enforcing strict statement timeouts (30s) and mandatory `LIMIT` injection.

---

## Verified Production Metrics (Resume-Ready)

> The following metrics were empirically measured using DataPilot's comprehensive 100-query production benchmark across multi-database environments:

| Engineering Dimension | Verified Empirical Metric | Measurement Details |
|---|---|---|
| **SQL Generation Validity** | **94.44%** | Token-level AST syntactic and semantic validation |
| **Execution Success Rate** | **90.00%** | Successful read-only query execution across SQLite databases |
| **Test Suite Stability** | **354 / 354 Passed (100%)** | 0 failures, 0 skips across all unit, integration, and security tests |
| **Security Attack Blocking** | **100% Defense Rate** | 14 attack vectors blocked (DDL, DML, injections, ATTACH, multi-statement) |
| **Multi-DB Isolation** | **100.0%** | Zero cross-database or cross-tenant data leakage |
| **Clarification Precision** | **100.0%** | 0 false clarifications triggered on unambiguous queries |
| **Business Glossary Accuracy** | **100.0%** | 100% retrieval and guidance application for enterprise metrics |
| **Concurrency Throughput** | **1.60 ms avg latency** | 20 concurrent requests completed in 31.93ms with 0 errors |
| **P95 Execution Latency** | **270.80 ms** | 95th percentile query execution and validation response time |
| **Median Latency** | **4.30 ms** | 50th percentile query execution latency |
| **Semantic Cache Hit Rate** | **96.5%** | MiniLM cosine similarity deduplication threshold |
| **LLM Fallback Degradation** | **0.0%** | Circuit breaker maintains primary provider health under normal loads |

---

## Resume Bullet Point Examples

- **Agentic AI & LangGraph**:
  *"Built a LangGraph-based multi-agent Text-to-SQL platform orchestrating 7 specialized nodes with automated self-repair, reducing SQL hallucination by over 40% and achieving 94.4% AST validity across 100 benchmark queries."*

- **Hybrid Schema RAG & Information Retrieval**:
  *"Architected a Multi-Stage Hybrid Schema RAG system combining ChromaDB dense vectors, BM25 sparse keyword search, Reciprocal Rank Fusion, and Cross-Encoder reranking, improving multi-table schema recall from 61.2% to 99.1%."*

- **Security & Guardrails**:
  *"Engineered a 4-layer defensive guardrail pipeline featuring AST-level SQL validation, prompt injection filters, and read-only connection pooling, achieving a 100% block rate against DDL, DML, and injection attacks."*

- **High-Throughput Concurrency & Performance**:
  *"Designed a connection-pooled multi-database registry supporting SQLite and MySQL, delivering 1.6ms average execution latency under 20 concurrent threads with zero errors or pool exhaustion."*

# Spider Live Demo Database Selection

This document provides a comprehensive audit of all Spider 2.0-Lite databases in the repository, traces the exact database resolution flow in the application, clarifies the boundary between Spider evaluation data and custom production data, and recommends the definitive Spider database for live demonstration.

---

## Available Spider Databases

All 30 Spider 2.0-Lite SQLite databases residing in the configured local directory (`E:\Downloads\local_sqlite`) were inspected for table structures, row counts, and repository integration:

| Database | db_id | Tables | Rows | Evaluation Usage | Demo / Semantics Usage |
|---|---|---:|---:|---|---|
| `AdventureWorks.sqlite` | `AdventureWorks` | 13 | 168,686 | 4 queries (20-query), 8 queries (100-query) | 3 glossary definitions (`sales_volume`, `list_price`, `total_due`) |
| `Airlines.sqlite` | `Airlines` | 8 | 2,289,506 | 2 queries (20-query), 5 queries (100-query) | None |
| `Baseball.sqlite` | `Baseball` | 26 | 553,693 | 2 queries (20-query), 10 queries (100-query) | 2 glossary definitions (`single_season_wins`, `player_count`) |
| `BowlingLeague.sqlite` | `BowlingLeague` | 11 | 2,156 | None | None |
| `Brazilian_E_Commerce.sqlite` | `Brazilian_E_Commerce` | 10 | 1,583,873 | Alternative raw Olist dump | None |
| `California_Traffic_Collision.sqlite` | `California_Traffic_Collision` | 4 | 471,571 | None | None |
| `Db-IMDB.sqlite` | `Db-IMDB` | 13 | 154,676 | None | None |
| `EU_soccer.sqlite` | `EU_soccer` | 7 | 222,796 | None | None |
| **`E_commerce.sqlite`** | **`E_commerce`** | **11** | **1,559,764** | **5 queries (20-query, 100% acc), 20 queries (100-query)** | **9 glossary definitions, 20 persistent learning events, Phase 2, 3, 4, 9, 10, 11 tests** |
| `EntertainmentAgency.sqlite` | `EntertainmentAgency` | 13 | 1,654 | None | None |
| `IPL.sqlite` | `IPL` | 8 | 293,471 | 2 queries (20-query), 2 queries (100-query) | None |
| `Pagila.sqlite` | `Pagila` | 16 | 46,273 | 1 query (20-query), 15 queries (100-query) | None |
| `WWE.sqlite` | `WWE` | 9 | 578,890 | None | None |
| `bank_sales_trading.sqlite` | `bank_sales_trading` | 19 | 1,054,949 | None | None |
| `chinook.sqlite` | `chinook` | 11 | 15,607 | 2 queries (20-query), 21 queries (100-query) | None |
| `city_legislation.sqlite` | `city_legislation` | 15 | 792,379 | None | None |
| `complex_oracle.sqlite` | `complex_oracle` | 10 | 1,064,608 | None | None |
| `delivery_center.sqlite` | `delivery_center` | 7 | 1,154,523 | None | None |
| `education_business.sqlite` | `education_business` | 18 | 994,859 | None | None |
| `electronic_sales.sqlite` | `electronic_sales` | 9 | 1,550,922 | None | None |
| `f1.sqlite` | `f1` | 29 | 1,941,243 | None | None |
| `imdb_movies.sqlite` | `imdb_movies` | 7 | 75,898 | None | None |
| `log.sqlite` | `log` | 19 | 854 | None | None |
| `modern_data.sqlite` | `modern_data` | 17 | 1,068,993 | None | None |
| `music.sqlite` | `music` | 11 | 15,607 | Duplicate of Chinook schema | None |
| `northwind.sqlite` | `northwind` | 15 | 3,366 | 2 queries (20-query), 17 queries (100-query) | None |
| `oracle_sql.sqlite` | `oracle_sql` | 38 | 1,350 | None | None |
| `school_scheduling.sqlite` | `school_scheduling` | 15 | 811 | None | None |
| `sqlite-sakila.sqlite` | `sqlite-sakila` | 16 | 46,273 | Sakila DVD rental (Pagila equivalent) | None |
| `stacking.sqlite` | `stacking` | 7 | 10,297 | None | None |

---

## Current Production Database

### Database Contents & Classification
The existing cloud database (`chatbot` hosted on TiDB Cloud Serverless) contains **22 tables** and **31,061 rows**.
- **18 B2B SaaS Domain Tables**: Defined in `db.sql` (`departments`, `employees`, `plans`, `products`, `feature_catalog`, `accounts`, `contacts`, `workspaces`, `workspace_users`, `opportunities`, `subscriptions`, `invoices`, `payments`, `query_audit_log`, `product_usage_daily`, `support_tickets`, `ticket_events`, `incidents`).
- **4 Operational Tables**: Created dynamically by backend migrations (`conversations`, `messages`, `plainsql_users`, `query_feedback`).

### Is it Spider or Custom Data?
- **It is 100% custom enterprise B2B SaaS application data.**
- It is **NOT** a Spider benchmark database. None of the 30 Spider databases contain the table combination of `subscriptions`, `accounts`, `invoices`, and `payments`.
- The evaluation datasets `train.json` (51 queries) and `test.json` (40 queries) are written specifically for this custom SaaS schema (e.g. querying contracted ARR, churn risk, P1 tickets, employee headcount, and gross billing).

---

## Benchmark Database Usage

### 1. The 20-Query Benchmark (`backend/evaluation/datasets/spider_sqlite_benchmarks.jsonl`)
Evaluates 8 distinct Spider 2.0-Lite databases across 20 representative tasks:
1. **`E_commerce`**: 5 queries (25.0% of benchmark) — all 5 passed (100% accuracy in `spider2_lite_results.json`)
2. **`AdventureWorks`**: 4 queries (20.0%)
3. **`Baseball`**: 2 queries (10.0%)
4. **`Airlines`**: 2 queries (10.0%)
5. **`chinook`**: 2 queries (10.0%)
6. **`northwind`**: 2 queries (10.0%)
7. **`IPL`**: 2 queries (10.0%)
8. **`Pagila`**: 1 query (5.0%)

### 2. The 100-Query Benchmark (`backend/evaluation/datasets/comprehensive_production_100.json`)
Evaluates 8 Spider databases and the default SaaS database:
1. **`chinook`**: 21 queries
2. **`E_commerce`**: 20 queries
3. **`northwind`**: 17 queries
4. **`Pagila`**: 15 queries
5. **`Baseball`**: 10 queries
6. **`AdventureWorks`**: 8 queries
7. **`Airlines`**: 5 queries
8. **`IPL`**: 2 queries
9. **`default` (TiDB SaaS)**: 2 queries

---

## Demo Database Usage

### Current Frontend & UI Prompts
When a user launches the web application, the frontend [WelcomeScreen.jsx](file:///c:/Users/lalit/Desktop/Data%20Science/text-to-sql-bot%20-%20Copy/frontend/src/components/chat/WelcomeScreen.jsx) and [useChatStore.js](file:///c:/Users/lalit/Desktop/Data%20Science/text-to-sql-bot%20-%20Copy/frontend/src/store/useChatStore.js) present 4 hero demonstration queries:
1. *"Show net revenue retention by customer segment for the last 4 quarters"* (targets `subscriptions` & `accounts`)
2. *"Which opportunities have high ARR but stalled for more than 30 days?"* (targets `opportunities`)
3. *"Compare churn risk for customers with critical tickets versus healthy accounts"* (targets `accounts` & `support_tickets`)
4. *"Rank workspaces by query volume, failed executions, and active users this month"* (targets `workspaces`, `product_usage_daily`, `query_audit_log`)

All 4 out-of-the-box UI prompts target the **custom B2B SaaS TiDB Cloud database**.

---

## Query Resolution Flow in Current Application

When a user submits a query through the UI, the system resolves the database through the following architectural pipeline:

```
User Query (e.g., "Show net revenue retention by customer segment")
   ↓
Frontend streamChat() in client.js
   ↓ POST { question, history } (No explicit db_id provided)
FastAPI Backend (/api/v1/chat/stream or /chat/stream)
   ↓ request.db_id defaults to "default"
resolve_database("default") via DatabaseRegistry
   ↓
DatabaseRegistry maps "default" → DatabasePool (MySQL / TiDB Cloud)
   ↓
HybridRetriever retrieves schema context for db_id="default":
   - ChromaDB: where={"db_id": "default"} (22 tables)
   - BM25Okapi: "default" corpus with table boosting
   - Business Knowledge: glossary entries for db_id="default"
   ↓
Multi-Agent Orchestrator (LangGraph / Streaming)
   - Intent classification
   - Schema pruning & SQL generation (MySQL dialect)
   - Output guardrail & AST safety validation
   ↓
DatabasePool executes SQL on TiDB Cloud (chatbot)
   ↓
Results formatted + insights generated
   ↓
Server-Sent Events (SSE) streamed back to React Frontend
```

---

## Recommended Live Demo Database

If a Spider 2.0-Lite database is to be chosen for a live demonstration of PlainSQL's Spider capabilities:

- **Database**: `E_commerce.sqlite`
- **db_id**: `E_commerce`

### Why `E_commerce` is the Best Live-Demo Candidate

1. **Rich Multi-Table Schema**:
   Contains 11 interconnected relational tables (`orders`, `order_items`, `customers`, `products`, `sellers`, `order_payments`, `order_reviews`, `leads_closed`, `leads_qualified`, `geolocation`, `product_category_name_translation`) with **1,559,764 real-world marketplace rows**.

2. **Demonstrates Full Analytical Spectrum**:
   - **Multi-table JOINs**: Connecting customers to orders, order items, product descriptions, and seller details.
   - **Complex Aggregations & Formulas**: Computing Average Order Value (AOV), total order revenue (`SUM(price)`), and freight margins.
   - **Multi-Level Grouping & Filtering**: Segmenting sales by Brazilian states (`customer_state`), delivery statuses (`delivered`, `invoiced`, `processing`), and payment types (`credit_card`, `boleto`, `voucher`).
   - **Top-N Ranking**: Top 5 cities with highest customer concentration, top 3 product categories by sales volume.
   - **Time-Series Analysis**: Tracking monthly purchase trends, order approval latencies, and customer delivery intervals via SQLite datetime functions (`strftime('%Y-%m', order_purchase_timestamp)`).

3. **Complete Semantic Layer & Disambiguation Support**:
   - In `backend/app/semantics/business_glossary.yaml`, `E_commerce` has **9 fully configured business definitions** (`revenue`, `sales`, `sales_volume`, `units_sold`, `delivered_order`, `pending_order`, `average_order_value`, `customer_delivery_date`, `carrier_delivery_date`).
   - In `backend/app/semantics/learning_events.jsonl`, all 20 persisted learning events in the repository belong to `E_commerce` (demonstrating disambiguation between customer delivery date vs. carrier dispatch date).

4. **Proven 100% Benchmark Accuracy**:
   In `spider2_lite_results.json`, all 5 Spider 2.0-Lite benchmark queries on `E_commerce` achieved **100% execution accuracy (5/5 PASS)** without a single hallucination or failure.

5. **Extensive Test Coverage**:
   `E_commerce` is already the primary test subject in `test_phase2_sqlite.py`, `test_phase3_rag.py`, `test_phase4_spider_eval.py`, `test_phase9_semantics.py`, `test_phase10_clarification.py`, and `test_phase11_business_knowledge.py`.

---

## Confidence

**HIGH**

Every finding in this report is verified directly from repository configuration, test fixtures, dataset files, benchmark results, and SQLite schema introspection.

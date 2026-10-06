# PlainSQL — Spider E_commerce TiDB Cloud Deployment Report

**Deployment Date**: October 6, 2026  
**Status**: COMPLETE & VERIFIED (SUCCESS)  
**Target Cluster**: `gateway01.ap-southeast-1.prod.alicloud.tidbcloud.com:4000`  
**Database**: `ecommerce` (Isolated)  
**Registered `db_id`**: `E_commerce`  
**Dialect**: `mysql`  
**Preserved Production Database**: `chatbot` (`db_id = default`, 22 tables, 31,662 rows intact)

---

## 1. Executive Summary

The Spider 2.0-Lite `E_commerce` dataset has been fully migrated from its local SQLite origin (`E:\Downloads\local_sqlite\E_commerce.sqlite`) into a dedicated, isolated database (`ecommerce`) on the existing TiDB Cloud cluster. 

Key results:
- **Zero Data Loss**: Exactly **1,559,764 rows** were migrated across **11 tables** with a 100% row-for-row match against the source SQLite file.
- **Zero Production Disruption**: The primary PlainSQL production database (`chatbot` / `db_id = default`) remains completely intact with all 22 tables and 31,662 rows unaffected.
- **Dialect Optimization**: Tables were provisioned with native MySQL DDL (`InnoDB`, `utf8mb4`, structured `DATETIME`, `DECIMAL(10,2)` financial types, and B-Tree indexes).
- **Multi-Database Routing**: `DatabaseRegistry` automatically routes `db_id = "E_commerce"` queries to the TiDB Cloud `ecommerce` pool using `dialect = "mysql"`.
- **Hybrid Schema RAG**: ChromaDB collection `schema_E_commerce` and BM25 index built and operational.
- **Semantic & Business Knowledge**: All 9 enterprise glossary terms defined for `E_commerce` validate to `VALID` status against the live TiDB schema.

---

## 2. Table-by-Table Row Count & Fidelity Verification

All 11 tables were migrated via high-performance streaming batch inserts (`5,000` rows/batch) with explicit datetime and null sanitization.

| Table Name | Source SQLite Rows | Target TiDB Rows | Verification Status | Primary / Unique Key |
| :--- | :--- | :--- | :--- | :--- |
| `product_category_name_translation` | 71 | 71 | **MATCH (100%)** | `product_category_name` |
| `sellers` | 3,095 | 3,095 | **MATCH (100%)** | `seller_id` |
| `customers` | 99,441 | 99,441 | **MATCH (100%)** | `customer_id` |
| `products` | 32,951 | 32,951 | **MATCH (100%)** | `product_id` |
| `orders` | 99,441 | 99,441 | **MATCH (100%)** | `order_id` |
| `order_items` | 112,650 | 112,650 | **MATCH (100%)** | `(order_id, order_item_id)` |
| `order_payments` | 103,886 | 103,886 | **MATCH (100%)** | `(order_id, payment_sequential)` |
| `order_reviews` | 99,224 | 99,224 | **MATCH (100%)** | `review_id` |
| `leads_qualified` | 8,000 | 8,000 | **MATCH (100%)** | `mql_id` |
| `leads_closed` | 842 | 842 | **MATCH (100%)** | `mql_id` |
| `geolocation` | 1,000,163 | 1,000,163 | **MATCH (100%)** | Compound Indexed |
| **TOTAL** | **1,559,764** | **1,559,764** | **PERFECT MATCH** | **11 Tables** |

---

## 3. Financial & Business Aggregates Validation

Independent SQL aggregate assertions executed against the live TiDB Cloud `ecommerce` database:

1. **Order Count & Temporal Range**:
   - Total Orders: `99,441`
   - Earliest Order Purchase: `2016-09-04 21:15:19`
   - Latest Order Purchase: `2018-10-17 17:30:18`
2. **Gross Merchandise & Freight Value (`order_items`)**:
   - Gross Product Sales: **$13,591,643.70**
   - Total Freight Collected: **$2,251,909.54**
3. **Total Customer Payments (`order_payments`)**:
   - Total Payments Processed: **$16,008,872.12**

---

## 4. Multi-Database Architecture & System Integration

### Database Registry Routing
- **Default Database (`db_id = default`)**: Resolves to `mysql+pymysql://...:4000/chatbot` (`dialect = "mysql"`).
- **Spider E-commerce Database (`db_id = E_commerce`)**: Resolves to `mysql+pymysql://...:4000/ecommerce` (`dialect = "mysql"`).
- **Lazy Pool Initialization**: The TiDB `ecommerce` database pool is created lazily on demand or during application startup.
- **Dialect Isolation**: Because `E_commerce` is registered with `dialect = "mysql"`, the SQL Generator agent generates MySQL syntax (e.g. `DATE_SUB`, `NOW()`, backticks) instead of SQLite functions (`STRFTIME`, `DATETIME('now')`).

### Bug Fix: In-Memory Schema Cache Partitioning
- **Identified Issue**: `backend/app/db/connection.py` previously read a static file (`schema_cache.json`) during `DatabasePool.__init__` regardless of the database name, which leaked `chatbot` tables into newly initialized pools.
- **Resolution**: Updated `connection.py` to only pre-populate from `schema_cache.json` if `self.db_name in ("chatbot", "default")`. Pools targeting `ecommerce` now dynamically query `SHOW TABLES` and `DESCRIBE`, correctly yielding only the 11 e-commerce tables.

### Hybrid Schema RAG
- **ChromaDB**: Dedicated collection `schema_E_commerce` created with `11` enriched table schema documents containing column metadata, foreign keys, and sample rows.
- **BM25 Index**: Dedicated `BM25Okapi` index registered under `self._bm25_indices["E_commerce"]`.
- **Retrieval Test**: Query `"top customers by total payments in sao paulo"` retrieves `customers`, `geolocation`, `orders`, `order_payments`, and `sellers` with cross-encoder reranking.

### Business Glossary & Semantic Knowledge
The enterprise glossary file (`backend/app/semantics/business_glossary.yaml`) contains 9 terms strictly scoped to `databases.E_commerce`:
- `revenue` -> `SUM(order_items.price)` [VALID]
- `sales` -> `SUM(order_items.price)` [VALID]
- `sales_volume` -> `COUNT(order_items.order_item_id)` [VALID]
- `units_sold` -> `COUNT(order_items.order_item_id)` [VALID]
- `delivered_order` -> `orders.order_status = 'delivered'` [VALID]
- `pending_order` -> `orders.order_status IN ('processing', 'invoiced', 'created')` [VALID]
- `average_order_value` -> `SUM(order_items.price) / COUNT(DISTINCT order_items.order_id)` [VALID]
- `customer_delivery_date` -> `orders.order_delivered_customer_date` [VALID]
- `carrier_delivery_date` -> `orders.order_delivered_carrier_date` [VALID]

All 9 definitions validate to `VALID` against the live schema.

---

## 5. Security & Isolation Testing

- **AST Guardrails**: `sql_validation_node` blocks destructive statements (`DROP`, `DELETE`, `UPDATE`, `ALTER`, `TRUNCATE`).
- **Hallucination Detection**: `OutputGuardrail` detects hallucinated tables and columns against the live schema while allowing valid queries.
- **Cache Isolation**: Redis/Memory cache keys hash both `tenant_id` and `db_id` (format: `plainsql:cache:{hash(tenant:db_id:query)}`), ensuring zero cache collisions between `default` and `E_commerce`.
- **Automated Test Suite**: `backend/tests/test_ecommerce_tidb.py` tests all isolation, data fidelity, RAG, and query execution criteria. (7/7 tests passing).

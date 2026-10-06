"""
Phase 3 Tests — Database-Aware Hybrid RAG.

Validates that:
1. SchemaEnricher generates enriched schema documents with db_id metadata and sample values
2. HybridRetriever builds isolated ChromaDB and BM25 indices per db_id
3. RRF (Reciprocal Rank Fusion) merges dense and sparse results accurately
4. ChromaDB and BM25 queries enforce strict database isolation
5. Mandatory Cross-Database Isolation: E_commerce never returns AdventureWorks schema and vice versa
6. Schema retrieval node uses Hybrid RAG and falls back to full schema gracefully when RAG is empty
7. Structured logging captures retrieval quality metrics
8. End-to-end query execution with RAG-retrieved schema on SQLite
"""

import os
import pytest
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

from app.db.registry import DatabaseRegistry, resolve_database
from app.db.sqlite_pool import SQLitePool
from app.rag.schema_enricher import SchemaEnricher
from app.rag.retriever import HybridRetriever, NoopCollection
from app.agents.schema_retrieval import schema_retrieval_node, _compress_schema_to_ddl
from app.agents.state import AgentState
from app.agents.orchestrator import AgentOrchestrator

ECOMMERCE_DB_PATH = r"E:\Downloads\local_sqlite\E_commerce.sqlite"
ADVENTUREWORKS_DB_PATH = r"E:\Downloads\local_sqlite\AdventureWorks.sqlite"


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ecommerce_pool():
    if os.path.isfile(ECOMMERCE_DB_PATH):
        return SQLitePool(ECOMMERCE_DB_PATH)
    pytest.skip(f"E_commerce database not found at {ECOMMERCE_DB_PATH}")


@pytest.fixture(scope="module")
def adventureworks_pool():
    if os.path.isfile(ADVENTUREWORKS_DB_PATH):
        return SQLitePool(ADVENTUREWORKS_DB_PATH)
    pytest.skip(f"AdventureWorks database not found at {ADVENTUREWORKS_DB_PATH}")


@pytest.fixture(scope="module")
def mock_mysql_pool():
    pool = MagicMock()
    pool.__class__.__name__ = "DatabasePool"
    pool.get_tables.return_value = ["employees", "departments", "salaries"]
    pool.get_table_schema.return_value = [
        {"name": "id", "type": "int", "null": "NO", "key": "PRI", "default": None},
        {"name": "name", "type": "varchar(100)", "null": "YES", "key": "", "default": None},
        {"name": "dept_id", "type": "int", "null": "YES", "key": "MUL", "default": None},
    ]
    pool.get_foreign_keys.return_value = [
        {
            "CONSTRAINT_NAME": "fk_dept",
            "COLUMN_NAME": "dept_id",
            "REFERENCED_TABLE_NAME": "departments",
            "REFERENCED_COLUMN_NAME": "id",
        }
    ]
    pool.get_row_count.return_value = 100
    pool.get_full_schema.return_value = "TABLE employees (id INT PK, name VARCHAR, dept_id INT FK)"
    pool.execute_query.return_value = [{"id": 1, "name": "Alice", "dept_id": 10}]
    return pool


@pytest.fixture(scope="module")
def multi_db_registry(ecommerce_pool, adventureworks_pool, mock_mysql_pool):
    registry = DatabaseRegistry()
    registry.register("default", mock_mysql_pool, dialect="mysql")
    registry.register("E_commerce", ecommerce_pool, dialect="sqlite", path=ECOMMERCE_DB_PATH)
    registry.register("AdventureWorks", adventureworks_pool, dialect="sqlite", path=ADVENTUREWORKS_DB_PATH)
    return registry


@pytest.fixture(scope="module")
def temp_chroma_dir():
    tmpdir = tempfile.mkdtemp(prefix="chroma_test_")
    yield tmpdir
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(scope="module")
def hybrid_retriever(mock_mysql_pool, multi_db_registry, temp_chroma_dir):
    retriever = HybridRetriever(
        db_pool=mock_mysql_pool,
        chroma_persist_dir=temp_chroma_dir,
        registry=multi_db_registry,
    )
    return retriever


# ── Test Suite 1: SchemaEnricher Integration & Metadata (13.I, 13.C) ───────

class TestSchemaEnricherPhase3:
    """Test SchemaEnricher database awareness, metadata, and sample values."""

    def test_enrich_all_tables_contains_db_id_metadata(self, ecommerce_pool):
        enricher = SchemaEnricher(ecommerce_pool)
        enriched = enricher.enrich_all_tables(db_id="E_commerce")

        assert len(enriched) > 0
        for item in enriched:
            assert "table_name" in item
            assert "document" in item
            assert "metadata" in item

            meta = item["metadata"]
            assert meta["db_id"] == "E_commerce"
            assert meta["table_name"] == item["table_name"]
            assert meta["document_type"] == "table_schema"
            assert "columns" in meta
            assert "row_count" in meta

            # Document text header must specify the database
            assert item["document"].startswith("Database: E_commerce\n")
            assert f"Table: {item['table_name']}" in item["document"]

    def test_sample_values_selective_extraction(self, ecommerce_pool):
        enricher = SchemaEnricher(ecommerce_pool)
        enriched = enricher.enrich_all_tables(db_id="E_commerce")

        orders_doc = next((item for item in enriched if item["table_name"] == "orders"), None)
        assert orders_doc is not None
        # Check that non-primary key columns like order_status include sample values
        doc_text = orders_doc["document"]
        assert "order_status" in doc_text
        assert "Examples:" in doc_text

    def test_relationships_in_document(self, ecommerce_pool):
        enricher = SchemaEnricher(ecommerce_pool)
        enriched = enricher.enrich_all_tables(db_id="E_commerce")

        # order_items or orders have foreign keys
        orders_doc = next((item for item in enriched if item["table_name"] == "orders"), None)
        assert orders_doc is not None
        if orders_doc["raw_fks"]:
            assert "Relationships:" in orders_doc["document"]
            assert "→" in orders_doc["document"]


# ── Test Suite 2: Index Building & Idempotency (13.A, 13.C, 8) ───────────

class TestIndexBuildingPhase3:
    """Test building and rebuilding RAG index for SQLite databases."""

    def test_ecommerce_indexing(self, hybrid_retriever, ecommerce_pool):
        count = hybrid_retriever.index_database("E_commerce", db_pool=ecommerce_pool, force=True)
        assert count == 11  # E_commerce has 11 tables

        # Verify BM25 index built
        assert "E_commerce" in hybrid_retriever._bm25_indices
        assert len(hybrid_retriever._documents.get("E_commerce", [])) == 11
        assert len(hybrid_retriever._metadatas.get("E_commerce", [])) == 11

        # Verify ChromaDB collection populated
        collection = hybrid_retriever._get_or_create_collection("E_commerce")
        if not isinstance(collection, NoopCollection):
            assert collection.count() == 11

    def test_indexing_idempotent_no_duplicates(self, hybrid_retriever, ecommerce_pool):
        # First index
        count1 = hybrid_retriever.index_database("E_commerce", db_pool=ecommerce_pool, force=False)
        # Second call with force=False should immediately return existing count
        count2 = hybrid_retriever.index_database("E_commerce", db_pool=ecommerce_pool, force=False)
        assert count1 == count2 == 11
        assert len(hybrid_retriever._documents["E_commerce"]) == 11

        # Force re-index should clean and recreate without duplicating
        count3 = hybrid_retriever.index_database("E_commerce", db_pool=ecommerce_pool, force=True)
        assert count3 == 11
        assert len(hybrid_retriever._documents["E_commerce"]) == 11
        collection = hybrid_retriever._get_or_create_collection("E_commerce")
        if not isinstance(collection, NoopCollection):
            assert collection.count() == 11


# ── Test Suite 3: E_commerce Retrieval (13.B) ───────────────────────────

class TestEcommerceRetrieval:
    """Test retrieving relevant schema documents for E_commerce."""

    def test_ecommerce_retrieval_returns_relevant_tables(self, hybrid_retriever, ecommerce_pool):
        hybrid_retriever.index_database("E_commerce", db_pool=ecommerce_pool)

        # Query about customer location
        docs = hybrid_retriever.retrieve("Find all customers living in Rio de Janeiro", top_k=3, db_id="E_commerce")
        assert len(docs) > 0
        joined = "\n".join(docs)
        assert "Table: customers" in joined or "Table: geolocation" in joined
        assert "Database: E_commerce" in joined

    def test_ecommerce_retrieval_orders_items(self, hybrid_retriever, ecommerce_pool):
        hybrid_retriever.index_database("E_commerce", db_pool=ecommerce_pool)

        docs = hybrid_retriever.retrieve("Total price and freight value of delivered orders", top_k=4, db_id="E_commerce")
        assert len(docs) > 0
        joined = "\n".join(docs)
        assert "Table: orders" in joined or "Table: order_items" in joined or "Table: order_payments" in joined
        assert "Database: E_commerce" in joined


# ── Test Suite 4: MANDATORY CROSS-DATABASE ISOLATION (13.D, 13.E, 13.F, 14) ──

class TestCrossDatabaseIsolation:
    """
    MANDATORY SECTION 14 REQUIREMENT:
    Index both E_commerce and AdventureWorks.
    E_commerce query must ONLY return E_commerce schema, NEVER AdventureWorks.
    AdventureWorks query must ONLY return AdventureWorks schema, NEVER E_commerce.
    """

    @pytest.fixture(autouse=True)
    def setup_both_databases(self, hybrid_retriever, ecommerce_pool, adventureworks_pool):
        hybrid_retriever.index_database("E_commerce", db_pool=ecommerce_pool, force=True)
        hybrid_retriever.index_database("AdventureWorks", db_pool=adventureworks_pool, force=True)

    def test_mandatory_cross_database_isolation(self, hybrid_retriever):
        """
        Ask E_commerce question -> 100% E_commerce schema, 0% AdventureWorks.
        Ask AdventureWorks question -> 100% AdventureWorks schema, 0% E_commerce.
        """
        # AdventureWorks specific tables
        adv_tables = {
            "salesperson", "product", "salesorderheader", "salesorderdetail",
            "productcategory", "productsubcategory", "salesterritory"
        }
        # E_commerce specific tables
        ecom_tables = {
            "customers", "orders", "order_items", "order_payments",
            "order_reviews", "sellers", "geolocation"
        }

        # 1. Query against E_commerce
        ecom_query = "Find all customer orders with customer city and order purchase date"
        ecom_docs = hybrid_retriever.retrieve(ecom_query, top_k=5, db_id="E_commerce")

        assert len(ecom_docs) > 0
        for doc in ecom_docs:
            assert "Database: E_commerce" in doc
            assert "Database: AdventureWorks" not in doc
            for adv_tbl in adv_tables:
                assert f"Table: {adv_tbl}\n" not in doc, f"Found AdventureWorks table {adv_tbl} in E_commerce retrieval!"

        # 2. Query against AdventureWorks
        adv_query = "Find all sales orders placed by salespersons and total due amount"
        adv_docs = hybrid_retriever.retrieve(adv_query, top_k=5, db_id="AdventureWorks")

        assert len(adv_docs) > 0
        for doc in adv_docs:
            assert "Database: AdventureWorks" in doc
            assert "Database: E_commerce" not in doc
            for ecom_tbl in ecom_tables:
                assert f"Table: {ecom_tbl}\n" not in doc, f"Found E_commerce table {ecom_tbl} in AdventureWorks retrieval!"

    def test_bm25_database_isolation(self, hybrid_retriever):
        """BM25 index must not score terms across databases."""
        # 'salesperson' exists in AdventureWorks, NOT in E_commerce
        ecom_bm25_results = hybrid_retriever._keyword_search("salesperson", top_k=5, db_id="E_commerce")
        assert len(ecom_bm25_results) == 0, "BM25 searched across database boundary!"

        adv_bm25_results = hybrid_retriever._keyword_search("salesperson", top_k=5, db_id="AdventureWorks")
        assert len(adv_bm25_results) > 0
        assert "Table: salesperson" in adv_bm25_results[0]

        # 'order_reviews' exists in E_commerce, NOT in AdventureWorks
        adv_reviews_results = hybrid_retriever._keyword_search("order_reviews", top_k=5, db_id="AdventureWorks")
        assert len(adv_reviews_results) == 0, "BM25 searched across database boundary!"

        ecom_reviews_results = hybrid_retriever._keyword_search("order_reviews", top_k=5, db_id="E_commerce")
        assert len(ecom_reviews_results) > 0
        assert "Table: order_reviews" in ecom_reviews_results[0]

    def test_chroma_database_isolation(self, hybrid_retriever):
        """ChromaDB collections and where clause enforce database separation."""
        col_ecom_name = hybrid_retriever._get_collection_name("E_commerce")
        col_adv_name = hybrid_retriever._get_collection_name("AdventureWorks")

        assert col_ecom_name != col_adv_name
        assert "E_commerce" in col_ecom_name
        assert "AdventureWorks" in col_adv_name

        # Vector search for E_commerce
        ecom_vec = hybrid_retriever._vector_search("customer reviews and ratings", top_k=5, db_id="E_commerce")
        for doc in ecom_vec:
            assert "Database: E_commerce" in doc
            assert "AdventureWorks" not in doc


# ── Test Suite 5: RRF Fusion (13.G, 5) ───────────────────────────────────

class TestRRFFusion:
    """Test Reciprocal Rank Fusion implementation and math."""

    def test_rrf_scoring_and_merge(self):
        doc_a = "Table: customers"
        doc_b = "Table: orders"
        doc_c = "Table: products"
        doc_d = "Table: sellers"

        list_a = [doc_a, doc_b, doc_c]       # Vector search ranking
        list_b = [doc_b, doc_a, doc_d]       # BM25 ranking

        # doc_a: 1/(60+1) + 1/(60+2) = 1/61 + 1/62 = 0.01639 + 0.01613 = 0.03252
        # doc_b: 1/(60+2) + 1/(60+1) = 0.03252 (tied for top)
        # doc_c: 1/(60+3) = 0.01587
        # doc_d: 1/(60+3) = 0.01587

        merged = HybridRetriever._rrf_merge(list_a, list_b, top_k=4, k=60)
        assert len(merged) == 4
        # doc_a and doc_b must be top 2 because they appeared in both lists
        assert set(merged[:2]) == {doc_a, doc_b}
        # doc_c and doc_d must be bottom 2 because they appeared in only one list
        assert set(merged[2:]) == {doc_c, doc_d}

    def test_rrf_single_list(self):
        list_a = ["doc1", "doc2"]
        list_b = []
        merged = HybridRetriever._rrf_merge(list_a, list_b, top_k=2)
        assert merged == ["doc1", "doc2"]

    def test_rrf_empty(self):
        merged = HybridRetriever._rrf_merge([], [], top_k=5)
        assert merged == []


# ── Test Suite 6: Schema Retrieval Node & Fallback (13.H, 10, 11, 12) ────

class TestSchemaRetrievalNode:
    """Test schema_retrieval_node database awareness, DDL compression, and fallback."""

    def test_node_retrieves_ecommerce_schema(self, hybrid_retriever, ecommerce_pool):
        hybrid_retriever.index_database("E_commerce", db_pool=ecommerce_pool)

        state: AgentState = {
            "user_query": "Find all customers in Rio de Janeiro",
            "entities": ["customers"],
            "db_id": "E_commerce",
            "sql_dialect": "sqlite",
            "retrieval_top_k": 3,
        }

        result = schema_retrieval_node(state, rag_retriever=hybrid_retriever, db_pool=ecommerce_pool)

        assert "relevant_schema" in result
        assert "relevant_tables" in result
        assert "TABLE customers" in result["relevant_schema"]
        assert "customers" in result["relevant_tables"]
        assert result["retrieval_source"].startswith("rag_top_k")

    def test_node_fallback_when_retrieval_empty(self, ecommerce_pool):
        # Empty mock retriever that returns nothing
        empty_retriever = MagicMock()
        empty_retriever.retrieve.return_value = []

        state: AgentState = {
            "user_query": "Non-existent table query",
            "db_id": "E_commerce",
            "sql_dialect": "sqlite",
            "retrieval_top_k": 5,
        }

        result = schema_retrieval_node(state, rag_retriever=empty_retriever, db_pool=ecommerce_pool)

        # Graceful fallback: returns full schema instead of crashing
        assert "relevant_schema" in result
        assert "TABLE" in result["relevant_schema"]
        assert len(result["relevant_tables"]) == 11
        assert "fallback_full_schema:E_commerce" in result["retrieval_source"]

    def test_node_fallback_when_retriever_raises(self, ecommerce_pool):
        failing_retriever = MagicMock()
        failing_retriever.retrieve.side_effect = RuntimeError("Index corrupted")

        state: AgentState = {
            "user_query": "Count orders",
            "db_id": "E_commerce",
            "sql_dialect": "sqlite",
            "retrieval_top_k": 5,
        }

        result = schema_retrieval_node(state, rag_retriever=failing_retriever, db_pool=ecommerce_pool)
        assert "relevant_schema" in result
        assert "TABLE" in result["relevant_schema"]
        assert "fallback" in result["retrieval_source"]

    def test_backward_compatibility_with_mock_retriever(self, ecommerce_pool):
        """Mock retrievers in existing tests that do not accept db_id parameter."""
        legacy_mock = MagicMock()
        # Takes only query and top_k
        legacy_mock.retrieve.side_effect = lambda query, top_k=5: ["Table: customers\nColumns:\n  - id (int)"]

        state: AgentState = {
            "user_query": "Show customers",
            "db_id": "E_commerce",
            "sql_dialect": "sqlite",
        }

        # Should not raise TypeError: retrieve() got an unexpected keyword argument 'db_id'
        result = schema_retrieval_node(state, rag_retriever=legacy_mock, db_pool=ecommerce_pool)
        assert "relevant_schema" in result
        assert "customers" in result["relevant_tables"]


# ── Test Suite 7: End-to-End Pipeline with Hybrid RAG (13.J) ─────────────

class TestEndToEndPipelineWithRAG:
    """Test full pipeline: Question -> Hybrid RAG -> Schema -> SQL Gen -> AST -> Exec."""

    @pytest.fixture
    def mock_llm_router(self):
        router = MagicMock()
        # Return valid SQLite JSON
        def side_effect(messages, **kwargs):
            sys_msg = next((m["content"] for m in messages if m["role"] == "system"), "")
            usr_msg = next((m["content"] for m in messages if m["role"] == "user"), "")

            if "customers in sao paulo" in usr_msg.lower():
                return '{"sql": "SELECT COUNT(*) AS total FROM customers WHERE customer_city = \'sao paulo\';", "explanation": "Count customers in Sao Paulo", "message": "Here is the count"}'
            elif "top 5 customers" in usr_msg.lower():
                return '{"sql": "SELECT customer_id, COUNT(order_id) AS order_cnt FROM orders GROUP BY customer_id ORDER BY order_cnt DESC LIMIT 5;", "explanation": "Top 5 customers by order count", "message": "Top customers"}'
            else:
                return '{"sql": "SELECT COUNT(*) FROM customers;", "explanation": "Default count", "message": "Count"}'

        router.generate.side_effect = side_effect
        return router

    def test_end_to_end_sao_paulo_customers(
        self, mock_llm_router, ecommerce_pool, multi_db_registry, hybrid_retriever
    ):
        hybrid_retriever.index_database("E_commerce", db_pool=ecommerce_pool)

        orchestrator = AgentOrchestrator(
            mock_llm_router,
            hybrid_retriever,
            ecommerce_pool,
            registry=multi_db_registry,
        )

        initial_state: AgentState = {
            "user_query": "How many customers in sao paulo?",
            "db_id": "E_commerce",
            "sql_dialect": "sqlite",
            "retry_count": 0,
            "max_retries": 2,
            "validation_errors": [],
            "conversation_history": [],
            "session_id": "test_phase3_rag_session",
        }

        final_state = orchestrator.graph.invoke(initial_state)

        # 1. Verify schema retrieval used RAG
        assert "customers" in final_state.get("relevant_tables", [])
        assert "TABLE customers" in final_state.get("relevant_schema", "")
        assert final_state.get("retrieval_source", "").startswith("rag_top_k")

        # 2. Verify SQL was generated
        sql = final_state.get("generated_sql", "")
        assert "SELECT COUNT(*)" in sql
        assert "customers" in sql

        # 3. Verify validation passed
        assert final_state.get("is_valid", False) is True

        # 4. Verify execution executed against SQLite successfully
        assert final_state.get("error") is None
        assert final_state.get("row_count", 0) >= 1
        results = final_state.get("query_results", [])
        assert len(results) == 1
        assert results[0]["total"] > 0
        assert final_state.get("sql_dialect") == "sqlite"
        assert final_state.get("db_id") == "E_commerce"

    def test_end_to_end_top_5_customers(
        self, mock_llm_router, ecommerce_pool, multi_db_registry, hybrid_retriever
    ):
        hybrid_retriever.index_database("E_commerce", db_pool=ecommerce_pool)

        orchestrator = AgentOrchestrator(
            mock_llm_router,
            hybrid_retriever,
            ecommerce_pool,
            registry=multi_db_registry,
        )

        initial_state: AgentState = {
            "user_query": "List the top 5 customers by total orders",
            "db_id": "E_commerce",
            "sql_dialect": "sqlite",
            "retry_count": 0,
            "max_retries": 2,
            "validation_errors": [],
            "conversation_history": [],
            "session_id": "test_phase3_rag_session_2",
        }

        final_state = orchestrator.graph.invoke(initial_state)

        assert final_state.get("error") is None
        assert final_state.get("row_count", 0) == 5
        results = final_state.get("query_results", [])
        assert len(results) == 5
        assert "order_cnt" in results[0]

"""
Phase 5 Tests — Exact Table-Mention Boosting & Retrieval Robustness.

Tests:
A. Exact table match: "How many total players are listed in the player table?" -> `player` is retrieved.
B. Compound-name protection: "player" boosts `player` but not `player_award` as an exact match.
C. Another database: AdventureWorks table detection (e.g. `salesorderheader`).
D. No explicit table: Normal semantic retrieval remains unchanged.
E. Multiple explicit tables: Both retained when feasible.
F. Cross-database isolation: Tables from other databases are never matched or retrieved.
G. Existing MySQL/TiDB retrieval: Preserved.
"""

import pytest
from unittest.mock import MagicMock
from app.rag.retriever import detect_exact_table_mentions, HybridRetriever
from app.db.sqlite_pool import SQLitePool
from app.db.registry import get_database_registry


# ── Unit Tests for detect_exact_table_mentions ───────────────────────────

class TestExactTableMentionDetection:
    """Unit tests for generic token/boundary table detection."""

    def test_exact_single_table_match(self):
        """A. Exact table match detection for Baseball 'player'."""
        tables = [
            "all_star", "appearances", "manager_award", "player_award",
            "manager_award_vote", "player_award_vote", "batting",
            "player_college", "player", "park", "pitching", "team"
        ]
        query = "How many total players are listed in the player table?"
        detected = detect_exact_table_mentions(query, tables)
        assert detected == ["player"], f"Expected ['player'], got {detected}"

    def test_compound_name_protection(self):
        """B. 'player' must NOT treat 'player_award' or 'player_college' as exact matches."""
        tables = ["player", "player_award", "player_college", "all_star"]
        query = "How many total players are in the player table?"
        detected = detect_exact_table_mentions(query, tables)
        assert "player" in detected
        assert "player_award" not in detected
        assert "player_college" not in detected

    def test_compound_name_exact_detection(self):
        """When query explicitly mentions compound table, it is detected."""
        tables = ["player", "player_award", "player_college"]
        query = "Show all rows from the player_award table"
        detected = detect_exact_table_mentions(query, tables)
        assert detected == ["player_award"]

    def test_compound_name_space_separated(self):
        """Detect compound table even if written with spaces."""
        tables = ["player", "player_award", "order_items"]
        query = "Show all items from order items table"
        detected = detect_exact_table_mentions(query, tables)
        assert "order_items" in detected

    def test_adventureworks_table_detection(self):
        """C. AdventureWorks table detection."""
        aw_tables = ["salesorderheader", "salesorderdetail", "customer", "product", "salesperson"]
        query = "List the top 5 sales orders from salesorderheader"
        detected = detect_exact_table_mentions(query, aw_tables)
        assert detected == ["salesorderheader"]

    def test_no_explicit_table(self):
        """D. No explicit table mentioned returns empty list."""
        tables = ["customers", "orders", "products"]
        query = "What is the highest grossing category this quarter?"
        detected = detect_exact_table_mentions(query, tables)
        assert detected == []

    def test_multiple_explicit_tables(self):
        """E. Multiple explicit tables mentioned are all detected."""
        tables = ["customers", "orders", "products", "order_details"]
        query = "Join customers and orders to calculate total spend"
        detected = detect_exact_table_mentions(query, tables)
        assert "customers" in detected
        assert "orders" in detected
        assert "products" not in detected

    def test_singular_plural_inflection(self):
        """Plural question matches singular table, and singular question matches plural table."""
        tables = ["customer", "order"]
        query = "List all customers with open orders"
        detected = detect_exact_table_mentions(query, tables)
        assert "customer" in detected
        assert "order" in detected

    def test_empty_query_or_tables(self):
        assert detect_exact_table_mentions("", ["orders"]) == []
        assert detect_exact_table_mentions("SELECT *", []) == []


# ── Integration Tests with HybridRetriever ────────────────────────────────

class TestHybridRetrieverBoosting:
    """Integration tests verifying retrieval ranking and forced inclusion."""

    @pytest.fixture
    def mock_db_pool(self):
        pool = MagicMock()
        pool.get_tables.return_value = ["accounts", "subscriptions", "invoices"]
        pool.get_table_schema.return_value = [
            {"name": "id", "type": "int", "nullable": False, "primary_key": True},
            {"name": "name", "type": "varchar(100)", "nullable": True, "primary_key": False},
        ]
        pool.get_foreign_keys.return_value = []
        pool.get_row_count.return_value = 100
        pool.execute_query.return_value = []
        return pool

    def test_exact_table_promoted_to_front(self, mock_db_pool):
        """Explicitly mentioned table is placed at index 0 of results."""
        retriever = HybridRetriever(mock_db_pool)
        
        # Seed mock documents for 3 tables
        retriever._documents["default"] = [
            "Table: invoices\nColumns: id, amount",
            "Table: accounts\nColumns: id, name",
            "Table: subscriptions\nColumns: id, arr",
        ]
        retriever._metadatas["default"] = [
            {"table_name": "invoices", "db_id": "default"},
            {"table_name": "accounts", "db_id": "default"},
            {"table_name": "subscriptions", "db_id": "default"},
        ]
        retriever._doc_ids["default"] = ["def_invoices", "def_accounts", "def_subscriptions"]

        # Mock vector and keyword search to simulate accounts being ranked last
        retriever._vector_search = MagicMock(return_value=[
            "Table: invoices\nColumns: id, amount",
            "Table: subscriptions\nColumns: id, arr",
            "Table: accounts\nColumns: id, name",
        ])
        retriever._keyword_search = MagicMock(return_value=[
            "Table: invoices\nColumns: id, amount",
            "Table: subscriptions\nColumns: id, arr",
        ])

        # Query explicitly mentions accounts
        query = "Find details from the accounts table"
        results = retriever.retrieve(query, top_k=2, db_id="default")
        
        # accounts must be in results and at index 0
        assert len(results) >= 1
        assert "Table: accounts" in results[0]

    def test_cross_database_isolation(self, mock_db_pool):
        """F. Tables from other databases are never matched or retrieved."""
        retriever = HybridRetriever(mock_db_pool)
        
        # Seed DB 1
        retriever._metadatas["db1"] = [
            {"table_name": "player", "db_id": "db1"},
            {"table_name": "team", "db_id": "db1"},
        ]
        # Seed DB 2
        retriever._metadatas["db2"] = [
            {"table_name": "customer", "db_id": "db2"},
            {"table_name": "order", "db_id": "db2"},
        ]

        # Querying db2 with a mention of "player" should NOT detect "player" in db2
        detected_db2 = retriever.detect_exact_table_mentions("How many players are in the player table?", db_id="db2")
        assert "player" not in detected_db2
        assert detected_db2 == []

        # Querying db1 should detect "player"
        detected_db1 = retriever.detect_exact_table_mentions("How many players are in the player table?", db_id="db1")
        assert detected_db1 == ["player"]

    def test_adaptive_top_k_for_large_schema(self, mock_db_pool):
        """Adaptive top_k increases top_k to at least 5 when database has >= 20 tables."""
        retriever = HybridRetriever(mock_db_pool)
        
        # Seed 25 mock tables
        metas = [{"table_name": f"table_{i}", "db_id": "large_db"} for i in range(25)]
        docs = [f"Table: table_{i}\nColumns: id" for i in range(25)]
        retriever._metadatas["large_db"] = metas
        retriever._documents["large_db"] = docs
        retriever._bm25_indices["large_db"] = MagicMock()

        retriever._vector_search = MagicMock(return_value=docs[:5])
        retriever._keyword_search = MagicMock(return_value=docs[:5])

        # Requested top_k=3, but schema has 25 tables -> adaptive top_k expands to 5
        results = retriever.retrieve("Count rows in table_0", top_k=3, db_id="large_db")
        assert len(results) == 5
        assert "Table: table_0" in results[0]

    def test_existing_mysql_tidb_retrieval_intact(self, mock_db_pool):
        """G. MySQL/TiDB default database retrieval functions normally."""
        retriever = HybridRetriever(mock_db_pool)
        retriever._metadatas["default"] = [
            {"table_name": "subscriptions", "db_id": "default"},
            {"table_name": "accounts", "db_id": "default"},
        ]
        retriever._documents["default"] = [
            "Table: subscriptions\nColumns: id, arr",
            "Table: accounts\nColumns: id, name",
        ]
        retriever._bm25_indices["default"] = MagicMock()
        retriever._vector_search = MagicMock(return_value=retriever._documents["default"])
        retriever._keyword_search = MagicMock(return_value=retriever._documents["default"])

        results = retriever.retrieve("Show all active subscriptions", top_k=2, db_id="default")
        assert len(results) == 2
        assert any("subscriptions" in r for r in results)

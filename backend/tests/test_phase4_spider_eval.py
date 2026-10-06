"""
Phase 4 Tests — Spider 2.0-Lite Evaluation Harness.

Validates:
1. Spider dataset loader reads and normalizes records correctly
2. Handling of missing/malformed fields and missing gold SQL
3. db_id mapping to SQLitePool via DatabaseRegistry
4. Result normalization and execution accuracy comparison
5. Table extraction from SQL and table recall calculation
6. Failure categorization
7. Metrics aggregation logic
8. Baseline runner execution
9. PlainSQL evaluation runner execution
"""

import os
import pytest
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

from app.db.registry import DatabaseRegistry
from app.db.sqlite_pool import SQLitePool
from evaluation.spider_loader import SpiderDatasetLoader
from evaluation.spider_eval import SpiderEvaluator, extract_tables_from_sql
from evaluation.runner import EvalMetrics

ECOMMERCE_DB_PATH = r"E:\Downloads\local_sqlite\E_commerce.sqlite"


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def sqlite_test_db():
    """Create a temporary SQLite database for unit tests."""
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    conn = sqlite3.connect(tmp.name)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);")
    conn.execute("CREATE TABLE orders (order_id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL);")
    conn.execute("INSERT INTO users VALUES (1, 'Alice', 30), (2, 'Bob', 25);")
    conn.execute("INSERT INTO orders VALUES (101, 1, 99.5), (102, 2, 45.0);")
    conn.commit()
    conn.close()
    yield tmp.name
    try:
        os.remove(tmp.name)
    except Exception:
        pass


@pytest.fixture
def mock_registry(sqlite_test_db):
    reg = DatabaseRegistry()
    pool = SQLitePool(sqlite_test_db)
    reg.register("test_db", pool, dialect="sqlite", path=sqlite_test_db)
    return reg


@pytest.fixture
def mock_llm_router():
    router = MagicMock()
    # Returns valid JSON for PlainSQL
    def side_effect(messages, **kwargs):
        usr_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
        if "user" in usr_msg.lower() or "alice" in usr_msg.lower():
            return '{"sql": "SELECT name, age FROM users WHERE name = \'Alice\';", "explanation": "Find Alice", "message": "Here is Alice"}'
        return '{"sql": "SELECT COUNT(*) AS cnt FROM users;", "explanation": "Count users", "message": "Count"}'

    router.generate.side_effect = side_effect
    return router


# ── Test Suite 1: Dataset Loader & Normalization ─────────────────────────

class TestSpiderDatasetLoader:
    """Test SpiderDatasetLoader normalization, missing fields, and gold SQL handling."""

    def test_normalize_valid_example(self):
        loader = SpiderDatasetLoader()
        raw = {
            "instance_id": "test_001",
            "db_id": "E_commerce",
            "question": "Count total customers",
            "gold_sql": "SELECT COUNT(*) FROM customers;",
            "difficulty": "easy",
            "external_knowledge": "table schema info",
        }
        item = loader.normalize_example(raw)

        assert item["instance_id"] == "test_001"
        assert item["db_id"] == "E_commerce"
        assert item["question"] == "Count total customers"
        assert item["gold_sql"] == "SELECT COUNT(*) FROM customers;"
        assert item["has_gold_sql"] is True
        assert item["external_knowledge"] == "table schema info"
        assert item["difficulty"] == "easy"

    def test_normalize_missing_gold_sql_explicit(self):
        """When gold SQL is missing, has_gold_sql must be False and not invented."""
        loader = SpiderDatasetLoader()
        raw = {
            "instance_id": "local015",
            "db_id": "California_Traffic_Collision",
            "question": "What is the primary collision factor?",
            # gold_sql is omitted
        }
        item = loader.normalize_example(raw)

        assert item["instance_id"] == "local015"
        assert item["db_id"] == "California_Traffic_Collision"
        assert item["gold_sql"] is None
        assert item["has_gold_sql"] is False

    def test_normalize_empty_gold_sql(self):
        loader = SpiderDatasetLoader()
        raw = {
            "instance_id": "local016",
            "db_id": "WWE",
            "question": "List champions",
            "gold_sql": "   ",
        }
        item = loader.normalize_example(raw)
        assert item["gold_sql"] is None
        assert item["has_gold_sql"] is False

    def test_read_jsonl_and_json_structures(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write('{"instance_id": "1", "db_id": "db1", "question": "q1"}\n')
            f.write('{"instance_id": "2", "db_id": "db2", "question": "q2"}\n')
            f_path = f.name

        try:
            items = SpiderDatasetLoader._read_jsonl_or_json(f_path)
            assert len(items) == 2
            assert items[0]["instance_id"] == "1"
            assert items[1]["instance_id"] == "2"
        finally:
            os.remove(f_path)


# ── Test Suite 2: Table Extraction & Table Recall ────────────────────────

class TestTableExtractionAndRecall:
    """Test extracting tables from SQL and calculating table recall."""

    def test_extract_tables_from_simple_sql(self):
        known = ["users", "orders", "products", "departments"]
        sql = "SELECT name FROM users WHERE id = 1;"
        extracted = extract_tables_from_sql(sql, known)
        assert extracted == ["users"]

    def test_extract_tables_from_join_sql(self):
        known = ["users", "orders", "products", "departments"]
        sql = "SELECT u.name, o.amount FROM users u JOIN orders o ON u.id = o.user_id;"
        extracted = extract_tables_from_sql(sql, known)
        assert set(extracted) == {"users", "orders"}

    def test_table_recall_calculation(self):
        gold_tables = ["users", "orders"]
        retrieved_tables = ["users", "orders", "products"]

        gold_set = set(gold_tables)
        retrieved_set = set(retrieved_tables)
        intersection = gold_set.intersection(retrieved_set)

        recall = round(len(intersection) / len(gold_set), 4)
        precision = round(len(intersection) / len(retrieved_set), 4)

        assert recall == 1.0  # 100% recall
        assert precision == round(2 / 3, 4)

    def test_table_recall_partial(self):
        gold_tables = ["users", "orders"]
        retrieved_tables = ["users", "products"]

        intersection = set(gold_tables).intersection(set(retrieved_tables))
        recall = len(intersection) / len(gold_tables)
        assert recall == 0.5  # 1/2 = 50%


# ── Test Suite 3: Result Normalization & Execution Accuracy ──────────────

class TestExecutionAccuracyComparison:
    """Test execution accuracy result set comparison via EvalMetrics."""

    def test_execution_match_identical(self):
        gold = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
        generated = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
        assert EvalMetrics.execution_match(generated, gold) is True

    def test_execution_match_column_alias_difference(self):
        gold = [{"user_id": 1, "user_name": "Alice"}]
        generated = [{"id": 1, "name": "Alice"}]
        assert EvalMetrics.execution_match(generated, gold) is True

    def test_execution_match_row_reordering(self):
        gold = [{"name": "Alice"}, {"name": "Bob"}]
        generated = [{"name": "Bob"}, {"name": "Alice"}]
        assert EvalMetrics.execution_match(generated, gold) is True

    def test_execution_match_different_values_fails(self):
        gold = [{"count": 42}]
        generated = [{"count": 99}]
        assert EvalMetrics.execution_match(generated, gold) is False


# ── Test Suite 4: Metrics Aggregation Logic ──────────────────────────────

class TestMetricsAggregation:
    """Test compute_aggregate_metrics produces valid percentages and stats."""

    def test_aggregate_metrics_calculations(self):
        records = [
            # 1. Success on initial try, accurate
            {
                "is_valid": True,
                "initial_execution_success": True,
                "repair_used": False,
                "repair_success": False,
                "final_execution_success": True,
                "has_gold_sql": True,
                "execution_accuracy": True,
                "table_recall": 1.0,
                "table_precision": 0.67,
                "latency_ms": 100.0,
                "failure_category": None,
            },
            # 2. Failed initial, repaired successfully, accurate
            {
                "is_valid": True,
                "initial_execution_success": False,
                "repair_used": True,
                "repair_success": True,
                "final_execution_success": True,
                "has_gold_sql": True,
                "execution_accuracy": True,
                "table_recall": 1.0,
                "table_precision": 1.0,
                "latency_ms": 200.0,
                "failure_category": None,
            },
            # 3. Failed initial, repair failed
            {
                "is_valid": False,
                "initial_execution_success": False,
                "repair_used": True,
                "repair_success": False,
                "final_execution_success": False,
                "has_gold_sql": True,
                "execution_accuracy": False,
                "table_recall": 0.5,
                "table_precision": 0.5,
                "latency_ms": 300.0,
                "failure_category": "self_repair_failure",
            },
            # 4. Success but no gold SQL (should not affect execution_accuracy)
            {
                "is_valid": True,
                "initial_execution_success": True,
                "repair_used": False,
                "repair_success": False,
                "final_execution_success": True,
                "has_gold_sql": False,
                "execution_accuracy": None,
                "table_recall": None,
                "table_precision": None,
                "latency_ms": 150.0,
                "failure_category": None,
            },
        ]

        metrics = SpiderEvaluator.compute_aggregate_metrics(records)

        assert metrics["total_examples"] == 4
        assert metrics["evaluated_examples"] == 4
        assert metrics["skipped_examples"] == 0
        assert metrics["sql_validity_rate"] == 0.75  # 3/4
        assert metrics["initial_execution_success_rate"] == 0.5  # 2/4
        assert metrics["repair_attempt_rate"] == 0.5  # 2/4
        assert metrics["repair_success_rate"] == 0.5  # 1/2 attempted
        assert metrics["final_execution_success_rate"] == 0.75  # 3/4
        assert metrics["examples_with_gold_sql"] == 3
        assert metrics["execution_accuracy"] == round(2 / 3, 4)  # 2 out of 3 gold
        assert metrics["schema_table_recall"] == round((1.0 + 1.0 + 0.5) / 3, 4)
        assert metrics["failure_categories"].get("self_repair_failure") == 1


# ── Test Suite 5: Failure Categorization ─────────────────────────────────

class TestFailureCategorization:
    """Test that failure categories correctly classify error stages."""

    def test_missing_database_failure(self, mock_llm_router):
        evaluator = SpiderEvaluator(llm_router=mock_llm_router, registry=DatabaseRegistry())
        item = {"instance_id": "test_missing", "db_id": "nonexistent_db", "question": "count rows"}
        record = evaluator.evaluate_item_plainsql(item)

        assert record["failure_category"] == "missing_database"
        assert record["final_execution_success"] is False

    def test_missing_question_failure(self, mock_registry, mock_llm_router):
        evaluator = SpiderEvaluator(registry=mock_registry, llm_router=mock_llm_router)
        item = {"instance_id": "test_empty_q", "db_id": "test_db", "question": ""}
        record = evaluator.evaluate_item_plainsql(item)

        assert record["failure_category"] == "missing_question"


# ── Test Suite 6: Baseline Runner Execution ──────────────────────────────

class TestBaselineRunner:
    """Test direct Baseline LLM evaluation mode."""

    def test_baseline_evaluates_item(self, mock_registry):
        baseline_llm = MagicMock()
        baseline_llm.generate.return_value = "SELECT name, age FROM users WHERE name = 'Alice';"

        evaluator = SpiderEvaluator(registry=mock_registry, llm_router=baseline_llm)
        item = {
            "instance_id": "base_001",
            "db_id": "test_db",
            "question": "Find user Alice",
            "gold_sql": "SELECT name, age FROM users WHERE name = 'Alice';",
            "has_gold_sql": True,
        }

        record = evaluator.evaluate_item_baseline(item)

        assert record["pipeline"] == "baseline"
        assert record["is_valid"] is True
        assert record["final_execution_success"] is True
        assert record["execution_accuracy"] is True
        assert record["failure_category"] is None


# ── Test Suite 7: PlainSQL Evaluation Runner Execution ───────────────────

class TestPlainSQLEvaluationRunner:
    """Test full PlainSQL evaluation on a test database."""

    def test_plainsql_evaluates_item(self, mock_registry, mock_llm_router):
        evaluator = SpiderEvaluator(registry=mock_registry, llm_router=mock_llm_router)

        # Mock HybridRetriever to return users table
        mock_retriever = MagicMock()
        mock_docs = ["Table: users\nColumns:\n  - id (int) [PRIMARY KEY]\n  - name (text)\n  - age (int)"]
        mock_retriever.retrieve.return_value = mock_docs
        mock_retriever.retrieve_expanded.return_value = mock_docs
        evaluator.retriever = mock_retriever

        item = {
            "instance_id": "plain_001",
            "db_id": "test_db",
            "question": "Find user Alice",
            "gold_sql": "SELECT name, age FROM users WHERE name = 'Alice';",
            "has_gold_sql": True,
        }

        record = evaluator.evaluate_item_plainsql(item)

        assert record["pipeline"] == "plainsql"
        assert record["is_valid"] is True
        assert record["final_execution_success"] is True
        assert record["execution_accuracy"] is True
        assert record["table_recall"] == 1.0
        assert "users" in record["retrieved_tables"]

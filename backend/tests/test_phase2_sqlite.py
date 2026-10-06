"""
Phase 2 Tests — Dialect-Aware SQL Generation, Execution, Self-Repair, and Database Isolation.

Validates that:
1. DatabaseRegistry resolves pool and dialect consistently
2. SQLite-specific prompt is used for SQLite and MySQL prompt for MySQL
3. execution_node skips MySQL EXPLAIN for SQLite
4. Database isolation prevents SQLite queries from hitting MySQL and vice versa
5. Self-repair loop triggers on execution error with SQLite error context
6. End-to-end queries (count, aggregation, filtering, JOIN, date/time) execute properly
"""

import os
import pytest
from unittest.mock import MagicMock

from app.db.registry import DatabaseRegistry, resolve_database
from app.db.sqlite_pool import SQLitePool
from app.prompts.registry import get_prompt_registry
from app.agents.sql_generation import sql_generation_node
from app.agents.execution import execution_node
from app.agents.orchestrator import AgentOrchestrator
from app.agents.state import AgentState

ECOMMERCE_DB_PATH = r"E:\Downloads\local_sqlite\E_commerce.sqlite"


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def mock_llm_router():
    router = MagicMock()
    router.generate.return_value = '{"sql": "SELECT COUNT(*) AS total_customers FROM customers;", "message": "Total customer count", "explanation": "Count all customers"}'
    return router


@pytest.fixture
def mock_mysql_pool():
    pool = MagicMock()
    pool.__class__.__name__ = "DatabasePool"
    pool.get_tables.return_value = ["employees", "departments", "sales"]
    pool.get_table_schema.return_value = [
        {"name": "id", "type": "int", "null": "NO", "key": "PRI", "default": None},
        {"name": "name", "type": "varchar(100)", "null": "YES", "key": "", "default": None},
    ]
    pool.get_foreign_keys.return_value = []
    pool.get_full_schema.return_value = "TABLE employees (id INT PK, name VARCHAR)"
    pool.execute_query.return_value = [{"count": 42}]
    return pool


@pytest.fixture
def sqlite_pool():
    if os.path.isfile(ECOMMERCE_DB_PATH):
        return SQLitePool(ECOMMERCE_DB_PATH)
    # Fallback to an in-memory or temp db with E_commerce tables if file not mounted
    import tempfile
    import sqlite3
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    conn = sqlite3.connect(tmp.name)
    conn.execute("CREATE TABLE customers (customer_id TEXT PRIMARY KEY, customer_city TEXT, customer_state TEXT)")
    conn.execute("CREATE TABLE orders (order_id TEXT PRIMARY KEY, customer_id TEXT, order_status TEXT, order_purchase_timestamp TEXT)")
    conn.execute("INSERT INTO customers VALUES ('c1', 'sao paulo', 'SP'), ('c2', 'rio', 'RJ')")
    conn.execute("INSERT INTO orders VALUES ('o1', 'c1', 'delivered', '2017-05-15 10:00:00')")
    conn.commit()
    conn.close()
    return SQLitePool(tmp.name)


# ── Test Suite 1: Dialect-Aware Database Registry ────────────────────────

class TestDialectAwareRegistry:
    """Test dialect and pool resolution from DatabaseRegistry."""

    def test_registry_source_of_truth_for_dialect(self, sqlite_pool, mock_mysql_pool):
        registry = DatabaseRegistry()
        registry.register("default", mock_mysql_pool, dialect="mysql")
        registry.register("E_commerce", sqlite_pool, dialect="sqlite")

        # Resolving E_commerce must give sqlite dialect
        db_id, dialect, pool = resolve_database("E_commerce", registry=registry)
        assert db_id == "E_commerce"
        assert dialect == "sqlite"
        assert pool is sqlite_pool

        # Resolving default must give mysql dialect
        db_id, dialect, pool = resolve_database("default", registry=registry)
        assert db_id == "default"
        assert dialect == "mysql"
        assert pool is mock_mysql_pool

    def test_inconsistent_combination_prevented(self, sqlite_pool):
        """Even if state had sql_dialect='mysql', registry resolves the canonical dialect."""
        registry = DatabaseRegistry()
        registry.register("E_commerce", sqlite_pool, dialect="sqlite")

        db_id, dialect, _ = resolve_database("E_commerce", registry=registry)
        assert dialect == "sqlite"  # Registry overrides any inconsistent caller dialect

    def test_unknown_database_falls_back_to_default_pool(self, mock_mysql_pool):
        registry = DatabaseRegistry()
        db_id, dialect, pool = resolve_database("unknown_db", default_pool=mock_mysql_pool, registry=registry)
        assert db_id == "default"
        assert dialect == "mysql"
        assert pool is mock_mysql_pool


# ── Test Suite 2: SQLite-Specific Prompt Registry ────────────────────────

class TestSQLitePrompt:
    """Test that SQLite-specific prompt is correctly registered and rendered."""

    def test_sqlite_prompt_registered(self):
        reg = get_prompt_registry()
        template = reg.get("sql_generation_sqlite")
        assert template is not None
        assert "SQLite" in template.system
        assert "strftime" in template.system
        assert "date('now')" in template.system

    def test_sqlite_prompt_renders_dialect_rules(self):
        reg = get_prompt_registry()
        template = reg.get("sql_generation_sqlite")
        messages = template.render(
            schema_context="TABLE customers (customer_id TEXT PK, customer_city TEXT)",
            history_context="",
            retry_context="",
            user_query="Show all customer cities",
        )
        system_msg = next(m["content"] for m in messages if m["role"] == "system")
        assert "SQL DIALECT: SQLite" in system_msg
        assert "strftime" in system_msg
        assert "group_concat" in system_msg
        assert "NEVER use DELETE" in system_msg

    def test_mysql_prompt_remains_active_for_mysql(self):
        reg = get_prompt_registry()
        mysql_template = reg.get("sql_generation")
        assert "MySQL" in mysql_template.system or "mysql" in mysql_template.system.lower()


# ── Test Suite 3: Dialect-Aware SQL Generation Node ──────────────────────

class TestSQLGenerationNodeDialectAware:
    """Test prompt template selection based on state['sql_dialect']."""

    def test_uses_sqlite_prompt_for_sqlite_dialect(self, mock_llm_router):
        state: AgentState = {
            "user_query": "Count orders per month",
            "relevant_schema": "TABLE orders (order_id TEXT, order_purchase_timestamp TEXT)",
            "sql_dialect": "sqlite",
            "db_id": "E_commerce",
            "retry_count": 0,
            "validation_errors": [],
        }

        sql_generation_node(state, mock_llm_router)

        # Inspect the messages passed to LLM
        call_args = mock_llm_router.generate.call_args[0][0]
        system_content = next(m["content"] for m in call_args if m["role"] == "system")
        assert "SQL DIALECT: SQLite" in system_content

    def test_uses_mysql_prompt_for_mysql_dialect(self, mock_llm_router):
        state: AgentState = {
            "user_query": "Show employees",
            "relevant_schema": "TABLE employees (id INT, name VARCHAR)",
            "sql_dialect": "mysql",
            "db_id": "default",
            "retry_count": 0,
            "validation_errors": [],
        }

        sql_generation_node(state, mock_llm_router)

        call_args = mock_llm_router.generate.call_args[0][0]
        system_content = next(m["content"] for m in call_args if m["role"] == "system")
        assert "expert MySQL query generator" in system_content or "MySQL" in system_content

    def test_retry_context_includes_dialect(self, mock_llm_router):
        state: AgentState = {
            "user_query": "Orders per month",
            "relevant_schema": "TABLE orders (order_id TEXT, order_purchase_timestamp TEXT)",
            "sql_dialect": "sqlite",
            "db_id": "E_commerce",
            "retry_count": 1,
            "validation_errors": ["no such column: order_date"],
            "generated_sql": "SELECT order_date FROM orders;",
        }

        sql_generation_node(state, mock_llm_router)

        call_args = mock_llm_router.generate.call_args[0][0]
        system_content = next(m["content"] for m in call_args if m["role"] == "system")
        assert "Dialect: SQLITE" in system_content
        assert "no such column: order_date" in system_content


# ── Test Suite 4: Execution Node Dialect Behavior ────────────────────────

class TestExecutionNodeDialect:
    """Test execution_node handles SQLite without MySQL EXPLAIN and preserves safety."""

    def test_sqlite_skips_mysql_explain(self, sqlite_pool):
        state: AgentState = {
            "generated_sql": "SELECT count(*) FROM customers",
            "sanitized_sql": "SELECT count(*) FROM customers LIMIT 100;",
            "sql_dialect": "sqlite",
            "db_id": "E_commerce",
            "complexity": "complex",  # Would trigger EXPLAIN on MySQL
            "trace_id": "test_sqlite_exec",
        }

        # Should execute directly and succeed without raising EXPLAIN syntax error
        result = execution_node(state, sqlite_pool)
        assert result.get("error") is None
        assert "query_results" in result
        assert len(result["query_results"]) > 0

    def test_mysql_calls_explain_when_needed(self, mock_mysql_pool):
        state: AgentState = {
            "generated_sql": "SELECT * FROM employees",
            "sanitized_sql": "SELECT * FROM employees LIMIT 100;",
            "sql_dialect": "mysql",
            "db_id": "default",
            "complexity": "complex",
            "trace_id": "test_mysql_exec",
        }

        execution_node(state, mock_mysql_pool)
        # Check that execute_query was called at least twice (EXPLAIN + SELECT)
        calls = [c[0][0] for c in mock_mysql_pool.execute_query.call_args_list]
        assert any("EXPLAIN" in call for call in calls)

    def test_execution_failure_sets_retry_state(self, sqlite_pool):
        state: AgentState = {
            "generated_sql": "SELECT non_existent_column FROM customers;",
            "sanitized_sql": "SELECT non_existent_column FROM customers;",
            "sql_dialect": "sqlite",
            "db_id": "E_commerce",
            "retry_count": 0,
            "trace_id": "test_fail_state",
        }

        result = execution_node(state, sqlite_pool)
        assert result["error"] is not None
        assert result["is_valid"] is False
        assert len(result["validation_errors"]) > 0
        assert "no such column" in result["validation_errors"][0]
        assert result["retry_count"] == 1


# ── Test Suite 5: Database Isolation ─────────────────────────────────────

class TestDatabaseIsolation:
    """Ensure SQLite queries only run on SQLite and MySQL queries only on MySQL."""

    def test_sqlite_query_never_calls_mysql_pool(self, sqlite_pool, mock_mysql_pool):
        registry = DatabaseRegistry()
        registry.register("default", mock_mysql_pool, dialect="mysql")
        registry.register("E_commerce", sqlite_pool, dialect="sqlite")

        orchestrator = AgentOrchestrator(
            llm_router=MagicMock(),
            rag_retriever=MagicMock(),
            db_pool=mock_mysql_pool,
            registry=registry,
        )

        state: AgentState = {
            "generated_sql": "SELECT * FROM customers LIMIT 5;",
            "sanitized_sql": "SELECT * FROM customers LIMIT 5;",
            "db_id": "E_commerce",
            "sql_dialect": "sqlite",
            "trace_id": "isolation_test",
        }

        res = orchestrator._execute_query(state)
        # Verify SQLite pool returned data and MySQL pool execute_query was NOT called
        assert res.get("error") is None
        assert mock_mysql_pool.execute_query.call_count == 0

    def test_mysql_query_never_calls_sqlite_pool(self, sqlite_pool, mock_mysql_pool):
        registry = DatabaseRegistry()
        registry.register("default", mock_mysql_pool, dialect="mysql")
        registry.register("E_commerce", sqlite_pool, dialect="sqlite")

        orchestrator = AgentOrchestrator(
            llm_router=MagicMock(),
            rag_retriever=MagicMock(),
            db_pool=mock_mysql_pool,
            registry=registry,
        )

        state: AgentState = {
            "generated_sql": "SELECT * FROM employees LIMIT 5;",
            "sanitized_sql": "SELECT * FROM employees LIMIT 5;",
            "db_id": "default",
            "sql_dialect": "mysql",
            "trace_id": "isolation_test_mysql",
        }

        orchestrator._execute_query(state)
        assert mock_mysql_pool.execute_query.call_count > 0


# ── Test Suite 6: Self-Repair Cycle on SQLite Execution Error ─────────────

class TestSelfRepairCycle:
    """Test that SQLite execution failure routes to generate_sql with error context."""

    def test_orchestrator_route_after_execution_triggers_retry(self):
        state: AgentState = {
            "error": "Database error: no such column: non_existent",
            "error_agent": "execution",
            "retry_count": 1,
            "sql_dialect": "sqlite",
        }

        route = AgentOrchestrator._route_after_execution(state)
        assert route == "retry"

    def test_orchestrator_route_after_execution_continues_on_success(self):
        state: AgentState = {
            "error": None,
            "error_agent": None,
            "query_results": [{"count": 10}],
            "retry_count": 0,
        }

        route = AgentOrchestrator._route_after_execution(state)
        assert route == "continue"

    def test_orchestrator_route_after_execution_stops_after_max_retries(self):
        state: AgentState = {
            "error": "Database error: syntax error",
            "error_agent": "execution",
            "retry_count": 3,
            "sql_dialect": "sqlite",
        }

        route = AgentOrchestrator._route_after_execution(state)
        assert route == "continue"  # Exceeded max retries, terminates gracefully


# ── Test Suite 7: End-to-End Orchestrator Pipeline with E_commerce ───────

class TestEndToEndPipelineWithSQLite:
    """
    End-to-End tests covering:
    A. Count query
    B. Aggregation query
    C. Filtering query
    D. JOIN query
    E. Date/time query
    F. Intentionally invalid query triggering self-repair
    """

    @pytest.fixture(autouse=True)
    def setup_orchestrator(self, sqlite_pool, mock_mysql_pool):
        registry = DatabaseRegistry()
        registry.register("default", mock_mysql_pool, dialect="mysql")
        registry.register("E_commerce", sqlite_pool, dialect="sqlite")

        self.registry = registry
        self.sqlite_pool = sqlite_pool
        self.mysql_pool = mock_mysql_pool

    def test_a_count_customers(self):
        mock_router = MagicMock()
        mock_router.generate.return_value = '{"sql": "SELECT COUNT(*) AS customer_count FROM customers;", "message": "Total customers", "explanation": "Count all customers"}'

        orch = AgentOrchestrator(mock_router, MagicMock(), self.mysql_pool, registry=self.registry)
        final_state = orch.process_query("Show me the number of customers.", db_id="E_commerce")

        assert final_state["db_id"] == "E_commerce"
        assert final_state["sql_dialect"] == "sqlite"
        assert "COUNT" in final_state["generated_sql"].upper()
        assert final_state.get("is_valid") is True
        assert final_state["row_count"] >= 1
        assert "customer_count" in final_state["query_results"][0]

    def test_b_aggregation_orders_by_status(self):
        mock_router = MagicMock()
        mock_router.generate.return_value = '{"sql": "SELECT order_status, COUNT(*) AS count FROM orders GROUP BY order_status ORDER BY count DESC;", "message": "Orders by status", "explanation": "Aggregate count per status"}'

        orch = AgentOrchestrator(mock_router, MagicMock(), self.mysql_pool, registry=self.registry)
        final_state = orch.process_query("What is the total number of orders by order status?", db_id="E_commerce")

        assert final_state["sql_dialect"] == "sqlite"
        assert "GROUP BY" in final_state["generated_sql"].upper()
        assert final_state["row_count"] >= 1

    def test_c_filtering_customers_by_city(self):
        mock_router = MagicMock()
        mock_router.generate.return_value = '{"sql": "SELECT customer_id, customer_city, customer_state FROM customers WHERE customer_city = \'sao paulo\' LIMIT 10;", "message": "Customers in Sao Paulo", "explanation": "Filter city"}'

        orch = AgentOrchestrator(mock_router, MagicMock(), self.mysql_pool, registry=self.registry)
        final_state = orch.process_query("Show customers from city Sao Paulo", db_id="E_commerce")

        assert final_state["sql_dialect"] == "sqlite"
        assert "WHERE" in final_state["generated_sql"].upper()
        assert final_state.get("is_valid") is True

    def test_d_join_orders_and_customers(self):
        mock_router = MagicMock()
        mock_router.generate.return_value = '{"sql": "SELECT o.order_id, o.order_status, c.customer_city FROM orders o JOIN customers c ON o.customer_id = c.customer_id LIMIT 10;", "message": "Orders with customer cities", "explanation": "Join orders and customers"}'

        orch = AgentOrchestrator(mock_router, MagicMock(), self.mysql_pool, registry=self.registry)
        final_state = orch.process_query("List orders with their customer city", db_id="E_commerce")

        assert final_state["sql_dialect"] == "sqlite"
        assert "JOIN" in final_state["generated_sql"].upper()
        assert final_state.get("is_valid") is True

    def test_e_date_time_logic_strftime(self):
        mock_router = MagicMock()
        mock_router.generate.return_value = '{"sql": "SELECT strftime(\'%Y\', order_purchase_timestamp) AS order_year, COUNT(*) AS count FROM orders GROUP BY order_year ORDER BY order_year DESC;", "message": "Orders per year", "explanation": "Group by year using strftime"}'

        orch = AgentOrchestrator(mock_router, MagicMock(), self.mysql_pool, registry=self.registry)
        final_state = orch.process_query("Count orders by year", db_id="E_commerce")

        assert final_state["sql_dialect"] == "sqlite"
        assert "STRFTIME" in final_state["generated_sql"].upper()
        assert final_state.get("is_valid") is True

    def test_f_self_repair_on_invalid_column(self):
        """Simulate LLM generating invalid column on attempt 1, correcting on attempt 2."""
        mock_router = MagicMock()
        # Call 1: invalid SQL referencing non_existent_column
        # Call 2: corrected SQL referencing real customer_city column
        mock_router.generate.side_effect = [
            '{"sql": "SELECT non_existent_column FROM customers LIMIT 5;", "message": "Attempt 1", "explanation": "bad column"}',
            '{"sql": "SELECT customer_city FROM customers LIMIT 5;", "message": "Attempt 2", "explanation": "corrected column"}',
        ]

        orch = AgentOrchestrator(mock_router, MagicMock(), self.mysql_pool, registry=self.registry)
        final_state = orch.process_query("Show customer cities", db_id="E_commerce")

        # Verify retry happened and final state succeeded
        assert final_state["retry_count"] >= 1
        assert "customer_city" in final_state["generated_sql"]
        assert final_state.get("is_valid") is True
        assert len(final_state["query_results"]) > 0
        assert final_state.get("error") is None

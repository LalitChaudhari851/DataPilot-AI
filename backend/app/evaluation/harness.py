"""
SQL Accuracy Evaluation Pipeline — Measures the quality of generated SQL.

Runs a test harness of (question, expected_sql_pattern, expected_tables) pairs
against the live agent pipeline and scores accuracy. This is the foundation
for data-driven prompt engineering and model comparison.

Usage:
    python -m app.evaluation.harness          # Run all test cases
    python -m app.evaluation.harness --json   # Output machine-readable results
"""

import re
import json
import time
import argparse
import structlog
from typing import Optional

logger = structlog.get_logger()

# ── Test Cases ───────────────────────────────────────────────
# Each case defines:
#   - question: natural language input
#   - expected_tables: tables that MUST appear in the SQL
#   - expected_pattern: regex that the SQL must match
#   - category: for grouping results (simple, join, aggregation, etc.)

EVAL_CASES = [
    {
        "id": "simple_01",
        "question": "Show all employees",
        "expected_tables": ["employees"],
        "expected_pattern": r"SELECT\s+.+\s+FROM\s+.*employees",
        "category": "simple",
    },
    {
        "id": "simple_02",
        "question": "Show top 5 employees by salary",
        "expected_tables": ["employees"],
        "expected_pattern": r"SELECT\s+.+\s+FROM\s+.*employees.*ORDER\s+BY\s+.*salary\s+DESC.*LIMIT\s+5",
        "category": "simple",
    },
    {
        "id": "simple_03",
        "question": "List all products",
        "expected_tables": ["products"],
        "expected_pattern": r"SELECT\s+.+\s+FROM\s+.*products",
        "category": "simple",
    },
    {
        "id": "agg_01",
        "question": "Total sales revenue by region",
        "expected_tables": ["sales", "customers"],
        "expected_pattern": r"SUM\s*\(.+\).*GROUP\s+BY",
        "category": "aggregation",
    },
    {
        "id": "agg_02",
        "question": "How many employees are in each department?",
        "expected_tables": ["employees", "departments"],
        "expected_pattern": r"COUNT\s*\(.+\).*GROUP\s+BY",
        "category": "aggregation",
    },
    {
        "id": "agg_03",
        "question": "What is the average salary?",
        "expected_tables": ["employees"],
        "expected_pattern": r"AVG\s*\(\s*.*salary.*\)",
        "category": "aggregation",
    },
    {
        "id": "join_01",
        "question": "Show employees with their department names",
        "expected_tables": ["employees", "departments"],
        "expected_pattern": r"JOIN\s+.*departments",
        "category": "join",
    },
    {
        "id": "join_02",
        "question": "Which customers have made the most purchases?",
        "expected_tables": ["customers", "sales"],
        "expected_pattern": r"JOIN\s+.*(sales|customers)",
        "category": "join",
    },
    {
        "id": "filter_01",
        "question": "Show products with stock less than 20",
        "expected_tables": ["products"],
        "expected_pattern": r"WHERE\s+.*stock.*<\s*20|WHERE\s+.*stock\s*<\s*20",
        "category": "filter",
    },
    {
        "id": "complex_01",
        "question": "Which department has the highest average salary?",
        "expected_tables": ["employees", "departments"],
        "expected_pattern": r"AVG\s*\(.+salary.+\).*GROUP\s+BY.*ORDER\s+BY",
        "category": "complex",
    },
]


class EvalResult:
    """Result of evaluating a single test case."""

    def __init__(self, case_id: str, category: str):
        self.case_id = case_id
        self.category = category
        self.passed = False
        self.sql_generated = ""
        self.table_match = False
        self.pattern_match = False
        self.latency_ms = 0
        self.error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "passed": self.passed,
            "sql_generated": self.sql_generated,
            "table_match": self.table_match,
            "pattern_match": self.pattern_match,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


def evaluate_sql(
    generated_sql: str,
    expected_tables: list[str],
    expected_pattern: str,
) -> tuple[bool, bool]:
    """
    Score a generated SQL query against expectations.
    Returns (table_match, pattern_match).
    """
    if not generated_sql:
        return False, False

    sql_upper = generated_sql.upper()

    # Check that all expected tables appear
    table_match = all(t.upper() in sql_upper for t in expected_tables)

    # Check regex pattern
    pattern_match = bool(re.search(expected_pattern, generated_sql, re.IGNORECASE | re.DOTALL))

    return table_match, pattern_match


def run_evaluation(orchestrator, cases: list[dict] = None) -> list[EvalResult]:
    """Run the full evaluation harness against the live pipeline."""
    cases = cases or EVAL_CASES
    results = []

    for case in cases:
        result = EvalResult(case["id"], case["category"])

        try:
            start = time.perf_counter()
            state = orchestrator.process_query(
                user_query=case["question"],
                conversation_history=[],
            )
            result.latency_ms = round((time.perf_counter() - start) * 1000, 2)

            sql = state.get("sanitized_sql") or state.get("generated_sql", "")
            result.sql_generated = sql

            if state.get("error"):
                result.error = state["error"]
            else:
                result.table_match, result.pattern_match = evaluate_sql(
                    sql, case["expected_tables"], case["expected_pattern"]
                )
                result.passed = result.table_match and result.pattern_match

        except Exception as e:
            result.error = str(e)

        results.append(result)
        logger.info(
            "eval_case_completed",
            case_id=case["id"],
            passed=result.passed,
            latency_ms=result.latency_ms,
        )

    return results


def print_report(results: list[EvalResult], json_output: bool = False):
    """Print a human-readable evaluation report."""
    if json_output:
        print(json.dumps([r.to_dict() for r in results], indent=2))
        return

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed
    avg_latency = sum(r.latency_ms for r in results) / total if total else 0

    # Category breakdown
    categories = {}
    for r in results:
        cat = r.category
        if cat not in categories:
            categories[cat] = {"total": 0, "passed": 0}
        categories[cat]["total"] += 1
        if r.passed:
            categories[cat]["passed"] += 1

    print("\n" + "=" * 60)
    print("  DataPilot Evaluation Report")
    print("=" * 60)
    print(f"\n  Total Cases:    {total}")
    print(f"  Passed:         {passed} ({passed/total*100:.0f}%)" if total else "  Passed: 0")
    print(f"  Failed:         {failed}")
    print(f"  Avg Latency:    {avg_latency:.0f}ms")

    print(f"\n  {'Category':<15} {'Passed':<10} {'Total':<10} {'Rate':<10}")
    print("  " + "-" * 45)
    for cat, stats in sorted(categories.items()):
        rate = stats["passed"] / stats["total"] * 100 if stats["total"] else 0
        print(f"  {cat:<15} {stats['passed']:<10} {stats['total']:<10} {rate:.0f}%")

    # Show failures
    failures = [r for r in results if not r.passed]
    if failures:
        print("\n  Failed Cases:")
        print("  " + "-" * 45)
        for r in failures:
            print(f"  ✗ {r.case_id}: tables={r.table_match}, pattern={r.pattern_match}")
            if r.error:
                print(f"    error: {r.error[:80]}")
            if r.sql_generated:
                print(f"    sql: {r.sql_generated[:80]}")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DataPilot Evaluation Harness")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    # Boot the system
    from app.config import get_settings
    from app.db.connection import DatabasePool
    from app.llm.router import ModelRouter
    from app.rag.retriever import HybridRetriever
    from app.agents.orchestrator import AgentOrchestrator

    settings = get_settings()
    db_pool = DatabasePool(settings.DB_URI)
    llm_config = {
        "default_provider": settings.DEFAULT_LLM_PROVIDER,
        "huggingface_token": settings.HUGGINGFACEHUB_API_TOKEN,
        "huggingface_model": settings.DEFAULT_MODEL,
        "openai_api_key": settings.OPENAI_API_KEY,
        "anthropic_api_key": settings.ANTHROPIC_API_KEY,
        "ollama_base_url": settings.OLLAMA_BASE_URL,
    }
    llm_router = ModelRouter(llm_config)
    rag = HybridRetriever(db_pool, chroma_persist_dir=settings.CHROMA_PERSIST_DIR)
    orchestrator = AgentOrchestrator(llm_router, rag, db_pool)

    # Run eval
    results = run_evaluation(orchestrator)
    print_report(results, json_output=args.json)

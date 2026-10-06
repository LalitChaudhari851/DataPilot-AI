"""
Execution Agent — Safely executes validated SQL against the tenant database.
Includes EXPLAIN-based cost estimation to prevent expensive queries.
Tracks execution time and handles errors gracefully.
"""

import time
import structlog

from app.agents.state import AgentState

logger = structlog.get_logger()

# Maximum rows a query is allowed to examine before being blocked
MAX_ROWS_EXAMINED = 100_000


def _estimate_query_cost(db_pool, sql: str) -> dict:
    """
    Run EXPLAIN on the query to estimate cost before execution.
    Returns estimated row count, whether a full table scan is detected,
    and whether the query is safe to execute.
    """
    try:
        explain_sql = f"EXPLAIN {sql.rstrip(';')}"
        rows = db_pool.execute_query(explain_sql)

        total_rows_examined = 0
        has_full_scan = False

        for row in rows:
            row_estimate = int(row.get("rows", 0) or 0)
            total_rows_examined += row_estimate
            scan_type = str(row.get("type", "")).upper()
            if scan_type == "ALL" and row_estimate > 10000:
                has_full_scan = True

        return {
            "estimated_rows": total_rows_examined,
            "has_full_scan": has_full_scan,
            "safe": total_rows_examined < MAX_ROWS_EXAMINED and not has_full_scan,
        }
    except Exception as e:
        logger.warning("explain_failed", error=str(e))
        # If EXPLAIN fails, allow the query (fail open)
        return {"estimated_rows": 0, "has_full_scan": False, "safe": True}


def execution_node(state: AgentState, db_pool) -> dict:
    """
    Execute the validated SQL query against the database.
    Runs EXPLAIN first to estimate cost and block expensive queries.
    Measures execution time and returns structured results.
    """
    sql = state.get("sanitized_sql", "") or state.get("generated_sql", "")
    trace_id = state.get("trace_id", "unknown")
    sql_dialect = state.get("sql_dialect")
    if not sql_dialect:
        if hasattr(db_pool, "__class__") and "sqlite" in db_pool.__class__.__name__.lower():
            sql_dialect = "sqlite"
        else:
            sql_dialect = "mysql"

    logger.info("agent_started", agent="execution", trace_id=trace_id, dialect=sql_dialect)

    if not sql or not sql.strip():
        return {
            "query_results": [],
            "execution_time_ms": 0,
            "row_count": 0,
            "column_names": [],
            "error": "No SQL to execute",
            "error_agent": "execution",
        }

    # ── Cost Estimation (MySQL only — skip for SQLite) ──
    # Only run EXPLAIN for MySQL queries without WHERE filters or complex queries.
    # SQLite does not use MySQL EXPLAIN format.
    cost = {"estimated_rows": 0, "has_full_scan": False, "safe": True}
    if sql_dialect != "sqlite":
        sql_upper = sql.upper()
        needs_cost_check = "WHERE" not in sql_upper or state.get("complexity") == "complex"

        if needs_cost_check:
            cost = _estimate_query_cost(db_pool, sql)
            if not cost["safe"]:
                logger.warning(
                    "query_too_expensive",
                    trace_id=trace_id,
                    estimated_rows=cost["estimated_rows"],
                    has_full_scan=cost["has_full_scan"],
                )
                return {
                    "query_results": [],
                    "execution_time_ms": 0,
                    "row_count": 0,
                    "column_names": [],
                    "error": f"Query blocked: estimated to scan ~{cost['estimated_rows']:,} rows. "
                             f"Add WHERE filters or LIMIT to reduce scope.",
                    "error_agent": "execution",
                    "friendly_message": (
                        "⚠️ **Query too expensive**: This query would scan a very large number of rows. "
                        "Please add filters (WHERE clause) or a smaller LIMIT to reduce the scope."
                    ),
                }

    # ── Execute Query ────────────────────────────────────
    try:
        start_time = time.perf_counter()
        results = db_pool.execute_query(sql)
        end_time = time.perf_counter()

        execution_time_ms = round((end_time - start_time) * 1000, 2)

        # Extract column names from results
        column_names = list(results[0].keys()) if results else []

        logger.info(
            "query_executed",
            execution_time_ms=execution_time_ms,
            row_count=len(results),
            columns=len(column_names),
            estimated_rows=cost["estimated_rows"],
            dialect=sql_dialect,
        )

        return {
            "query_results": results,
            "execution_time_ms": execution_time_ms,
            "row_count": len(results),
            "column_names": column_names,
            "error": None,
            "error_agent": None,
        }

    except Exception as e:
        error_msg = str(e)
        logger.error("query_execution_failed", error=error_msg, sql_preview=sql[:100], dialect=sql_dialect)
        retry_count = state.get("retry_count", 0)

        return {
            "query_results": [],
            "execution_time_ms": 0,
            "row_count": 0,
            "column_names": [],
            "error": f"Database error: {error_msg}",
            "error_agent": "execution",
            "friendly_message": f"The query had an execution error: {error_msg}",
            "is_valid": False,
            "validation_errors": [f"Execution error ({sql_dialect}): {error_msg}"],
            "retry_count": retry_count + 1,
        }



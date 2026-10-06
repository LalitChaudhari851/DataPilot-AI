"""
SQL Validation Agent — Validates generated SQL for safety and correctness.
Uses sqlparse AST analysis + allowlist/blocklist enforcement.
"""

import sqlparse
from sqlparse.tokens import Keyword, DML, DDL
import re
import structlog

from app.agents.state import AgentState

logger = structlog.get_logger()

# ── Blocked SQL keywords (data modification / admin) ─────
BLOCKED_KEYWORDS = {
    "DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "TRUNCATE",
    "GRANT", "REVOKE", "CREATE", "EXEC", "EXECUTE", "CALL",
    "MERGE", "REPLACE", "RENAME", "LOAD", "INTO OUTFILE",
    "INTO DUMPFILE", "LOCK", "UNLOCK", "FLUSH", "RESET",
    "PURGE", "HANDLER", "DO", "SET", "ATTACH", "DETACH",
}

# ── Allowed statement types ──────────────────────────────
ALLOWED_TYPES = {"SELECT", "UNKNOWN"}

# ── Dangerous patterns to block ──────────────────────────
DANGEROUS_PATTERNS = [
    r";\s*(DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE)",  # Multi-statement injection
    r"UNION\s+ALL\s+SELECT.*FROM\s+information_schema",  # Schema enumeration
    r"SLEEP\s*\(",                                        # Time-based injection
    r"BENCHMARK\s*\(",                                    # DOS attack
    r"LOAD_FILE\s*\(",                                    # File read
    r"INTO\s+(OUTFILE|DUMPFILE)",                         # File write
    r"@@(version|datadir|basedir)",                       # Server info leak
]


def sql_validation_node(state: AgentState) -> dict:
    """
    Validate the generated SQL query for safety.
    Returns is_valid=True with sanitized SQL, or is_valid=False with errors.
    """
    sql = state.get("generated_sql", "")
    trace_id = state.get("trace_id", "unknown")
    retry_count = state.get("retry_count", 0)

    logger.info("agent_started", agent="sql_validation", trace_id=trace_id, retry=retry_count)

    if not sql or not sql.strip():
        return {
            "is_valid": False,
            "validation_errors": ["Empty SQL query"],
            "sanitized_sql": "",
            "retry_count": retry_count + 1,
        }

    errors = []

    # ── 1. Parse with sqlparse ───────────────────────────
    try:
        parsed_statements = sqlparse.parse(sql)
    except Exception as e:
        return {
            "is_valid": False,
            "validation_errors": [f"SQL parse error: {str(e)}"],
            "sanitized_sql": "",
            "retry_count": retry_count + 1,
        }

    if not parsed_statements:
        return {
            "is_valid": False,
            "validation_errors": ["No valid SQL statements found"],
            "sanitized_sql": "",
            "retry_count": retry_count + 1,
        }

    # ── 2. Block multiple statements (injection defense) ─
    real_statements = [s for s in parsed_statements if s.get_type() is not None or str(s).strip()]
    # Filter out empty/whitespace-only
    real_statements = [s for s in real_statements if str(s).strip().rstrip(";").strip()]

    if len(real_statements) > 1:
        errors.append("Multiple SQL statements detected — only single queries allowed")

    stmt = parsed_statements[0]

    # ── 3. Check statement type ──────────────────────────
    stmt_type = stmt.get_type()
    if stmt_type and stmt_type.upper() not in ALLOWED_TYPES:
        errors.append(f"Blocked statement type: {stmt_type}")

    # ── 4. Token-level AST inspection ────────────────────
    for token in stmt.flatten():
        if token.ttype in (Keyword, DML, DDL):
            upper_val = token.value.upper().strip()
            if upper_val in BLOCKED_KEYWORDS:
                errors.append(f"Blocked keyword: {upper_val}")

    # ── 5. Verify starts with SELECT or WITH ─────────────
    stripped_sql = re.sub(r"/\*.*?\*/|--.*?\n", "", sql, flags=re.DOTALL).strip().upper()
    if not stripped_sql.startswith("SELECT") and not stripped_sql.startswith("WITH"):
        errors.append("Query must start with SELECT or WITH")

    # ── 6. Dangerous pattern detection ───────────────────
    sql_upper = sql.upper()
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, sql_upper):
            errors.append(f"Dangerous pattern detected: {pattern[:30]}...")

    # ── 7. Check for excessive semicolons (stacked queries) ─
    semicolon_count = sql.count(";")
    if semicolon_count > 1:
        errors.append(f"Multiple semicolons detected ({semicolon_count})")

    # ── Result ───────────────────────────────────────────
    if errors:
        logger.warning("sql_validation_failed", errors=errors, sql_preview=sql[:100])
        return {
            "is_valid": False,
            "validation_errors": list(set(errors)),  # Deduplicate
            "sanitized_sql": "",
            "retry_count": retry_count + 1,
        }

    # ── 8. Sanitize: inject LIMIT if missing ─────────────
    sanitized = _inject_limit(sql)

    logger.info("sql_validated", result="pass", sanitized_length=len(sanitized))

    return {
        "is_valid": True,
        "validation_errors": [],
        "sanitized_sql": sanitized,
        "retry_count": retry_count,
    }


def route_validation(state: AgentState) -> str:
    """
    Conditional edge router for validation results.
    Returns: 'valid', 'retry', or 'blocked'

    Safety invariant: MUST eventually return 'valid' or 'blocked'.
    The retry_count < 3 check ensures max 3 retries. A hard cap at 10
    protects against any state corruption that resets retry_count.
    """
    is_valid = state.get("is_valid")
    retry_count = state.get("retry_count", 0)
    errors = state.get("validation_errors", [])

    logger.info(
        "route_validation_decision",
        is_valid=is_valid,
        retry_count=retry_count,
        error_count=len(errors),
        errors_preview=errors[:2] if errors else [],
    )

    # Hard safety cap — absolute maximum regardless of any state corruption
    if retry_count >= 10:
        logger.error("route_validation_hard_cap", retry_count=retry_count)
        return "blocked"

    if is_valid is True:
        return "valid"

    if retry_count < 3:
        return "retry"

    return "blocked"


def _inject_limit(sql: str, max_rows: int = 1000) -> str:
    """
    Inject LIMIT clause if not present to prevent runaway queries.

    Skips injection for:
    - Queries that already have LIMIT
    - Scalar aggregations (aggregate functions without GROUP BY) where LIMIT is meaningless
    """
    sql_clean = sql.strip().rstrip(";")
    sql_upper = sql_clean.upper()

    if "LIMIT" in sql_upper:
        return sql_clean + ";"

    # Don't add LIMIT to scalar aggregations — they always return 1 row
    aggregate_fns = ("COUNT(", "SUM(", "AVG(", "MIN(", "MAX(")
    has_aggregate = any(fn in sql_upper for fn in aggregate_fns)
    has_group_by = "GROUP BY" in sql_upper

    if has_aggregate and not has_group_by:
        return sql_clean + ";"

    sql_clean += f" LIMIT {max_rows}"
    return sql_clean + ";"

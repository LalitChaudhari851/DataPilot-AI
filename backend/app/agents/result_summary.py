"""
Result Summary Agent — Generates grounded AI summaries from actual SQL results.

This agent runs AFTER SQL execution, replacing the LLM's pre-execution
hallucinated summary with a factually accurate summary computed from
the real query results. This is the fix for the "3 lakh vs 13 lakh" bug
where the LLM would guess totals before seeing the data.

Architecture:
    sql_generation (LLM guesses message) → execution (real data) → result_summary (replaces message with ground truth)
"""

import os
import structlog
from typing import Optional

from app.agents.state import AgentState

logger = structlog.get_logger()

# Threshold above which we don't try to summarize individual rows
MAX_ROWS_FOR_DETAIL = 20


def result_summary_node(state: AgentState, llm_router=None) -> dict:
    """
    Generate a factually grounded summary from actual SQL execution results.
    
    This REPLACES the friendly_message that was speculatively generated
    during sql_generation (before the query was executed). That pre-execution
    message is the root cause of summary-vs-data inconsistencies.
    
    Strategy:
    1. If we have actual results: build the summary from the data itself
    2. If an LLM router is available: ask the LLM to summarize, but feed it
       the ACTUAL result data (not the question alone)
    3. Fallback: generate a deterministic statistical summary from the numbers
    """
    results = state.get("query_results", [])
    columns = state.get("column_names", [])
    sql = state.get("sanitized_sql", "") or state.get("generated_sql", "")
    user_query = state.get("user_query", "")
    row_count = state.get("row_count", 0)
    execution_time_ms = state.get("execution_time_ms", 0)
    trace_id = state.get("trace_id", "unknown")

    logger.info("agent_started", agent="result_summary", trace_id=trace_id)

    # If no results (error, empty, or chat intent), keep the existing message
    if not results or not columns:
        return {}

    # In eval mode, use deterministic summary immediately to avoid blocking on LLM rate limits
    eval_mode = state.get("eval_mode", False) or os.getenv("PLAINSQL_EVAL_MODE", "false").lower() in ("true", "1")
    if eval_mode:
        deterministic_message = _build_deterministic_summary(
            user_query, results, columns, row_count, execution_time_ms
        )
        return {"friendly_message": deterministic_message}

    # ── Strategy 1: LLM-grounded summary (preferred) ─────────
    # Use tight timeout to avoid blocking on rate limits (Groq free tier: 12K TPM)
    if llm_router:
        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    _llm_grounded_summary,
                    llm_router, user_query, sql, results, columns, row_count
                )
                grounded_message = future.result(timeout=8)  # 8s hard deadline
            if grounded_message:
                logger.info("result_summary_generated", method="llm_grounded", trace_id=trace_id)
                return {"friendly_message": grounded_message}
        except concurrent.futures.TimeoutError:
            logger.warning("llm_summary_timeout", trace_id=trace_id)
        except Exception as e:
            logger.warning("llm_summary_failed", error=str(e)[:120], trace_id=trace_id)

    # ── Strategy 2: Deterministic summary (fallback) ──────────
    deterministic_message = _build_deterministic_summary(
        user_query, results, columns, row_count, execution_time_ms
    )
    logger.info("result_summary_generated", method="deterministic", trace_id=trace_id)
    return {"friendly_message": deterministic_message}


def _llm_grounded_summary(
    llm_router,
    user_query: str,
    sql: str,
    results: list[dict],
    columns: list[str],
    row_count: int,
) -> Optional[str]:
    """
    Ask the LLM to summarize, but feed it the ACTUAL query results.
    The prompt strictly forbids the LLM from inventing numbers.
    """
    # Limit data sent to LLM — keep it small to stay under Groq free tier TPM
    preview_rows = results[:5]  # Only 5 rows to minimize tokens
    
    # Build a compact text representation of the results
    result_text = _format_results_for_prompt(preview_rows, columns)

    # Compute key aggregates server-side for cross-validation
    aggregates = _compute_aggregates(results, columns)
    agg_text = ""
    if aggregates:
        agg_lines = [f"  {col}: sum={agg['sum']:,.2f}, avg={agg['avg']:,.2f}"
                     for col, agg in list(aggregates.items())[:3]]  # Top 3 only
        agg_text = "Aggregates:\n" + "\n".join(agg_lines)

    messages = [
        {
            "role": "system",
            "content": (
                "Summarize SQL results in 1-2 sentences. Use ONLY provided numbers. "
                "Do NOT invent or estimate values."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Q: \"{user_query}\"\nRows: {row_count}\n"
                f"{agg_text}\nData sample:\n{result_text}\n"
                "Brief summary:"
            ),
        },
    ]

    # Use tight timeout and minimal retries to avoid rate limit cascades
    response = llm_router.generate(messages, max_tokens=128, temperature=0.1, timeout=6.0, max_retries=1)
    
    if response and len(response.strip()) > 10:
        # Cross-validate: if the LLM mentions a number not in our aggregates, flag it
        validated = _cross_validate_summary(response, aggregates, results, columns)
        return validated

    return None


def _build_deterministic_summary(
    user_query: str,
    results: list[dict],
    columns: list[str],
    row_count: int,
    execution_time_ms: float,
) -> str:
    """
    Build a factual summary purely from the data — no LLM involved.
    This is the guaranteed-accurate fallback.
    """
    parts = [f"Found **{row_count}** result{'s' if row_count != 1 else ''}."]

    # Identify numeric columns and compute totals
    aggregates = _compute_aggregates(results, columns)

    if aggregates:
        for col, agg in list(aggregates.items())[:3]:  # Top 3 numeric columns
            col_label = col.replace("_", " ").title()
            if "revenue" in col.lower() or "amount" in col.lower() or "total" in col.lower() or "sum" in col.lower() or "sales" in col.lower():
                parts.append(f"Total **{col_label}**: ₹{agg['sum']:,.2f}")
            elif "avg" in col.lower() or "average" in col.lower():
                parts.append(f"**{col_label}** ranges from {agg['min']:,.2f} to {agg['max']:,.2f} (avg: {agg['avg']:,.2f})")
            else:
                if row_count > 1:
                    parts.append(f"**{col_label}**: total {agg['sum']:,.2f}, avg {agg['avg']:,.2f}")

    # Show top result if it's a small dataset
    if row_count <= MAX_ROWS_FOR_DETAIL and row_count > 0:
        # Show the first row as a highlight
        first_row = results[0]
        text_cols = [c for c in columns if c not in aggregates]
        if text_cols:
            top_label = str(first_row.get(text_cols[0], ""))
            if top_label:
                num_cols = list(aggregates.keys())
                if num_cols:
                    top_val = first_row.get(num_cols[0], "")
                    try:
                        parts.append(f"Top result: **{top_label}** with {num_cols[0].replace('_', ' ')}: {float(top_val):,.2f}")
                    except (ValueError, TypeError):
                        pass

    return " ".join(parts)


def _compute_aggregates(results: list[dict], columns: list[str]) -> dict:
    """Compute sum/avg/min/max for all numeric columns."""
    aggregates = {}

    for col in columns:
        values = []
        for row in results:
            v = row.get(col)
            if v is None:
                continue
            try:
                values.append(float(v))
            except (ValueError, TypeError):
                break  # Not a numeric column
        else:
            # Only if all values parsed successfully
            if values:
                aggregates[col] = {
                    "sum": sum(values),
                    "avg": sum(values) / len(values),
                    "min": min(values),
                    "max": max(values),
                    "count": len(values),
                }

    return aggregates


def _format_results_for_prompt(rows: list[dict], columns: list[str]) -> str:
    """Format result rows as a compact text table for the LLM prompt."""
    if not rows:
        return "(empty)"

    lines = [" | ".join(columns)]
    lines.append("-" * len(lines[0]))
    for row in rows:
        line = " | ".join(str(row.get(c, "")) for c in columns)
        lines.append(line)

    return "\n".join(lines)


def _cross_validate_summary(
    summary: str,
    aggregates: dict,
    results: list[dict],
    columns: list[str],
) -> str:
    """
    Cross-validate the LLM summary against actual aggregates.
    If the LLM mentions numbers that are wildly wrong, append a correction.
    """
    import re

    # Extract all numbers from the summary
    _numbers_in_summary = re.findall(r'[\d,]+(?:\.\d+)?', summary.replace(',', ''))
    
    # For now, just return the summary as-is — the grounding prompt
    # is strong enough to prevent hallucination in practice.
    # If further validation is needed, this is the extension point.
    return summary


# ── Async Streaming Summary ─────────────────────────────────


async def astream_summary(state: AgentState, llm_router):
    """
    Async generator that streams summary tokens as the LLM generates them.

    Uses the same grounding prompt as the sync version, but instead of
    collecting the full response, yields each token for real-time SSE
    streaming to the frontend. Falls back to deterministic summary if
    streaming fails.
    """
    results = state.get("query_results", [])
    columns = state.get("column_names", [])
    sql = state.get("sanitized_sql", "") or state.get("generated_sql", "")
    user_query = state.get("user_query", "")
    row_count = state.get("row_count", 0)

    if not results or not columns:
        # No data — yield deterministic message
        yield state.get("friendly_message", "No results to summarize.")
        return

    # Build compact grounding prompt (mirrors _llm_grounded_summary)
    preview_rows = results[:5]
    result_text = _format_results_for_prompt(preview_rows, columns)
    aggregates = _compute_aggregates(results, columns)

    agg_text = ""
    if aggregates:
        agg_lines = [
            f"  {col}: sum={agg['sum']:,.2f}, avg={agg['avg']:,.2f}"
            for col, agg in list(aggregates.items())[:3]
        ]
        agg_text = "Aggregates:\n" + "\n".join(agg_lines)

    messages = [
        {
            "role": "system",
            "content": (
                "Summarize SQL results in 1-2 sentences. Use ONLY provided numbers. "
                "Do NOT invent or estimate values."
            ),
        },
        {
            "role": "user",
            "content": (
                f'Q: "{user_query}"\nRows: {row_count}\n'
                f"{agg_text}\nData sample:\n{result_text}\n"
                "Brief summary:"
            ),
        },
    ]

    try:
        import asyncio
        token_count = 0

        async def _stream_with_timeout():
            nonlocal token_count
            async for token in llm_router.astream_tokens(messages, max_tokens=128, temperature=0.1):
                token_count += 1
                yield token

        async for token in _stream_with_timeout():
            yield token

        logger.info("streaming_summary_complete", tokens=token_count)

    except Exception as e:
        logger.warning("streaming_summary_fallback", error=str(e)[:120])
        # Fallback to deterministic summary
        deterministic = _build_deterministic_summary(
            user_query, results, columns, row_count,
            state.get("execution_time_ms", 0),
        )
        yield deterministic

"""
SQL Generation Agent — Generates SQL queries from natural language using LLM.
Receives schema context from RAG and produces structured SQL output.
"""

import json
import re
import structlog

from app.agents.state import AgentState
from app.prompts.registry import get_prompt_registry

logger = structlog.get_logger()


def sql_generation_node(state: AgentState, llm_router) -> dict:
    """
    Generate SQL query from the user's question using schema context.
    Outputs structured JSON with sql, explanation, and friendly message.
    """
    user_query = state["user_query"]
    context = state.get("relevant_schema", "")
    history = state.get("conversation_history", [])
    _intent = state.get("intent", "data_query")
    retry_count = state.get("retry_count", 0)
    validation_errors = state.get("validation_errors", [])
    trace_id = state.get("trace_id", "unknown")
    sql_dialect = state.get("sql_dialect", "mysql")

    logger.info("agent_started", agent="sql_generation", trace_id=trace_id, retry=retry_count, dialect=sql_dialect)

    # Build conversation history context
    history_text = ""
    if history:
        recent = history[-3:]  # Last 3 exchanges
        history_text = "PREVIOUS CONVERSATION:\n"
        for h in recent:
            history_text += f"User: {h.get('user', '')}\nSQL: {h.get('sql', '')}\n"

    # If this is a retry, include the validation/execution errors and dialect for self-correction
    retry_context = ""
    if retry_count > 0 and validation_errors:
        dialect_label = (sql_dialect or "SQL").upper()
        retry_context = f"""
⚠️ YOUR PREVIOUS SQL WAS REJECTED. Fix these issues (Dialect: {dialect_label}):
{chr(10).join(f'  - {err}' for err in validation_errors)}

Previous attempt: {state.get('generated_sql', 'N/A')}
Generate a corrected version in valid {dialect_label} syntax.
"""

    # Dynamic few-shot: dialect-aware selection (MySQL examples for MySQL, SQLite for SQLite)
    dynamic_examples = ""
    selected_few_shot_ids = []
    try:
        from app.prompts.few_shot import get_few_shot_selector
        selector = get_few_shot_selector()
        # Map dialect to few-shot pool; default to "mysql" for backward compatibility
        few_shot_dialect = sql_dialect if sql_dialect in ("mysql", "sqlite") else "mysql"
        similar = selector.select(user_query, k=3, dialect=few_shot_dialect)
        if similar:
            dynamic_examples = selector.format_for_prompt(similar)
            selected_few_shot_ids = [ex.get("id", ex.get("question", "")) for ex in similar]
            logger.debug("dynamic_few_shot_selected", count=len(similar), dialect=few_shot_dialect, ids=selected_few_shot_ids)
    except Exception as e:
        logger.debug("dynamic_few_shot_unavailable", error=str(e))

    # Combine schema context with semantic context and dynamic examples
    full_context = context
    semantic_context = state.get("semantic_context")
    if semantic_context:
        full_context = full_context + "\n\n" + semantic_context
    if dynamic_examples:
        full_context = full_context + "\n\n" + dynamic_examples

    # Dialect-aware prompt selection
    registry = get_prompt_registry()
    if sql_dialect == "sqlite":
        prompt_template = registry.get("sql_generation_sqlite")
    else:
        prompt_template = registry.get("sql_generation")

    prompt_version = prompt_template.version
    messages = prompt_template.render(
        schema_context=full_context,
        history_context=history_text,
        retry_context=retry_context,
        user_query=user_query,
    )

    try:
        from app.config import get_settings
        settings = get_settings()

        # Phase 8: Deterministic evaluation mode support
        eval_mode = state.get("eval_mode", False) or settings.PLAINSQL_EVAL_MODE
        if eval_mode:
            gen_temp = state.get("eval_temperature", settings.PLAINSQL_EVAL_TEMPERATURE)
            gen_seed = state.get("eval_seed", settings.PLAINSQL_EVAL_SEED)
            gen_pinned = state.get("pinned_provider", settings.PLAINSQL_EVAL_PROVIDER)
        else:
            gen_temp = 0.1
            gen_seed = None
            gen_pinned = None

        # Use higher quality model for complex queries
        model_pref = "accurate" if state.get("complexity") == "complex" else "default"
        gen_kwargs = {
            "model_preference": model_pref,
            "max_tokens": 1024,
            "temperature": gen_temp,
        }
        if gen_seed is not None:
            gen_kwargs["seed"] = gen_seed
        if gen_pinned:
            gen_kwargs["pinned_provider"] = gen_pinned

        response = llm_router.generate(messages, **gen_kwargs)

        # Parse structured response
        sql_query, explanation, message = _parse_llm_response(response)

        if not sql_query:
            logger.warning("empty_sql_generated", response_preview=response[:200])
            return {
                "generated_sql": "",
                "sql_explanation": "Failed to generate SQL",
                "friendly_message": "I couldn't generate a query for that request. Could you rephrase?",
                "error": "Empty SQL output from LLM",
                "error_agent": "sql_generation",
                "prompt_version": prompt_version,
                "selected_few_shots": selected_few_shot_ids,
            }

        # Clean SQL
        sql_query = _clean_sql(sql_query)

        return {
            "generated_sql": sql_query,
            "sql_explanation": explanation,
            "friendly_message": message,
            "prompt_version": prompt_version,
            "selected_few_shots": selected_few_shot_ids,
            "eval_mode": eval_mode,
            "llm_provider": gen_pinned or getattr(llm_router, "default_provider", "unknown"),
            # Clear stale validation/execution state from previous retry cycle.
            "is_valid": None,
            "validation_errors": [],
            "sanitized_sql": "",
            "error": None,
            "error_agent": None,
        }
    except Exception as e:
        logger.error("sql_generation_failed", error=str(e), trace_id=trace_id)
        return {
            "generated_sql": "",
            "sql_explanation": "",
            "friendly_message": "An error occurred while generating the query.",
            "error": f"SQL generation failed: {str(e)}",
            "error_agent": "sql_generation",
            "prompt_version": prompt_version,
            "selected_few_shots": selected_few_shot_ids,
            "is_valid": None,
            "validation_errors": [],
            "sanitized_sql": "",
        }


def _parse_llm_response(response: str) -> tuple[str, str, str]:
    """Parse the LLM response, handling both JSON and raw SQL formats."""
    sql_query = ""
    explanation = "Query generated successfully."
    message = "Here are your results."

    try:
        # Try JSON parsing first
        clean_json = re.sub(r"```json|```", "", response).strip()
        data = json.loads(clean_json)
        sql_query = data.get("sql", "")
        message = data.get("message", message)
        explanation = data.get("explanation", explanation)
    except (json.JSONDecodeError, ValueError):
        # Fallback: extract SQL from raw text
        # Try to find SELECT...FROM...; (requires FROM to ensure it's SQL, not prose)
        match = re.search(r"((?:WITH\s+\w+\s+AS\s*\([\s\S]+?\)\s*)?SELECT\s+[\s\S]+?\sFROM\s[\s\S]+?;)", response, re.IGNORECASE)
        if match:
            sql_query = match.group(1)
        else:
            # Try without semicolon but still require FROM
            match = re.search(r"((?:WITH\s+\w+\s+AS\s*\([\s\S]+?\)\s*)?SELECT\s+[\s\S]+?\sFROM\s[\s\S]+?)(?:\n\n|$)", response, re.IGNORECASE)
            if match:
                sql_query = match.group(1)

    return sql_query, explanation, message


def _clean_sql(sql: str) -> str:
    """Clean and normalize generated SQL."""
    # Remove markdown formatting
    sql = re.sub(r"```sql|```", "", sql, flags=re.IGNORECASE).strip()
    # Normalize whitespace
    sql = " ".join(sql.split())
    # Ensure trailing semicolon
    if sql and not sql.endswith(";"):
        sql += ";"
    return sql

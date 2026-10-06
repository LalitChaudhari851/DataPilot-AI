"""
Output Guardrails — Schema grounding, confidence scoring, and robust SQL extraction.
Prevents hallucinated table/column references and handles malformed LLM output.
"""

import re
import sqlparse
import structlog

logger = structlog.get_logger()


class OutputGuardrail:
    """
    Validates LLM output before SQL execution:
    1. Schema grounding — ensures SQL only references real tables and columns
    2. SQL extraction — robustly extracts SQL from messy LLM responses
    3. Confidence scoring — detects uncertain/hedging language
    """

    def __init__(self, known_tables: set = None, known_columns: dict = None):
        """
        Args:
            known_tables: Set of valid table names in the database
            known_columns: Dict of {table_name: set of column_names}
        """
        self.known_tables = {t.lower() for t in (known_tables or set())}
        self.known_columns = {
            t.lower(): {c.lower() for c in cols}
            for t, cols in (known_columns or {}).items()
        }
        self.all_columns = set()
        for cols in self.known_columns.values():
            self.all_columns.update(cols)

    def update_schema(self, tables: set, columns: dict):
        """Update known schema when database connection changes."""
        self.known_tables = {t.lower() for t in tables}
        self.known_columns = {
            t.lower(): {c.lower() for c in cols}
            for t, cols in columns.items()
        }
        self.all_columns = set()
        for cols in self.known_columns.values():
            self.all_columns.update(cols)

    def validate_sql_references(self, sql: str) -> list[str]:
        """
        Check that generated SQL only references real tables and columns.
        Returns a list of hallucination warnings (empty = clean).
        """
        if not sql or not self.known_tables:
            return []

        warnings = []

        try:
            parsed = sqlparse.parse(sql)
            if not parsed:
                return warnings

            # Comprehensive SQL functions to skip
            sql_functions = {
                "count", "sum", "avg", "min", "max", "round", "coalesce",
                "ifnull", "isnull", "nullif", "concat", "substring", "trim",
                "upper", "lower", "length", "cast", "convert", "date_format",
                "date_sub", "date_add", "curdate", "now", "year", "month",
                "day", "hour", "minute", "second", "datediff", "timestampdiff",
                "group_concat", "distinct", "if", "abs", "ceil", "floor",
                "power", "sqrt", "mod", "sign", "truncate", "replace",
                "lpad", "rpad", "left", "right", "reverse", "space",
                "char_length", "character_length", "locate", "instr",
                "field", "elt", "format", "hex", "unhex", "crc32",
                "last_insert_id", "row_number", "rank", "dense_rank",
                "ntile", "lag", "lead", "first_value", "last_value",
                "nth_value", "percent_rank", "cume_dist", "quarter",
                "week", "dayofweek", "dayofyear", "monthname", "dayname",
                "time", "timestamp", "str_to_date", "date",
                "strftime", "julianday", "total", "iif", "datetime", "unixepoch",
            }

            # SQL keywords that appear as Name tokens
            sql_keywords = {
                "asc", "desc", "limit", "offset", "as", "on", "and", "or",
                "not", "in", "between", "like", "is", "null", "true", "false",
                "inner", "left", "right", "outer", "cross", "natural",
                "select", "from", "where", "join", "group", "order", "having",
                "by", "union", "except", "intersect", "with", "recursive",
                "case", "when", "then", "else", "end", "exists", "any", "all",
                "interval", "rows", "unbounded", "preceding", "following",
                "over", "partition", "range", "current", "row",
                "rollup", "cube", "grouping", "sets",
            }

            # ── Extract ALL defined aliases ──────────────────
            defined_aliases = set()

            # 1. SELECT ... AS alias_name (column aliases)
            for m in re.finditer(r'\bAS\s+`?(\w+)`?', sql, re.IGNORECASE):
                defined_aliases.add(m.group(1).lower())

            # 2. FROM/JOIN table aliases
            for m in re.finditer(
                r'(?:FROM|JOIN)\s+`?(\w+)`?\s+(?:AS\s+)?`?(\w+)`?',
                sql, re.IGNORECASE,
            ):
                alias = m.group(2).lower()
                table = m.group(1).lower()
                if alias != table and alias not in sql_keywords:
                    defined_aliases.add(alias)

            # 3. CTE names: WITH cte_name AS (...)
            for m in re.finditer(r'\bWITH\s+(\w+)\s+AS\s*\(', sql, re.IGNORECASE):
                defined_aliases.add(m.group(1).lower())

            for token in parsed[0].flatten():
                if token.ttype is sqlparse.tokens.Name:
                    name = token.value.lower().strip('`"[]')

                    # Skip known SQL functions and keywords
                    if name in sql_functions or name in sql_keywords:
                        continue

                    # Skip single-char aliases (e.g., e, s, p, c, d, t)
                    if len(name) <= 1:
                        continue

                    # Skip defined aliases (e.g., dept, emp, avg_salary)
                    if name in defined_aliases:
                        continue

                    # Check against known schema
                    if name not in self.known_tables and name not in self.all_columns:
                        warnings.append(f"Unknown reference: '{name}' — not a known table or column")

        except Exception as e:
            logger.warning("guardrail_parse_failed", error=str(e))

        return warnings

    def extract_sql_from_response(self, llm_output: str) -> str:
        """
        Robustly extract SQL from potentially messy LLM output.
        Handles:
        - JSON-wrapped responses
        - Markdown code blocks
        - Raw SQL with explanatory text
        - Multiple SQL statements (returns first SELECT)
        """
        if not llm_output:
            return ""

        text = llm_output.strip()

        # Strategy 1: Try JSON extraction
        try:
            # Remove markdown JSON blocks
            clean = re.sub(r"```(?:json)?\s*", "", text)
            clean = re.sub(r"```\s*$", "", clean, flags=re.MULTILINE)
            import json
            data = json.loads(clean.strip())
            if isinstance(data, dict) and "sql" in data:
                sql = data["sql"].strip()
                if sql:
                    return self._clean_extracted_sql(sql)
        except (ValueError, TypeError):
            pass

        # Strategy 2: Extract from markdown SQL block
        sql_block = re.search(r"```sql\s*\n?(.*?)\n?```", text, re.DOTALL | re.IGNORECASE)
        if sql_block:
            return self._clean_extracted_sql(sql_block.group(1))

        # Strategy 3: Extract from generic code block
        code_block = re.search(r"```\s*\n?(.*?)\n?```", text, re.DOTALL)
        if code_block:
            candidate = code_block.group(1).strip()
            if re.match(r"(?:WITH|SELECT|INSERT|UPDATE|DELETE)\b", candidate, re.IGNORECASE):
                return self._clean_extracted_sql(candidate)

        # Strategy 4: Find standalone SQL statement
        sql_match = re.search(
            r"((?:WITH\s+\w+\s+AS\s*\(.*?\)\s*)?SELECT\s[\s\S]+?)(?:;|\n\n|$)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if sql_match:
            return self._clean_extracted_sql(sql_match.group(1))

        # Strategy 5: Return empty if nothing found
        logger.warning("sql_extraction_failed", output_preview=text[:200])
        return ""

    def score_confidence(self, llm_output: str) -> float:
        """
        Score LLM confidence from 0.0 to 1.0 based on language analysis.
        Low confidence suggests asking for clarification instead of executing.
        """
        if not llm_output:
            return 0.0

        text = llm_output.lower()

        # Hedging phrases that indicate low confidence
        hedging_phrases = [
            "i'm not sure", "i am not sure",
            "i think", "maybe", "perhaps", "possibly",
            "i don't have enough", "unclear",
            "ambiguous", "could you clarify",
            "i'm guessing", "it's hard to tell",
            "without more context", "assuming",
            "i cannot determine", "not enough information",
        ]

        hedging_count = sum(1 for phrase in hedging_phrases if phrase in text)

        # Check if SQL was actually generated
        has_sql = bool(re.search(r"\bSELECT\b", text, re.IGNORECASE))

        # Base confidence
        confidence = 1.0 if has_sql else 0.3

        # Penalize for hedging
        confidence -= hedging_count * 0.15

        # Penalize for error indicators
        if "error" in text or "cannot" in text or "unable to" in text:
            confidence -= 0.3

        return max(0.0, min(1.0, confidence))

    def _clean_extracted_sql(self, sql: str) -> str:
        """Normalize extracted SQL."""
        # Remove leading/trailing whitespace and markdown artifacts
        sql = sql.strip()
        sql = re.sub(r"```\w*", "", sql).strip()
        # Normalize whitespace
        sql = " ".join(sql.split())
        # Ensure trailing semicolon
        if sql and not sql.endswith(";"):
            sql += ";"
        return sql

    def validate_output(self, llm_output: str) -> dict:
        """
        Full output validation pipeline.
        Returns a dict with sql, confidence, warnings, and is_safe.
        """
        sql = self.extract_sql_from_response(llm_output)
        confidence = self.score_confidence(llm_output)
        warnings = self.validate_sql_references(sql) if sql else []

        is_safe = (
            bool(sql)
            and confidence >= 0.4
            and len(warnings) == 0
        )

        return {
            "sql": sql,
            "confidence": round(confidence, 2),
            "warnings": warnings,
            "is_safe": is_safe,
            "needs_clarification": confidence < 0.4,
        }

"""
Prompt Registry — Versioned prompt template management.
Centralizes all LLM prompts for the system, enabling versioning,
A/B testing, and easy rollback without code changes.
"""

import structlog
from typing import Optional

logger = structlog.get_logger()


class PromptTemplate:
    """A versioned prompt template with variable substitution."""

    def __init__(self, name: str, version: str, system: str, user: str, description: str = ""):
        self.name = name
        self.version = version
        self.system = system
        self.user = user
        self.description = description

    def render(self, **kwargs) -> list[dict]:
        """Render the template with the given variables into chat messages."""
        system_content = self.system.format(**kwargs) if kwargs else self.system
        user_content = self.user.format(**kwargs) if kwargs else self.user
        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]


class PromptRegistry:
    """
    Central registry for all prompt templates used in the system.
    Supports multiple versions per template for A/B testing and rollback.
    """

    def __init__(self):
        self._templates: dict[str, dict[str, PromptTemplate]] = {}
        self._active_versions: dict[str, str] = {}
        self._register_defaults()

    def register(self, template: PromptTemplate, set_active: bool = True):
        """Register a prompt template version."""
        if template.name not in self._templates:
            self._templates[template.name] = {}
        self._templates[template.name][template.version] = template
        if set_active:
            self._active_versions[template.name] = template.version
        logger.info("prompt_registered", name=template.name, version=template.version, active=set_active)

    def get(self, name: str, version: Optional[str] = None) -> PromptTemplate:
        """Get a prompt template by name (and optionally version)."""
        if name not in self._templates:
            raise KeyError(f"Prompt template '{name}' not found")
        
        target_version = version or self._active_versions.get(name)
        if not target_version or target_version not in self._templates[name]:
            raise KeyError(f"Version '{target_version}' not found for prompt '{name}'")
        
        return self._templates[name][target_version]

    def set_active_version(self, name: str, version: str):
        """Switch the active version of a prompt template."""
        if name not in self._templates or version not in self._templates[name]:
            raise KeyError(f"Template '{name}' version '{version}' not found")
        self._active_versions[name] = version
        logger.info("prompt_version_switched", name=name, version=version)

    def list_templates(self) -> dict[str, dict]:
        """List all registered templates with their versions."""
        return {
            name: {
                "active_version": self._active_versions.get(name),
                "versions": list(versions.keys()),
                "description": versions.get(self._active_versions.get(name, ""), 
                    PromptTemplate("", "", "", "")).description,
            }
            for name, versions in self._templates.items()
        }

    # ── Default Prompt Templates ──────────────────────────

    def _register_defaults(self):
        """Register all default prompt templates used by the agent pipeline."""

        # ── Query Classification ─────────────────────────
        self.register(PromptTemplate(
            name="query_classification",
            version="v1",
            description="Classifies user intent and extracts entities for routing",
            system="You are a query classifier. Respond ONLY with valid JSON.",
            user="""Classify this user message and extract any table or column names mentioned.

User Query: "{user_query}"

Respond ONLY with valid JSON:
{{
  "intent": "chat|sql",
  "route_intent": "data_query|aggregation|comparison|explanation",
  "entities": ["table_or_column_names_found"],
  "complexity": "simple|moderate|complex"
}}

Intent rules:
- "chat": Greetings, thanks, capability questions, or general conversation that does not ask for database data
- "sql": Database-related requests that need schema retrieval and SQL generation

Route intent rules for SQL messages:
- "data_query": Fetching specific rows or records (SELECT with WHERE)
- "aggregation": Counting, summing, averaging, grouping (COUNT, SUM, AVG, GROUP BY)
- "comparison": Comparing two datasets or time periods
- "explanation": Asking to explain a previous result or query

If the message is "chat", set route_intent to "data_query" and keep entities empty.

Complexity rules:
- "simple": Single table, no joins
- "moderate": 1-2 joins, some aggregation
- "complex": Multiple joins, subqueries, window functions""",
        ))

        # ── SQL Generation v1 (baseline — kept for rollback) ──
        self.register(PromptTemplate(
            name="sql_generation",
            version="v1",
            description="Baseline SQL generation — no few-shot examples. Kept for rollback and A/B comparison.",
            system="""You are an elite SQL expert for MySQL databases.

DATABASE SCHEMA:
{schema_context}

{history_context}

{retry_context}

RULES:
1. Output ONLY valid JSON: {{ "sql": "SELECT ...", "message": "friendly explanation for user", "explanation": "technical breakdown" }}
2. Query MUST be Read-Only (SELECT or WITH...SELECT only).
3. NEVER use DELETE, DROP, UPDATE, INSERT, ALTER, TRUNCATE, or any data-modification statement.
4. Always use exact table and column names from the schema above.
5. Use proper JOINs when querying across tables — check the Relationships section.
6. Include LIMIT 100 unless the user specifically asks for all data.
7. Do NOT wrap output in markdown code blocks.
8. For aggregation queries, always include meaningful column aliases.
9. Handle NULL values appropriately in filters.
10. Only generate SQL for database-related requests. If the request is conversational or unrelated to the schema, return {{ "sql": "", "message": "I can answer chat directly, but SQL generation only handles database questions.", "explanation": "Non-database request" }}.""",
            user="{user_query}",
        ), set_active=False)  # Demoted — v2 is now the active default

        # ── SQL Generation v2 (Few-Shot + Chain-of-Thought) — ACTIVE DEFAULT ──
        self.register(PromptTemplate(
            name="sql_generation",
            version="v2",
            description="SQL generation with few-shot examples and chain-of-thought reasoning — active default.",
            system="""You are an elite SQL expert for MySQL databases.

DATABASE SCHEMA:
{schema_context}

{history_context}

{retry_context}

FEW-SHOT EXAMPLES:

Example 1:
Question: "Show top 5 employees by salary"
Thinking: Single table query on employees, ORDER BY salary DESC, LIMIT 5.
SQL: SELECT name, salary FROM employees ORDER BY salary DESC LIMIT 5

Example 2:
Question: "Total sales revenue by region"
Thinking: Need to join sales with customers (for region). Aggregate SUM on sales.total_amount, GROUP BY customer.region.
SQL: SELECT c.region, SUM(s.total_amount) AS revenue FROM sales s JOIN customers c ON s.customer_id = c.id GROUP BY c.region ORDER BY revenue DESC

Example 3:
Question: "Which department has the highest average salary?"
Thinking: Join employees with departments. AVG(salary) grouped by department name. ORDER DESC, LIMIT 1 for highest.
SQL: SELECT d.name AS department, AVG(e.salary) AS avg_salary FROM employees e JOIN departments d ON e.department_id = d.id GROUP BY d.name ORDER BY avg_salary DESC LIMIT 1

INSTRUCTIONS:
1. First, reason step-by-step about which tables and joins are needed (chain-of-thought).
2. Then generate the SQL query.
3. Output ONLY valid JSON: {{ "sql": "SELECT ...", "message": "friendly explanation", "explanation": "step-by-step reasoning" }}
4. Query MUST be Read-Only (SELECT or WITH...SELECT only).
5. Always use exact table and column names from the schema.
6. Use proper JOINs when querying across tables.
7. Include LIMIT 100 unless the user asks for all data.
8. Do NOT wrap output in markdown code blocks.
9. For aggregation queries, always include meaningful column aliases.
10. Handle NULL values appropriately.""",
            user="{user_query}",
        ), set_active=False)  # Demoted — v3 is now the active default

        # ── SQL Generation v3 (Production — Strict Grounding) — ACTIVE DEFAULT ──
        self.register(PromptTemplate(
            name="sql_generation",
            version="v3",
            description="Production prompt with strict schema grounding, anti-hallucination rules, and expanded few-shot examples.",
            system="""You are an expert MySQL query generator. You MUST follow these rules exactly.

## AVAILABLE SCHEMA
{schema_context}

## STRICT RULES
1. Use ONLY tables and columns listed in AVAILABLE SCHEMA above.
2. NEVER invent column names. If unsure which columns exist, use SELECT * FROM table_name LIMIT 10.
3. Output ONLY valid JSON: {{"sql": "...", "message": "...", "explanation": "..."}}
4. Read-only queries ONLY (SELECT, WITH...SELECT). Never use DELETE, DROP, UPDATE, INSERT, ALTER, TRUNCATE.
5. Use JOINs based on the Relationships / Foreign Key section in the schema.
6. Add LIMIT 100 ONLY to data-listing queries that return individual rows. Do NOT add LIMIT to:
   - Aggregation queries (GROUP BY) unless the user asks for "top N"
   - Scalar aggregations (single COUNT/SUM/AVG result)
   - Queries where the user explicitly asks for "all" results
7. When the user asks for "top N", use ORDER BY ... DESC LIMIT N.
8. For "highest" / "most expensive" / "maximum", use ORDER BY col DESC LIMIT 1 — NOT MAX(col) with non-aggregated columns in SELECT.
9. Always include ORDER BY when results should be ranked or sorted.
10. Do NOT wrap output in markdown code blocks.
11. Use meaningful column aliases with AS for aggregated values (e.g., AS total_revenue, AS avg_salary).
12. Handle NULL values appropriately in filters and LEFT JOINs.
13. NEVER use SELECT column aliases (like 'yr', 'qtr', 'month') in the GROUP BY clause. Always GROUP BY the actual column or database function call itself (e.g., use GROUP BY YEAR(invoice_date), QUARTER(invoice_date) instead of GROUP BY yr, qtr).
14. When writing CTEs (Common Table Expressions) that use GROUP BY, ensure all grouping fields are defined in the CTE's SELECT list using proper table columns or expressions. Do not group by undefined aliases.
15. When calculating Net Revenue Retention (NRR) or subscription cohorts, avoid complex self-joins on quarters. Simply calculate the ratio of active contracted ARR to total contracted ARR for subscriptions starting in each period (e.g., SELECT a.segment, YEAR(s.start_date) AS yr, QUARTER(s.start_date) AS qtr, SUM(CASE WHEN s.status = 'active' THEN s.contracted_arr ELSE 0 END) AS active_arr, SUM(s.contracted_arr) AS total_arr, ROUND(SUM(CASE WHEN s.status = 'active' THEN s.contracted_arr ELSE 0 END) * 100.0 / SUM(s.contracted_arr), 2) AS nrr_pct FROM subscriptions s JOIN accounts a ON s.account_id = a.account_id WHERE s.start_date >= DATE_SUB((SELECT MAX(start_date) FROM subscriptions), INTERVAL 1 YEAR) GROUP BY a.segment, YEAR(s.start_date), QUARTER(s.start_date) ORDER BY yr DESC, qtr DESC, nrr_pct DESC).

{history_context}
{retry_context}

## EXAMPLES

Example 1 — Simple data query:
Q: "Show top 5 employees by salary"
Reasoning: Single table, order by salary descending, limit 5.
A: {{"sql": "SELECT name, salary FROM employees ORDER BY salary DESC LIMIT 5", "message": "Here are the top 5 highest-paid employees.", "explanation": "Query employees table, sort by salary DESC, limit to 5."}}

Example 2 — Aggregation with JOIN:
Q: "Total sales revenue by region"
Reasoning: Need to join sales with customers (for region). SUM total_amount, GROUP BY customer.region.
A: {{"sql": "SELECT c.region, SUM(s.total_amount) AS revenue FROM sales s JOIN customers c ON s.customer_id = c.id GROUP BY c.region ORDER BY revenue DESC", "message": "Sales revenue broken down by region.", "explanation": "Join sales with customers, aggregate by region."}}

Example 3 — LEFT JOIN (finding missing records):
Q: "Find products that have never been sold"
Reasoning: LEFT JOIN products to sales, filter WHERE sales side IS NULL.
A: {{"sql": "SELECT p.name, p.category, p.price FROM products p LEFT JOIN sales s ON p.id = s.product_id WHERE s.sale_id IS NULL", "message": "Products with no sales records.", "explanation": "LEFT JOIN to find unmatched products."}}

Example 4 — Subquery (percentage calculation):
Q: "What percentage of total sales comes from each region?"
Reasoning: Subquery for total, divide each region's sum by total, multiply by 100.
A: {{"sql": "SELECT c.region, SUM(s.total_amount) AS revenue, ROUND(SUM(s.total_amount) * 100.0 / (SELECT SUM(total_amount) FROM sales), 2) AS percentage FROM sales s JOIN customers c ON s.customer_id = c.id GROUP BY c.region ORDER BY percentage DESC", "message": "Regional contribution to total sales.", "explanation": "Scalar subquery for total, percentage calculation per region."}}

Example 5 — Window function:
Q: "Show the running total of sales by date"
Reasoning: GROUP BY sale_date for daily totals, window SUM for running total.
A: {{"sql": "SELECT sale_date, SUM(total_amount) AS daily_total, SUM(SUM(total_amount)) OVER (ORDER BY sale_date) AS running_total FROM sales GROUP BY sale_date ORDER BY sale_date", "message": "Daily sales with running cumulative total.", "explanation": "Nested aggregate with window function for running total."}}""",
            user="{user_query}",
        ))  # set_active defaults to True — v3 is now the active sql_generation prompt

        # ── SQL Generation SQLite v1 (Dialect-Aware) — kept for rollback ───
        self.register(PromptTemplate(
            name="sql_generation_sqlite",
            version="v1",
            description="SQLite-specific SQL generation with dialect rules and SQLite-compatible functions. Kept for rollback.",
            system="""You are an expert SQLite query generator. You MUST generate valid SQLite SQL following these rules exactly.

## SQL DIALECT: SQLite
You MUST write SQLite-compatible SQL. Do NOT use MySQL, PostgreSQL, or SQL Server specific functions or syntax.

## AVAILABLE SCHEMA
{schema_context}

## STRICT RULES
1. Output ONLY valid JSON: {{"sql": "...", "message": "...", "explanation": "..."}}
2. Read-only queries ONLY (SELECT, WITH...SELECT). NEVER use DELETE, DROP, UPDATE, INSERT, ALTER, TRUNCATE, ATTACH.
3. Use ONLY tables and columns listed in AVAILABLE SCHEMA above.
4. NEVER invent table or column names.
5. SQLite Syntax Requirements:
   - Date formatting: Use strftime('%Y-%m', date_col) or strftime('%Y', date_col) — NEVER DATE_FORMAT() or YEAR() or MONTH().
   - Current date/time: Use date('now') or datetime('now') — NEVER CURDATE(), NOW(), or CURRENT_DATE().
   - Date arithmetic: Use date(col, '+1 month') or date('now', '-30 days') — NEVER DATE_ADD(), DATE_SUB(), or INTERVAL syntax.
   - Date difference in days: Use (julianday(date1) - julianday(date2)) — NEVER DATEDIFF().
   - String concatenation: Use || (e.g., col1 || ' ' || col2) — NEVER CONCAT().
   - Conditional logic: Use CASE WHEN ... THEN ... ELSE ... END or iif(cond, val1, val2) — NEVER IF().
   - Null handling: Use COALESCE(col, val) or IFNULL(col, val).
   - Aggregations: Use group_concat(DISTINCT col) or group_concat(col, ', ') — NEVER GROUP_CONCAT(col SEPARATOR ', ').
   - Substring: Use substr(col, start, len) — NEVER SUBSTRING().
6. Add LIMIT 100 ONLY to data-listing queries returning individual records. Do NOT add LIMIT to:
   - Aggregation queries (GROUP BY) unless asking for "top N"
   - Scalar aggregations (COUNT, SUM, AVG)
   - Queries where user asks for "all" records
7. When user asks for "top N", use ORDER BY col DESC LIMIT N.
8. For "highest" / "maximum", use ORDER BY col DESC LIMIT 1.
9. Always include ORDER BY when results should be sorted or ranked.
10. Do NOT wrap JSON output in markdown code blocks.

{history_context}
{retry_context}

## DIALECT EXAMPLES (SQLite vs MySQL):

Example 1 — Date Extraction:
Q: "Count orders placed in each month"
Thinking: SQLite requires strftime for date parts.
A: {{"sql": "SELECT strftime('%Y-%m', order_purchase_timestamp) AS order_month, COUNT(*) AS total_orders FROM orders GROUP BY strftime('%Y-%m', order_purchase_timestamp) ORDER BY order_month DESC", "message": "Order counts grouped by month.", "explanation": "Use strftime for monthly grouping in SQLite."}}

Example 2 — Aggregation & String Concatenation:
Q: "Customer full address"
Thinking: Concatenate strings with || operator in SQLite.
A: {{"sql": "SELECT customer_id, customer_city || ', ' || customer_state AS location FROM customers LIMIT 100", "message": "Customer locations formatted as city, state.", "explanation": "Use string concatenation with || operator."}}

Example 3 — Current Date Comparison:
Q: "Orders placed in the last 30 days"
Thinking: Use date('now', '-30 days') in SQLite.
A: {{"sql": "SELECT order_id, order_purchase_timestamp, order_status FROM orders WHERE order_purchase_timestamp >= date('now', '-30 days') ORDER BY order_purchase_timestamp DESC", "message": "Recent orders from the last 30 days.", "explanation": "Filter using SQLite date('now', '-30 days') modifier."}}

Example 4 — Group Concat & Join:
Q: "List categories for each seller"
Thinking: SQLite group_concat takes (col, separator).
A: {{"sql": "SELECT seller_id, group_concat(DISTINCT category) AS categories FROM products GROUP BY seller_id LIMIT 100", "message": "Product categories per seller.", "explanation": "SQLite group_concat aggregate function."}}""",
            user="{user_query}",
        ), set_active=False)  # Demoted — v2 is now the active default

        # ── SQL Generation SQLite v2 (Projection Guidance + Dynamic Few-Shot) — ACTIVE ──
        self.register(PromptTemplate(
            name="sql_generation_sqlite",
            version="v2",
            description="Enhanced SQLite prompt with projection/identifier guidance and dynamic few-shot integration.",
            system="""You are an expert SQLite query generator. You MUST generate valid SQLite SQL following these rules exactly.

## SQL DIALECT: SQLite
You MUST write SQLite-compatible SQL. Do NOT use MySQL, PostgreSQL, or SQL Server specific functions or syntax.

## AVAILABLE SCHEMA
{schema_context}

## STRICT RULES
1. Output ONLY valid JSON: {{"sql": "...", "message": "...", "explanation": "..."}}
2. Read-only queries ONLY (SELECT, WITH...SELECT). NEVER use DELETE, DROP, UPDATE, INSERT, ALTER, TRUNCATE, ATTACH.
3. Use ONLY tables and columns listed in AVAILABLE SCHEMA above.
4. NEVER invent table or column names. If unsure which columns exist, use SELECT * FROM table_name LIMIT 10.
5. SQLite Syntax Requirements:
   - Date formatting: Use strftime('%Y-%m', date_col) or strftime('%Y', date_col) — NEVER DATE_FORMAT() or YEAR() or MONTH().
   - Current date/time: Use date('now') or datetime('now') — NEVER CURDATE(), NOW(), or CURRENT_DATE().
   - Date arithmetic: Use date(col, '+1 month') or date('now', '-30 days') — NEVER DATE_ADD(), DATE_SUB(), or INTERVAL syntax.
   - Date difference in days: Use (julianday(date1) - julianday(date2)) — NEVER DATEDIFF().
   - String concatenation: Use || (e.g., col1 || ' ' || col2) — NEVER CONCAT().
   - Conditional logic: Use CASE WHEN ... THEN ... ELSE ... END or iif(cond, val1, val2) — NEVER IF().
   - Null handling: Use COALESCE(col, val) or IFNULL(col, val).
   - Aggregations: Use group_concat(DISTINCT col) or group_concat(col, ', ') — NEVER GROUP_CONCAT(col SEPARATOR ', ').
   - Substring: Use substr(col, start, len) — NEVER SUBSTRING().
   - Boolean comparisons: Use col = 1 or col = 0 — NEVER col = TRUE or col = FALSE.
6. Add LIMIT 100 ONLY to data-listing queries returning individual records. Do NOT add LIMIT to:
   - Aggregation queries (GROUP BY) unless asking for "top N"
   - Scalar aggregations (COUNT, SUM, AVG)
   - Queries where user asks for "all" records
7. When user asks for "top N", use ORDER BY col DESC LIMIT N.
8. For "highest" / "maximum", use ORDER BY col DESC LIMIT 1.
9. Always include ORDER BY when results should be sorted or ranked.
10. Do NOT wrap JSON output in markdown code blocks.

## COLUMN SELECTION / PROJECTION GUIDANCE
11. SELECT only the columns that directly answer the user's question. Avoid SELECT * in production queries.
12. For "how many" / "count" questions: Use SELECT COUNT(*) — do NOT add extra columns unless the user specifically asks for them.
13. For "top N by X" questions: SELECT the ranking column (X), the identifier (name/id), and at most 1-2 supporting columns. Do NOT select every column in the table.
14. For aggregation queries: SELECT only the GROUP BY dimension(s) and the aggregated metric(s). Do NOT add unrelated columns.
15. Always use the EXACT column names from the schema. Column names in SQLite are case-insensitive but prefer the original casing from the schema.
16. When joining tables, always qualify column names with table aliases (e.g., t.column_name) to avoid ambiguity.
17. When a ranking, maximum/minimum, or extremum query is qualified by a temporal dimension such as 'per year', 'by year', 'in a season', 'in a single season', 'per month', 'by month', or similar temporal scope, and the relevant temporal column exists in the schema, include the temporal dimension in the SELECT projection when it is semantically relevant to identifying the result (e.g., SELECT name, year, w FROM team ORDER BY w DESC LIMIT 5). Important: Do NOT add temporal columns if no such column exists in the schema or if it is not semantically relevant.
18. Business Terminology Guidance (Volume vs Revenue):
   - For unit/quantity concepts ('units sold', 'quantity sold', 'number of items sold', 'item count', 'transaction count', 'item sales volume'): use COUNT(order_item_id) or SUM(quantity) according to the schema.
   - For financial concepts ('revenue', 'sales revenue', 'sales value', 'monetary sales', 'turnover', 'total amount'): use SUM(price), SUM(total_amount), or SUM(price * quantity) according to available financial columns.
   - For ambiguous terms such as 'sales volume', inspect user wording and retrieved schema columns: if the question refers to 'item sales volume' or the schema features item rows/quantities, prefer unit volume/count; if the schema features monetary amounts without item counts or the user implies financial volume, use monetary sums. Do not hardcode a single universal rule.

{history_context}
{retry_context}

## DIALECT EXAMPLES (SQLite):

Example 1 — Simple count (minimal projection):
Q: "How many total records are in the table?"
Thinking: Simple COUNT query. No extra columns needed.
A: {{"sql": "SELECT COUNT(*) AS total_count FROM table_name;", "message": "Total record count.", "explanation": "Scalar count — no GROUP BY, no extra columns."}}

Example 2 — Top N with minimal columns:
Q: "Show top 5 items by value"
Thinking: Need the item identifier and the ranking column only.
A: {{"sql": "SELECT name, value FROM items ORDER BY value DESC LIMIT 5;", "message": "Top 5 items by value.", "explanation": "Select only name and value for ranking."}}

Example 3 — Date grouping with strftime:
Q: "Count orders placed in each month"
Thinking: SQLite requires strftime for date parts.
A: {{"sql": "SELECT strftime('%Y-%m', order_date) AS order_month, COUNT(*) AS total_orders FROM orders GROUP BY strftime('%Y-%m', order_date) ORDER BY order_month;", "message": "Order counts grouped by month.", "explanation": "Use strftime for monthly grouping in SQLite."}}

Example 4 — Join with aggregation:
Q: "Total revenue by customer region"
Thinking: Join orders with customers, SUM amount, GROUP BY region.
A: {{"sql": "SELECT c.region, SUM(o.amount) AS total_revenue FROM orders o JOIN customers c ON o.customer_id = c.customer_id GROUP BY c.region ORDER BY total_revenue DESC;", "message": "Revenue by region.", "explanation": "Join + aggregate, qualified column names."}}

Example 5 — LEFT JOIN (finding unmatched records):
Q: "Find products that have never been ordered"
Thinking: LEFT JOIN products to order_items, filter WHERE order side IS NULL.
A: {{"sql": "SELECT p.name, p.category FROM products p LEFT JOIN order_items oi ON p.product_id = oi.product_id WHERE oi.product_id IS NULL;", "message": "Products with no orders.", "explanation": "LEFT JOIN to find unmatched products."}}

Example 6 — Percentage with subquery:
Q: "What percentage of total sales comes from each category?"
Thinking: Subquery for total, divide each category's sum by total.
A: {{"sql": "SELECT category, SUM(amount) AS category_total, ROUND(SUM(amount) * 100.0 / (SELECT SUM(amount) FROM sales), 2) AS percentage FROM sales GROUP BY category ORDER BY percentage DESC;", "message": "Category contribution percentages.", "explanation": "Scalar subquery for total, percentage per category."}}""",
            user="{user_query}",
        ))

        # ── Query Classification v2 (Enhanced) ─────────
        self.register(PromptTemplate(
            name="query_classification",
            version="v2",
            description="Enhanced intent classification with better examples and meta_query support",
            system="You are a query classifier. Respond ONLY with valid JSON.",
            user="""Classify this user message and extract any table or column names mentioned.

User Query: "{user_query}"

Examples:
- "Hello, what can you do?" → {{ "intent": "chat", "route_intent": "data_query", "entities": [], "complexity": "simple" }}
- "Show top 5 employees by salary" → {{ "intent": "sql", "route_intent": "data_query", "entities": ["employees", "salary"], "complexity": "simple" }}
- "Total revenue by region" → {{ "intent": "sql", "route_intent": "aggregation", "entities": ["sales", "region"], "complexity": "moderate" }}
- "Compare Q1 vs Q2 sales" → {{ "intent": "sql", "route_intent": "comparison", "entities": ["sales"], "complexity": "complex" }}
- "What tables do you have?" → {{ "intent": "sql", "route_intent": "meta_query", "entities": [], "complexity": "simple" }}

Respond ONLY with valid JSON:
{{
  "intent": "chat|sql",
  "route_intent": "data_query|aggregation|comparison|explanation|meta_query",
  "entities": ["table_or_column_names_found"],
  "complexity": "simple|moderate|complex"
}}""",
        ), set_active=False)  # v1 remains default; v2 available for A/B testing

        # ── SQL Explanation ───────────────────────────
        self.register(PromptTemplate(
            name="sql_explanation",
            version="v1",
            description="Explains SQL queries in plain English for non-technical users",
            system="You are a helpful data analyst explaining queries to business stakeholders. Be concise.",
            user="""Explain this SQL query in simple, non-technical English.
Write 2-3 sentences that a business person would understand.

SQL Query:
{sql}

Number of results returned: {results_count}

Explain:
1. What data it retrieves
2. Any filters or conditions applied  
3. How results are organized (sorting/grouping)

Be concise and avoid technical jargon.""",
        ))




# ── Module-level singleton ───────────────────────────────
_registry = PromptRegistry()


def get_prompt_registry() -> PromptRegistry:
    """Get the global prompt registry singleton."""
    return _registry

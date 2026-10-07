"""
Intent classifier for routing user input before SQL generation.

Uses a hybrid approach:
1. ML model (sentence-transformers + LogisticRegression) when available and confident
2. Heuristic keyword matching as a fallback

Clear chat messages return fast and never touch schema retrieval or SQL
generation. Database-shaped requests continue through the existing SQL path.
"""

from dataclasses import dataclass
from typing import Literal
import re
import structlog

logger = structlog.get_logger()

IntentKind = Literal["chat", "sql", "ambiguous"]
RouteIntent = Literal["chat", "ambiguous", "meta_query", "data_query", "aggregation", "comparison", "explanation"]

# Minimum confidence from the ML model to trust its classification
_ML_CONFIDENCE_THRESHOLD = 0.70


@dataclass(frozen=True)
class IntentClassification:
    intent: IntentKind
    route_intent: RouteIntent
    complexity: Literal["simple", "moderate", "complex"] = "simple"
    reason: str = "heuristic"


GREETING_PATTERNS = {
    "hello", "hi", "hey", "good morning", "good afternoon", "good evening",
    "hola", "sup", "what's up", "howdy", "yo", "greetings", "namaste", "bonjour",
}

CHAT_EXACT = {
    "thanks", "thank you", "thank u", "thx", "ty", "ok", "okay", "cool",
    "great", "nice", "awesome", "got it", "bye", "goodbye", "see you",
    "see ya", "later", "yes", "no", "yep", "nope", "sure", "nah",
    "help", "help me", "what can you do", "who are you", "what are you",
    "how are you", "how do you work", "how does this work",
    "tell me about yourself", "what is this", "what is plainsql", "what is datapilot",
}

CHAT_PREFIXES = {
    "thanks for", "thank you for", "can you help", "please help",
    "i need help", "i don't understand", "what do you do", "what can i ask",
    "how do i use", "tell me about you", "who made you", "are you",
    "tell me a joke", "how is the weather",
    "tell me about yourself", "do you know about",
}

DATA_SIGNAL_PHRASES = {
    "how many", "how much", "group by", "order by", "by region",
    "by department", "by category", "by month", "by year",
}

# Strong signals — specific enough to indicate a real SQL query
DATA_SIGNAL_WORDS_STRONG = {
    "show", "list", "find", "get", "select", "count", "total", "average",
    "avg", "sum", "top", "bottom", "highest", "lowest", "maximum",
    "minimum", "max", "min", "most", "least", "revenue", "sales",
    "salary", "salaries", "price", "stock", "spend", "employees",
    "employee", "customers", "customer", "products", "product",
    "departments", "department", "orders", "order",
    "where", "between", "greater", "less", "equal", "filter", "sort",
    "compare", "vs", "versus", "per", "each", "join", "query", "report",
}

# Weak signals — too generic alone, need a strong signal to confirm SQL intent
DATA_SIGNAL_WORDS_WEAK = {
    "table", "tables", "column", "columns", "rows", "records",
    "all", "every", "data", "database", "info", "information",
    "about", "details",
}

DATA_SIGNAL_WORDS = DATA_SIGNAL_WORDS_STRONG | DATA_SIGNAL_WORDS_WEAK

# Known database table names — used to validate ambiguous queries
KNOWN_TABLES = {
    "employees", "employee", "departments", "department",
    "products", "product", "customers", "customer",
    "sales", "sale", "orders", "order",
}

META_KEYWORDS = {
    "what tables", "show tables", "list tables", "what columns",
    "describe table", "schema", "what database",
}
AGGREGATION_KEYWORDS = {
    "count", "total", "sum", "average", "avg", "minimum", "maximum",
    "min", "max", "group by", "by region", "by department", "by category",
    "by month", "by year",
}
COMPARISON_KEYWORDS = {"compare", " vs ", " versus ", "between"}
EXPLANATION_KEYWORDS = {"explain this", "why did", "what does this query"}


def classify_intent(user_query: str) -> IntentClassification:
    """
    Classify input as chat, SQL, or ambiguous and choose the downstream graph route.

    Strategy:
    1. Try ML model first (sentence-transformers + LogisticRegression)
    2. If model unavailable or confidence < threshold, fall back to heuristic
    """
    query = _normalize(user_query)
    if not query:
        return IntentClassification("chat", "chat", reason="empty")

    # ── ML Classification (preferred) ────────────────────
    ml_result = _try_ml_classification(user_query)
    if ml_result is not None:
        return ml_result

    # ── Heuristic Fallback ───────────────────────────────

    # Meta queries are always SQL
    if any(kw in query for kw in META_KEYWORDS):
        return IntentClassification(
            "sql",
            "meta_query",
            complexity="simple",
            reason="meta_keyword",
        )

    has_data_signal = _has_data_signal(query)

    # Mixed inputs like "hi, show top 5 employees" should still be SQL.
    if has_data_signal:
        # Check if only weak signals present (e.g. "do you have info about admin")
        words = set(re.findall(r"[a-z_]+", query))
        has_strong = bool(words.intersection(DATA_SIGNAL_WORDS_STRONG))
        has_known_table = bool(words.intersection(KNOWN_TABLES))
        has_phrase = any(phrase in query for phrase in DATA_SIGNAL_PHRASES)

        if has_strong or has_phrase or has_known_table:
            return IntentClassification(
                "sql",
                _classify_sql_route(query),
                complexity=_estimate_complexity(query),
                reason="data_signal",
            )
        else:
            # Weak signal only — ambiguous query
            return IntentClassification(
                "ambiguous",
                "ambiguous",
                complexity="simple",
                reason="weak_data_signal_only",
            )

    if _is_chat(query):
        return IntentClassification("chat", "chat", reason="chat_signal")

    if len(query.split()) <= 4:
        return IntentClassification("chat", "chat", reason="short_without_data_signal")

    # Longer queries without any data signal — likely ambiguous
    words = set(re.findall(r"[a-z_]+", query))
    has_known_table = bool(words.intersection(KNOWN_TABLES))
    if not has_known_table:
        return IntentClassification(
            "ambiguous",
            "ambiguous",
            complexity="simple",
            reason="no_table_reference",
        )

    # Preserve existing text-to-SQL behavior for ambiguous analytical requests.
    return IntentClassification(
        "sql",
        _classify_sql_route(query),
        complexity=_estimate_complexity(query),
        reason="ambiguous_default_sql",
    )


def build_chat_response(user_query: str) -> str:
    """Return a simple conversational response without invoking SQL generation."""
    query = _normalize(user_query)

    if any(kw in query for kw in ("what can you do", "what do you do", "how do you work", "how does this work", "help", "what can i ask", "how do i use")):
        return (
            "I'm DataPilot, your Agentic AI Data Analyst. Ask me a database question in plain "
            "English and I can generate safe read-only SQL, run it, and summarize the result. "
            "Try: 'Show top 5 employees by salary' or 'Total sales by region'."
        )

    if any(kw in query for kw in ("who are you", "what are you", "tell me about you", "what is plainsql", "what is datapilot", "what is this")):
        return (
            "I'm DataPilot, an agentic AI Data Analyst for querying your database with natural language. "
            "Describe the data you want and I'll handle the SQL generation, execution, and analytics."
        )

    if any(kw in query for kw in ("thanks", "thank", "thx", "ty")):
        return "You're welcome. Ask me any database question when you're ready."

    if any(kw in query for kw in ("bye", "goodbye", "see you", "see ya", "later")):
        return "Goodbye. Come back anytime you want to explore your data."

    return (
        "Hello. I can help you query your database in plain English. "
        "Try asking something like 'Show top 5 employees by salary'."
    )


def _normalize(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value.lower()).strip()
    return cleaned.strip(" \t\r\n.!?")


def _has_data_signal(query: str) -> bool:
    if any(phrase in query for phrase in DATA_SIGNAL_PHRASES):
        return True
    words = set(re.findall(r"[a-z_]+", query))
    return bool(words.intersection(DATA_SIGNAL_WORDS))


def _is_chat(query: str) -> bool:
    if query in CHAT_EXACT or query in GREETING_PATTERNS:
        return True
    if any(query.startswith(prefix) for prefix in CHAT_PREFIXES):
        return True
    return any(query == greeting or query.startswith(greeting + " ") for greeting in GREETING_PATTERNS)


def _classify_sql_route(query: str) -> RouteIntent:
    if any(kw in query for kw in META_KEYWORDS):
        return "meta_query"
    if any(kw in query for kw in COMPARISON_KEYWORDS):
        return "comparison"
    if any(kw in query for kw in EXPLANATION_KEYWORDS):
        return "explanation"
    if any(kw in query for kw in AGGREGATION_KEYWORDS):
        return "aggregation"
    return "data_query"


def _estimate_complexity(query: str) -> Literal["simple", "moderate", "complex"]:
    if any(kw in query for kw in ("subquery", "window", "rank", "running total", "percentile")):
        return "complex"
    if any(kw in query for kw in ("join", "compare", " vs ", " versus ", "group by", " by ")):
        return "moderate"
    return "simple"


# ── ML Classification Bridge ────────────────────────────────

def _try_ml_classification(user_query: str) -> IntentClassification | None:
    """
    Attempt to classify using the ML model.

    Returns an IntentClassification if the model is available and confident,
    or None to signal the caller should use the heuristic fallback.
    """
    import os
    if os.environ.get("DISABLE_ML_INTENT", "false").lower() in ("true", "1", "yes"):
        logger.debug("ml_classification_disabled_by_env")
        return None

    try:
        from app.agents.ml_classifier import get_ml_classifier

        classifier = get_ml_classifier()
        if not classifier.available:
            return None

        result = classifier.classify(user_query)
        if result is None:
            return None

        # Only trust the ML model when it's confident enough
        if result.confidence < _ML_CONFIDENCE_THRESHOLD:
            logger.debug(
                "ml_confidence_below_threshold",
                confidence=result.confidence,
                threshold=_ML_CONFIDENCE_THRESHOLD,
                ml_label=result.intent,
                fallback="heuristic",
            )
            return None

        # Map ML result to the full IntentClassification with complexity
        query_normalized = _normalize(user_query)
        complexity = _estimate_complexity(query_normalized)

        # For SQL intents, refine the route using the heuristic sub-classifier
        if result.intent == "sql":
            route = _classify_sql_route(query_normalized)
        else:
            route = result.route_intent

        logger.info(
            "intent_classified",
            method="ml",
            intent=result.intent,
            route_intent=route,
            confidence=round(result.confidence, 3),
        )

        return IntentClassification(
            intent=result.intent,
            route_intent=route,
            complexity=complexity,
            reason=f"ml_model(conf={result.confidence:.2f})",
        )

    except Exception as e:
        logger.debug("ml_classification_unavailable", error=str(e))
        return None

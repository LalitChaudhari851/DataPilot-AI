"""
Conversational Fast-Path — Server-side detection for chat queries.

Handles greetings, thanks, identity questions, etc. without invoking the
full LLM pipeline. Provides instant (<1ms) responses for conversational queries.
"""

import re

# ── Pattern Registry ─────────────────────────────────────
_CHAT_PATTERNS = [
    re.compile(r'^(hi|hello|hey|howdy|sup|yo)\b[\s!.?]*$', re.I),
    re.compile(r'^how are you\??[\s!.]*$', re.I),
    re.compile(r'^thank(s| you)[\s!.,]*$', re.I),
    re.compile(r'^(bye|goodbye|see you|cya|later)[\s!.]*$', re.I),
    re.compile(r'^good (morning|afternoon|evening|night)[\s!.]*$', re.I),
    re.compile(r'^(ok|okay|alright|great|perfect|cool|nice|awesome)[\s!.]*$', re.I),
    re.compile(r'^(who are you|what are you|what is (plainsql|datapilot))\??$', re.I),
    re.compile(r'^(what can you do|help me|help)\??[\s!.]*$', re.I),
]

_CHAT_RESPONSES = {
    'greeting': "Hello! I'm DataPilot — your Agentic AI Data Analyst. Ask me anything about your database in plain English and I'll handle the SQL, analytics, and insights.",
    'thanks': "You're welcome! Let me know if you'd like to explore more insights from your data.",
    'farewell': "Goodbye! Your data will be here when you need it.",
    'identity': "I'm DataPilot — an agentic AI Data Analyst that converts natural language into grounded answers, safe SQL queries, and interactive visualizations using a hybrid RAG + multi-agent pipeline.",
    'capabilities': "I can help you query your database in plain English, generate safe SQL, visualize results with charts, and provide AI insights. Try: 'Show me top 5 employees by salary'",
    'default': "I'm DataPilot, your Agentic AI Data Analyst. Ask me questions about your database!",
}


def detect_conversational(query: str):
    """
    Returns a fast-path response string if the query is conversational, else None.

    This avoids a full LLM round-trip for simple greetings/thanks/identity questions.
    Average latency: <0.1ms.
    """
    q = query.strip()
    if re.match(r'^(hi|hello|hey|howdy|sup|yo)\b', q, re.I):
        return _CHAT_RESPONSES['greeting']
    if re.match(r'^how are you', q, re.I):
        return _CHAT_RESPONSES['greeting']
    if re.match(r'^thank(s| you)', q, re.I):
        return _CHAT_RESPONSES['thanks']
    if re.match(r'^(bye|goodbye|see you)', q, re.I):
        return _CHAT_RESPONSES['farewell']
    if re.match(r'^(who are you|what are you|what is (plainsql|datapilot))', q, re.I):
        return _CHAT_RESPONSES['identity']
    if re.match(r'^(what can you do|help)', q, re.I):
        return _CHAT_RESPONSES['capabilities']
    for pat in _CHAT_PATTERNS:
        if pat.match(q):
            return _CHAT_RESPONSES['default']
    return None

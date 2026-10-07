const BASE = import.meta.env.VITE_API_URL || '';

export const API = {
  stream: `${BASE}/api/v1/chat/stream`,
  feedback: `${BASE}/api/v1/feedback`,
  conversations: `${BASE}/api/v1/conversations`,
  health: `${BASE}/api/v1/health`,
  schema: `${BASE}/api/v1/schema`,
  databases: `${BASE}/api/v1/databases`,
};

const CHAT_RULES = [
  { re: /^(hi|hello|hey|howdy|sup|yo)\b[\s!.?]*$/i, type: 'greeting' },
  { re: /^how are you\??[\s!.]*$/i, type: 'greeting' },
  { re: /^thank(s| you)[\s!.,]*$/i, type: 'thanks' },
  { re: /^(bye|goodbye|see you|cya|later)[\s!.]*$/i, type: 'farewell' },
  { re: /^good (morning|afternoon|evening|night)[\s!.]*$/i, type: 'greeting' },
  { re: /^(ok|okay|alright|sounds good|great|perfect|cool|nice|awesome)[\s!.]*$/i, type: 'ack' },
  { re: /^(who are you|what are you|what is (plainsql|datapilot))\??$/i, type: 'identity' },
  { re: /^(what can you do|help me|help)\??[\s!.]*$/i, type: 'capabilities' },
];

const CHAT_RESPONSES = {
  greeting: [
    "Hello. I'm **DataPilot**, your Agentic AI Data Analyst. Ask a business question and I will retrieve schema context, generate safe SQL, execute it, and explain the result.",
    'Ready when you are. Try *"Show net revenue retention by segment"* and I will show the SQL, trace, result table, and chart.',
    'DataPilot here. I turn natural language into validated SQL with visible RAG and execution reasoning.',
  ],
  thanks: [
    "You're welcome. Send me the next business question when you want to go deeper.",
    'Happy to help. I can keep drilling into revenue, product usage, support, or pipeline data.',
    'Anytime. Your next question can be broad, messy, or very specific.',
  ],
  farewell: [
    'Goodbye. Your analysis workspace will be here when you come back.',
    'See you. Come back anytime for more database insights.',
  ],
  ack: [
    'Great. What would you like to query next?',
    'Perfect. I can keep going with a follow-up analysis or a new table context.',
  ],
  identity: [
    "I'm **DataPilot**, an agentic AI Data Analyst that converts natural language into grounded answers, safe SQL queries, and interactive visualizations.\n\nI use a **hybrid RAG + LLM pipeline** to understand database schema and generate accurate, safe SQL queries.\n\n**Stack:** Vector Search, Multi-Agent Orchestration, LLM Routing, Safety Validation.",
  ],
  capabilities: [
    'I can help you:\n- **Query** your database in plain English\n- **Generate** safe, optimized SQL automatically\n- **Visualize** results with charts and tables\n- **Explain** the SQL I generate\n- **Analyze** trends and generate insights\n\nTry: *"Show net revenue retention by customer segment"* or *"Which accounts have churn risk?"*',
  ],
};

export function detectConversational(query) {
  const q = query.trim();
  for (const { re, type } of CHAT_RULES) {
    if (re.test(q)) return type;
  }
  return null;
}

export function getConversationalResponse(type) {
  const pool = CHAT_RESPONSES[type] ?? CHAT_RESPONSES.greeting;
  return pool[Math.floor(Math.random() * pool.length)];
}

export async function fetchHealth() {
  const res = await fetch(API.health);
  if (!res.ok) throw new Error('Health check failed');
  return res.json();
}

export async function fetchDatabases() {
  try {
    const res = await fetch(API.databases);
    if (!res.ok) throw new Error('Databases fetch failed');
    return await res.json();
  } catch {
    return {
      databases: [
        {
          db_id: 'default',
          name: 'DataPilot SaaS',
          dialect: 'mysql',
          description: 'Production SaaS dataset (accounts, subscriptions, invoices, 22 tables)',
          table_count: 22,
          target: 'TiDB Cloud (chatbot)'
        },
        {
          db_id: 'E_commerce',
          name: 'E-Commerce (Spider)',
          dialect: 'mysql',
          description: 'Spider 2.0-Lite benchmark (orders, products, customers, 11 tables)',
          table_count: 11,
          target: 'TiDB Cloud (ecommerce)'
        }
      ],
      default: 'default'
    };
  }
}

export async function fetchSchema(db_id = 'default') {
  const url = db_id ? `${API.schema}?db_id=${encodeURIComponent(db_id)}` : API.schema;
  const res = await fetch(url);
  if (!res.ok) throw new Error('Schema fetch failed');
  return res.json();
}

export async function fetchConversations() {
  const res = await fetch(API.conversations);
  if (!res.ok) return { conversations: [] };
  return res.json();
}

export async function createConversation(title) {
  await fetch(API.conversations, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  }).catch(() => {});
}

export async function submitFeedback({ message_id, user_query, generated_sql, rating, comment }) {
  await fetch(API.feedback, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message_id, user_query, generated_sql: generated_sql ?? '', rating, comment: comment ?? '' }),
  });
}

export async function executeManualQuery({ sql, db_id = 'default' }) {
  const res = await fetch(`${BASE}/api/v1/execute-query`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sql, db_id: db_id || 'default' }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || body.error || `Execution failed (${res.status})`);
  }
  return res.json();
}

export async function streamChat({ question, history = [], db_id = 'default', onChunk }) {
  const res = await fetch(API.stream, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, history: history.slice(-6), db_id: db_id || 'default' }),
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    if (res.status === 429) throw new Error('Rate limit exceeded. Please wait a moment.');
    if (res.status === 401) throw new Error('Session expired. Please refresh.');
    if (res.status === 400) throw new Error(body.error ?? 'Query blocked. Try rephrasing.');
    throw new Error(body.error ?? body.detail ?? `Server error (${res.status})`);
  }

  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';

  const processBlocks = (text) => {
    const blocks = text.split('\n\n');
    blocks.forEach(block => {
      if (!block.trim()) return;
      const lines = block.split('\n');
      let eventType = null;
      let dataLine = null;
      for (const line of lines) {
        if (line.startsWith('event: ')) eventType = line.slice(7).trim();
        else if (line.startsWith('data: ')) dataLine = line.slice(6).trim();
      }
      if (!dataLine) return;
      try {
        const chunk = JSON.parse(dataLine);
        const type = chunk.type ?? eventType;
        if (!type) return;
        const mappedType = type === 'insights' ? 'message' : type;
        onChunk(mappedType, { ...chunk, type: mappedType });
      } catch {
        /* skip malformed SSE blocks */
      }
    });
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const lastDouble = buf.lastIndexOf('\n\n');
    if (lastDouble !== -1) {
      processBlocks(buf.slice(0, lastDouble + 2));
      buf = buf.slice(lastDouble + 2);
    }
  }
  if (buf.trim()) processBlocks(buf);
}

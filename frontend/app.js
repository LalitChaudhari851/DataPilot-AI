const API = {
  chat: "/chat",
  stream: "/chat/stream",
  feedback: "/api/v1/feedback",
  conversations: "/api/v1/conversations",
  health: "/api/v1/health",
  schema: "/api/v1/schema"
};

const dom = {
  sidebar: document.getElementById("sidebar"),
  chatRoot: document.getElementById("chatRoot"),
  chatScroll: document.getElementById("chatScroll"),
  form: document.getElementById("composerForm"),
  input: document.getElementById("promptInput"),
  send: document.getElementById("sendButton"),
  suggestions: document.getElementById("suggestions"),
  title: document.getElementById("conversationTitle"),
  subtitle: document.getElementById("conversationSubtitle"),
  healthChip: document.getElementById("healthChip"),
  toasts: document.getElementById("toasts")
};

const state = {
  chats: [],
  activeChatId: null,
  selectedSchema: "default",
  schemaTables: ["default"],
  savedQueries: [
    "Show top 5 employees by salary",
    "Total sales revenue by region",
    "Which department has the highest spend?",
    "Show products with low stock"
  ],
  system: {
    status: "Checking",
    latency: null,
    schemaSource: "Loading"
  },
  isSending: false,
  charts: new Map()
};

// ── Persistence ─────────────────────────────────────────
function saveState() {
  try {
    const serializable = {
      chats: state.chats.map(c => ({
        ...c,
        messages: c.messages.map(m => ({ ...m, streaming: false, pending: false }))
      })),
      activeChatId: state.activeChatId,
      savedQueries: state.savedQueries,
    };
    localStorage.setItem("plainsql_state", JSON.stringify(serializable));
  } catch { }
}

function loadState() {
  // Load from localStorage as immediate fallback
  try {
    const saved = JSON.parse(localStorage.getItem("plainsql_state") || "null");
    if (saved) {
      state.chats = saved.chats || [];
      state.activeChatId = saved.activeChatId;
      if (Array.isArray(saved.savedQueries) && saved.savedQueries.length) {
        state.savedQueries = saved.savedQueries;
      }
    }
  } catch { }
  // Then hydrate from server (non-blocking)
  loadConversationsFromServer();
}

async function loadConversationsFromServer() {
  try {
    const res = await fetch(API.conversations);
    if (!res.ok) return;
    const data = await res.json();
    if (data.conversations && data.conversations.length) {
      // Merge: keep local messages for active chats, add server-only chats
      const localIds = new Set(state.chats.map(c => c.id));
      for (const conv of data.conversations) {
        if (!localIds.has(conv.id)) {
          state.chats.push({
            id: conv.id,
            title: conv.title,
            messages: [],
            context: [],
            _serverSynced: true,
            _messageCount: conv.message_count,
          });
        }
      }
      if (!state.activeChatId && state.chats.length) {
        state.activeChatId = state.chats[0].id;
      }
      render();
    }
  } catch { }
}

const Component = {
  Sidebar() {
    const activeChat = getActiveChat();
    const chats = state.chats.length ? state.chats : [{ id: "empty", title: "No conversations yet", messages: [] }];
    return `
      <div class="sidebar-head">
        <div class="brand">
          <div class="brand-mark">DP</div>
          <div class="brand-copy"><strong>DataPilot</strong><span>Agentic AI Data Analyst</span></div>
        </div>
        <button class="new-chat" type="button" data-action="new-chat">+ New chat</button>
      </div>
      <div class="sidebar-scroll">
        <div class="section">
          <div class="section-title"><span>Schema</span><span>${state.schemaTables.length} tables</span></div>
          <div class="schema-panel">
            <label class="field-label" for="schemaSelect">Table context</label>
            <select class="select" id="schemaSelect" data-action="select-schema">
              ${state.schemaTables.map(name => `<option value="${escapeAttr(name)}" ${name === state.selectedSchema ? "selected" : ""}>${escapeHtml(name)}</option>`).join("")}
            </select>
            <div class="schema-meta">
              <span>${state.selectedSchema === "default" ? "All tables" : "Filtered"}</span>
              <span>Read-only</span>
            </div>
          </div>
        </div>
        <div class="section">
          <div class="section-title"><span>History</span><span>${state.chats.length}</span></div>
          <div class="history-list">
            ${chats.map(chat => `
              <button class="side-item ${activeChat && chat.id === activeChat.id ? "active" : ""}" type="button" data-chat-id="${escapeAttr(chat.id)}" ${chat.id === "empty" ? "disabled" : ""}>
                <span>${escapeHtml(chat.title)}</span>${chat.messages.length ? `<small>${chat.messages.length}</small>` : ""}
                ${chat.id !== "empty" ? `<span class="delete-chat" data-delete-chat="${escapeAttr(chat.id)}" title="Delete chat">✕</span>` : ""}
              </button>
            `).join("")}
          </div>
        </div>
        <div class="section">
          <div class="section-title"><span>Saved</span><span>${state.savedQueries.length}</span></div>
          <div class="saved-list">
            ${state.savedQueries.map(query => `
              <button class="side-item" type="button" data-saved-query="${escapeAttr(query)}"><span>${escapeHtml(query)}</span></button>
            `).join("")}
          </div>
        </div>
      </div>
      <div class="sidebar-foot"><div class="system-pill ${state.system.status.toLowerCase().includes("offline") ? "warning" : ""}"><span>${escapeHtml(state.system.status)}</span><span class="status-dot"></span></div></div>
    `;
  },

  ChatWindow() {
    const chat = getActiveChat();
    if (!chat || chat.messages.length === 0) return Component.Welcome();
    return chat.messages.map(message => Component.MessageBubble(message)).join("");
  },

  Welcome() {
    const prompts = [
      ["Revenue analysis", "Total sales revenue by region"],
      ["Top customers", "Which customers generated the most revenue?"],
      ["Inventory check", "Show products with low stock"],
      ["Team overview", "Show top 5 employees by salary"]
    ];
    return `
      <div class="welcome">
        <div class="welcome-badge">DataPilot</div>
        <h2>What do you want to <span>query?</span></h2>
        <p>Ask in plain English. I'll generate safe SQL, execute it, and show you the results.</p>
        <div class="prompt-grid">
          ${prompts.map(([title, query]) => `
            <button class="prompt-card" type="button" data-saved-query="${escapeAttr(query)}">
              <strong>${escapeHtml(title)}</strong><span>${escapeHtml(query)}</span>
            </button>
          `).join("")}
        </div>
      </div>
    `;
  },

  MessageBubble(message) {
    const role = message.role === "user" ? "user" : "ai";
    const body = message.pending
      ? Component.LoadingState()
      : role === "user"
        ? `<div>${escapeHtml(message.content)}</div>`
        : Component.AssistantContent(message);
    return `
      <article class="message ${role}" data-message-id="${escapeAttr(message.id)}">
        ${role === "user" ? `<div class="bubble">${body}</div><div class="avatar">U</div>` : `<div class="avatar ai">AI</div><div class="bubble">${body}</div>`}
      </article>
    `;
  },

  LoadingState() {
    const stages = [
      "Understanding your question",
      "Retrieving schema context",
      "Generating SQL",
      "Validating query safety",
      "Executing query",
      "Preparing results"
    ];
    const stageIndex = Math.min(Math.floor((Date.now() / 1800) % stages.length), stages.length - 1);
    return `<div class="loading-row" aria-label="Loading response"><div class="loading-stage"><div class="spinner"></div><span>${stages[stageIndex]}...</span></div><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div>`;
  },

  AssistantContent(message) {
    if (message.error) return `<div class="error-box">${escapeHtml(message.error)}</div>`;
    const data = message.data || {};
    const rows = normalizeRows(data);
    const stream = message.streamText || data.message || "I generated the SQL and result set below.";
    return `
      <div class="bubble-stream ${message.streaming ? "typing-caret" : ""}">${escapeHtml(stream)}</div>
      ${Component.MetaRow(data, rows)}
      ${rows.length ? Component.ResultSummary(rows) : ""}
      ${data.sql ? Component.SQLBlock(data.sql, message.id) : ""}
      ${rows.length ? Component.ResultTable(rows, message.id) : data.sql ? Component.EmptyState() : ""}
      ${rows.length && hasChartSupport(rows) ? Component.ChartView(message.id) : ""}
      ${data.explanation || data.sql_explanation ? Component.ExplanationBlock(data.explanation || data.sql_explanation) : ""}
      ${Array.isArray(data.insights) && data.insights.length ? Component.InsightBlock(data.insights) : ""}
      ${!message.streaming ? Component.MessageActions(message) : ""}
    `;
  },

  MessageActions(message) {
    const data = message.data || {};
    const userQuery = message._userQuery || "";
    return `
      <div class="message-actions">
        <button class="small-button" type="button" data-regenerate="${escapeAttr(message.id)}" title="Regenerate response">↻ Regenerate</button>
        ${data.sql ? `<button class="small-button" type="button" data-copy-sql="${escapeAttr(message.id)}">Copy SQL</button>` : ""}
        <button class="small-button" type="button" data-copy-response="${escapeAttr(message.id)}">Copy response</button>
        <span class="feedback-group" data-feedback-for="${escapeAttr(message.id)}">
          <button class="small-button feedback-btn ${message._feedback === 'up' ? 'active' : ''}" type="button" data-feedback="up" data-feedback-msg="${escapeAttr(message.id)}" title="Good response">👍</button>
          <button class="small-button feedback-btn ${message._feedback === 'down' ? 'active' : ''}" type="button" data-feedback="down" data-feedback-msg="${escapeAttr(message.id)}" title="Bad response">👎</button>
        </span>
      </div>
    `;
  },

  MetaRow(data, rows) {
    const items = [];
    if (data.intent) items.push(["Intent", data.intent]);
    if (typeof data.execution_time_ms !== "undefined") items.push(["Latency", `${Math.round(Number(data.execution_time_ms) || 0)}ms`]);
    if (typeof data.row_count !== "undefined" || rows.length) items.push(["Rows", data.row_count ?? rows.length]);
    if (data.trace_id) items.push(["Trace", data.trace_id]);
    return items.length ? `<div class="meta-row">${items.map(([label, value]) => `<span class="meta">${escapeHtml(label)} <b>${escapeHtml(String(value))}</b></span>`).join("")}</div>` : "";
  },

  SQLBlock(sql, messageId) {
    return `
      <section class="sql-block">
        <div class="block-head">
          <span class="block-title">Generated SQL</span>
          <div class="block-actions">
            <button class="small-button" type="button" data-copy-sql="${escapeAttr(messageId)}">Copy SQL</button>
            <button class="small-button" type="button" data-save-sql="${escapeAttr(messageId)}">Save query</button>
          </div>
        </div>
        <pre><code>${highlightSQL(sql)}</code></pre>
      </section>
    `;
  },

  ResultSummary(rows) {
    const summary = summarizeRows(rows);
    return `
      <section class="summary-strip" aria-label="Result summary">
        ${summary.map(item => `<div><span>${escapeHtml(item.label)}</span><strong>${escapeHtml(item.value)}</strong></div>`).join("")}
      </section>
    `;
  },

  ResultTable(rows, messageId) {
    const cols = Object.keys(rows[0] || {});
    return `
      <section class="result-block">
        <div class="block-head">
          <span class="block-title">Result table</span>
          <div class="block-actions">
            <button class="small-button" type="button" data-export-csv="${escapeAttr(messageId)}">Export CSV</button>
            <button class="small-button" type="button" data-copy-result="${escapeAttr(messageId)}">Copy JSON</button>
          </div>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr>${cols.map(col => `<th>${escapeHtml(col)}</th>`).join("")}</tr></thead>
            <tbody>${rows.slice(0, 100).map(row => `<tr>${cols.map(col => `<td>${escapeHtml(formatCell(row[col]))}</td>`).join("")}</tr>`).join("")}</tbody>
          </table>
        </div>
      </section>
    `;
  },

  ChartView(messageId) {
    return `
      <section class="chart-block">
        <div class="block-head">
          <span class="block-title">Chart view</span>
          <div class="block-actions">
            <button class="small-button chart-toggle" type="button" data-chart-type="${escapeAttr(messageId)}" data-type="bar">Bar</button>
            <button class="small-button chart-toggle" type="button" data-chart-type="${escapeAttr(messageId)}" data-type="line">Line</button>
          </div>
        </div>
        <div class="chart-body"><canvas id="chart-${escapeAttr(messageId)}"></canvas></div>
      </section>
    `;
  },

  ExplanationBlock(explanation) {
    return `<section class="explanation-block"><h3 class="insight-title">SQL reasoning</h3><p>${escapeHtml(stripMarkdown(String(explanation)))}</p></section>`;
  },

  InsightBlock(insights) {
    return `<section class="insight-block"><h3 class="insight-title">AI insights</h3><ul class="insight-list">${insights.map(item => `<li>${escapeHtml(stripMarkdown(String(item)))}</li>`).join("")}</ul></section>`;
  },

  EmptyState() {
    return `<div class="empty-result">No rows matched this query. Try broadening your filters.</div>`;
  },

  Suggestions(items) {
    return items.map(item => `<button class="suggestion" type="button" data-saved-query="${escapeAttr(item)}">${escapeHtml(item)}</button>`).join("");
  }
};

function render() {
  dom.sidebar.innerHTML = Component.Sidebar();
  dom.chatRoot.innerHTML = Component.ChatWindow();
  const chat = getActiveChat();
  dom.title.textContent = chat?.title || "Data analysis chat";
  dom.subtitle.textContent = chat?.messages.length ? `${chat.messages.length} messages in this workspace` : "Ask in natural language. Review the SQL. Trust the result after inspection.";
  hydrateCharts();
  scrollToBottom();
}

function getActiveChat() {
  return state.chats.find(chat => chat.id === state.activeChatId) || null;
}

function ensureChat(seedTitle = "Untitled analysis") {
  let chat = getActiveChat();
  if (chat) return chat;
  chat = { id: createId("chat"), title: seedTitle, messages: [], context: [] };
  state.chats.unshift(chat);
  state.activeChatId = chat.id;
  // Create on server (fire-and-forget)
  createConversationOnServer(chat.id, seedTitle);
  return chat;
}

function newChat() {
  const chatId = createId("chat");
  const chat = { id: chatId, title: "New analysis", messages: [], context: [] };
  state.chats.unshift(chat);
  state.activeChatId = chat.id;
  state.charts.forEach(chart => chart.destroy());
  state.charts.clear();
  dom.suggestions.innerHTML = "";
  // Create on server (fire-and-forget)
  createConversationOnServer(chatId, "New analysis");
  render();
  dom.input.focus();
}

async function createConversationOnServer(id, title) {
  try {
    await fetch(API.conversations, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
  } catch { }
}

async function submitPrompt(raw) {
  const question = String(raw ?? "").trim();
  if (!question || state.isSending) return;
  const chat = ensureChat(titleFromPrompt(question));
  if (!chat.messages.length || chat.title === "New analysis" || chat.title === "Untitled analysis") chat.title = titleFromPrompt(question);
  const pending = { id: createId("msg"), role: "assistant", pending: true, data: null, _userQuery: question };
  chat.messages.push({ id: createId("msg"), role: "user", content: question }, pending);
  dom.input.value = "";
  autoSizeInput();
  dom.suggestions.innerHTML = "";
  setSending(true);
  render();

  try {
    const payload = {
      question: state.selectedSchema === "default" ? question : `${question}\n\nUse table context: ${state.selectedSchema}`,
      history: chat.context.slice(-6)
    };

    // ── True SSE streaming via /chat/stream ──────────────
    const response = await fetch(API.stream, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      if (response.status === 401) throw new Error("Your session has expired. Please refresh the page.");
      if (response.status === 429) throw new Error("You're sending requests too quickly. Please wait a moment.");
      if (response.status === 400) throw new Error(errorData.error || "This query was blocked. Try rephrasing.");
      throw new Error(errorData.error || `Server error (${response.status}). Please try again.`);
    }

    // ── Parse SSE chunks progressively ───────────────────
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    const assembled = { intent: "", sql: "", explanation: "", answer: [], message: "", insights: [], follow_ups: [], row_count: 0, execution_time_ms: 0, chart_config: null };

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        try {
          const chunk = JSON.parse(line.slice(6));
          switch (chunk.type) {
            case "stage":
              // Update loading stage text in real time
              const loadingStage = document.querySelector(".loading-stage span");
              if (loadingStage) loadingStage.textContent = chunk.message;
              break;
            case "intent":
              assembled.intent = chunk.intent;
              break;
            case "sql":
              assembled.sql = chunk.sql;
              assembled.explanation = chunk.explanation || "";
              // Show SQL as soon as it arrives
              pending.pending = false;
              pending.data = { ...assembled };
              pending.streamText = "Generating results...";
              pending.streaming = true;
              render();
              break;
            case "results":
              assembled.answer = chunk.data || [];
              assembled.row_count = chunk.row_count || 0;
              assembled.execution_time_ms = chunk.execution_time_ms || 0;
              pending.data = { ...assembled };
              render();
              break;
            case "message":
              assembled.message = chunk.message || "";
              assembled.insights = chunk.insights || [];
              assembled.follow_ups = chunk.follow_ups || [];
              pending.data = { ...assembled };
              // Stream the message text token-by-token
              pending.streamText = "";
              pending.streaming = true;
              render();
              await streamResponse(pending, assembled.message || "Done.");
              pending.streaming = false;
              render();
              break;
            case "error":
              // Pipeline crashed — show the error to the user
              pending.error = chunk.error || "An unexpected error occurred.";
              pending.pending = false;
              pending.streaming = false;
              render();
              break;
            case "done":
              assembled.execution_time_ms = assembled.execution_time_ms || chunk.total_time_ms || 0;
              break;
          }
        } catch { }
      }
    }

    // Finalize
    pending.pending = false;
    pending.streaming = false;
    pending.data = assembled;
    if (!pending.streamText) pending.streamText = assembled.message || "Done.";
    if (assembled.sql && !String(assembled.sql).toLowerCase().includes("error")) {
      chat.context.push({ user: question, sql: assembled.sql });
      chat.context = chat.context.slice(-8);
    }
    dom.suggestions.innerHTML = Component.Suggestions(Array.isArray(assembled.follow_ups) ? assembled.follow_ups.slice(0, 5) : []);
    toast("Query complete", "success");
  } catch (error) {
    pending.pending = false;
    const msg = error.message === "Failed to fetch"
      ? "Unable to connect to the server. Check if it's running and try again."
      : error.message;
    pending.error = msg;
    render();
    toast("Query failed", "error");
  } finally {
    setSending(false);
    render();
    saveState();
  }
}

async function streamResponse(message, text) {
  const tokens = String(text).split(/(\s+)/).filter(Boolean);
  for (const token of tokens) {
    message.streamText += token;
    updateMessage(message);
    await wait(token.trim() ? 24 : 6);
  }
}

function updateMessage(message) {
  const node = document.querySelector(`[data-message-id="${escapeSelector(message.id)}"] .bubble`);
  if (!node) return;
  node.innerHTML = Component.AssistantContent(message);
  hydrateCharts();
  scrollToBottom();
}

function hydrateCharts(typeOverrideById = {}) {
  const chat = getActiveChat();
  if (!chat) return;
  chat.messages.filter(message => message.role === "assistant" && message.data && hasChartSupport(normalizeRows(message.data))).forEach(message => {
    const canvas = document.getElementById(`chart-${message.id}`);
    if (!canvas || !window.Chart) return;
    const rows = normalizeRows(message.data);
    const type = typeOverrideById[message.id] || canvas.dataset.type || inferChartType(rows);
    canvas.dataset.type = type;
    document.querySelectorAll(`[data-chart-type="${escapeSelector(message.id)}"]`).forEach(button => {
      button.classList.toggle("active", button.dataset.type === type);
    });
    if (state.charts.has(message.id)) state.charts.get(message.id).destroy();
    state.charts.set(message.id, new Chart(canvas.getContext("2d"), buildChartConfig(rows, type)));
  });
}

function buildChartConfig(rows, type) {
  const cols = getColumns(rows);
  const numericCol = getNumericColumns(rows)[0] || cols[0];
  const labelCol = cols.find(col => col !== numericCol && rows.some(row => !isNumericValue(row[col]))) || cols.find(col => col !== numericCol) || cols[0];
  return {
    type,
    data: {
      labels: rows.slice(0, 24).map(row => String(row[labelCol] ?? "").slice(0, 28)),
      datasets: [{
        label: readableLabel(numericCol),
        data: rows.slice(0, 24).map(row => toNumber(row[numericCol])),
        borderColor: "#6366f1",
        backgroundColor: type === "line" ? "rgba(99,102,241,0.14)" : "rgba(99,102,241,0.55)",
        pointBackgroundColor: "#818cf8",
        pointRadius: type === "line" ? 3 : 0,
        borderWidth: 2,
        borderRadius: type === "bar" ? 6 : 0,
        tension: 0.36,
        fill: type === "line"
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: "#a1a1a1", font: { family: "Inter" } } },
        tooltip: { backgroundColor: "#171717", borderColor: "rgba(255,255,255,0.12)", borderWidth: 1 }
      },
      scales: {
        x: { ticks: { color: "#737373" }, grid: { color: "rgba(255,255,255,0.05)" } },
        y: { ticks: { color: "#737373" }, grid: { color: "rgba(255,255,255,0.06)" } }
      }
    }
  };
}

function inferChartType(rows) {
  const cols = getColumns(rows);
  return cols.some(col => /date|month|year|day|time/i.test(col)) ? "line" : "bar";
}

async function loadHealth() {
  try {
    const started = performance.now();
    const response = await fetch(API.health);
    const health = await response.json();
    state.system.latency = Math.max(1, Math.round(performance.now() - started));
    state.system.status = health.status === "healthy" ? "Healthy" : "Degraded";
    dom.healthChip.textContent = state.system.status;
  } catch {
    state.system.status = "Offline mode";
    state.system.latency = null;
    dom.healthChip.textContent = state.system.status;
  }
  render();
}

async function loadSchema() {
  try {
    const token = localStorage.getItem("plainsql_token");
    const headers = token ? { Authorization: `Bearer ${token}` } : {};
    const response = await fetch(API.schema, { headers });
    if (!response.ok) throw new Error("Schema requires auth");
    const schema = await response.json();
    state.schemaTables = ["default", ...(schema.tables || [])];
    state.system.schemaSource = "Connected";
  } catch {
    state.schemaTables = ["default", "employees", "sales", "products", "customers"];
    state.system.schemaSource = "Demo fallback";
  }
  render();
}

function handleClick(event) {
  const openSidebar = event.target.closest("[data-action='open-sidebar']");
  const closeSidebar = event.target.closest("[data-action='close-sidebar']");
  const newChatButton = event.target.closest("[data-action='new-chat']");
  const historyButton = event.target.closest("[data-chat-id]");
  const savedButton = event.target.closest("[data-saved-query]");
  const copySql = event.target.closest("[data-copy-sql]");
  const saveSql = event.target.closest("[data-save-sql]");
  const exportCsv = event.target.closest("[data-export-csv]");
  const copyResult = event.target.closest("[data-copy-result]");
  const chartType = event.target.closest("[data-chart-type]");
  const deleteChat = event.target.closest("[data-delete-chat]");
  const regenerate = event.target.closest("[data-regenerate]");
  const copyResponse = event.target.closest("[data-copy-response]");
  if (openSidebar) document.body.classList.add("sidebar-open");
  if (closeSidebar) document.body.classList.remove("sidebar-open");
  if (newChatButton) newChat();
  if (deleteChat) {
    event.stopPropagation();
    const chatId = deleteChat.dataset.deleteChat;
    state.chats = state.chats.filter(c => c.id !== chatId);
    if (state.activeChatId === chatId) state.activeChatId = state.chats[0]?.id || null;
    saveState();
    render();
    return;
  }
  if (historyButton && historyButton.dataset.chatId !== "empty") {
    state.activeChatId = historyButton.dataset.chatId;
    document.body.classList.remove("sidebar-open");
    render();
  }
  if (savedButton) {
    document.body.classList.remove("sidebar-open");
    submitPrompt(savedButton.dataset.savedQuery);
  }
  if (copySql) writeClipboard(findMessage(copySql.dataset.copySql)?.data?.sql || "", "SQL copied");
  if (saveSql) {
    const sql = findMessage(saveSql.dataset.saveSql)?.data?.sql || "";
    if (sql && !state.savedQueries.includes(sql)) state.savedQueries.unshift(sql);
    toast("Saved query added", "success");
    saveState();
    render();
  }
  if (exportCsv) downloadCSV(normalizeRows(findMessage(exportCsv.dataset.exportCsv)?.data || {}));
  if (copyResult) writeClipboard(JSON.stringify(normalizeRows(findMessage(copyResult.dataset.copyResult)?.data || {}), null, 2), "Result JSON copied");
  if (regenerate) {
    const msg = findMessage(regenerate.dataset.regenerate);
    if (msg) {
      // Find the user message before this assistant message
      const chat = getActiveChat();
      if (chat) {
        const idx = chat.messages.findIndex(m => m.id === msg.id);
        if (idx > 0) {
          const userMsg = chat.messages[idx - 1];
          if (userMsg?.role === "user") {
            // Remove old assistant message and re-submit
            chat.messages.splice(idx, 1);
            render();
            submitPrompt(userMsg.content);
          }
        }
      }
    }
  }
  if (copyResponse) {
    const msg = findMessage(copyResponse.dataset.copyResponse);
    if (msg) {
      const data = msg.data || {};
      const parts = [msg.streamText || data.message || "", data.sql ? `\nSQL:\n${data.sql}` : "", data.explanation || data.sql_explanation ? `\nExplanation: ${data.explanation || data.sql_explanation}` : ""].filter(Boolean).join("\n");
      writeClipboard(parts, "Response copied");
    }
  }
  if (chartType) {
    const id = chartType.dataset.chartType;
    const canvas = document.getElementById(`chart-${id}`);
    if (canvas) canvas.dataset.type = chartType.dataset.type;
    hydrateCharts({ [id]: chartType.dataset.type });
  }
  // Feedback buttons (thumbs up/down)
  const feedbackBtn = event.target.closest("[data-feedback-msg]");
  if (feedbackBtn) {
    const msgId = feedbackBtn.dataset.feedbackMsg;
    const rating = feedbackBtn.dataset.feedback;
    sendFeedback(msgId, rating);
  }
}

async function sendFeedback(messageId, rating) {
  const msg = findMessage(messageId);
  if (!msg) return;
  msg._feedback = rating;
  render();
  try {
    await fetch(API.feedback, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message_id: messageId,
        user_query: msg._userQuery || "",
        generated_sql: msg.data?.sql || "",
        rating,
      })
    });
    toast(`Feedback recorded: ${rating === "up" ? "👍" : "👎"}`, "success");
    saveState();
  } catch {
    toast("Failed to send feedback", "error");
  }
}

function handleChange(event) {
  if (event.target.matches("[data-action='select-schema']")) {
    state.selectedSchema = event.target.value;
    toast(`Schema context: ${state.selectedSchema}`, "success");
    render();
  }
}

function handleSubmit(event) {
  event.preventDefault();
  submitPrompt(dom.input.value);
}

function handleInputKeydown(event) {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    submitPrompt(dom.input.value);
  }
}

let _rafId;
function autoSizeInput() {
  cancelAnimationFrame(_rafId);
  _rafId = requestAnimationFrame(() => {
    dom.input.style.height = "auto";
    dom.input.style.height = `${Math.min(dom.input.scrollHeight, 150)}px`;
  });
}

function setSending(value) {
  state.isSending = value;
  dom.send.disabled = value;
  // Animate loading stage rotation while waiting
  if (value) {
    state._loadingInterval = setInterval(() => {
      const loadingStage = document.querySelector(".loading-stage span");
      if (loadingStage) {
        const stages = ["Understanding your question", "Retrieving schema context", "Generating SQL", "Validating query safety", "Executing query", "Preparing results"];
        const current = stages.findIndex(s => loadingStage.textContent.startsWith(s));
        const next = (current + 1) % stages.length;
        loadingStage.textContent = stages[next] + "...";
      }
    }, 1800);
  } else if (state._loadingInterval) {
    clearInterval(state._loadingInterval);
    state._loadingInterval = null;
  }
}

function getWorkspaceMetrics() {
  const messages = state.chats.flatMap(chat => chat.messages);
  const assistantMessages = messages.filter(message => message.role === "assistant" && message.data);
  const resultRows = assistantMessages.reduce((total, message) => total + normalizeRows(message.data).length, 0);
  const sqlBlocks = assistantMessages.filter(message => message.data?.sql).length;
  return {
    questions: messages.filter(message => message.role === "user").length,
    sqlBlocks,
    resultRows,
    pipelineIndex: state.isSending ? 1 : sqlBlocks ? 3 : 0
  };
}

function normalizeRows(data) {
  const rows = data?.answer || data?.data || [];
  return Array.isArray(rows) && rows.length && typeof rows[0] === "object" ? rows : [];
}

function summarizeRows(rows) {
  const cols = getColumns(rows);
  const numericCols = getNumericColumns(rows);
  const labelCol = cols.find(col => !numericCols.includes(col)) || cols[0] || "Column";
  const primaryMetric = numericCols[0];
  const total = primaryMetric
    ? rows.reduce((sum, row) => sum + toNumber(row[primaryMetric]), 0)
    : rows.length;
  const peak = primaryMetric
    ? rows.reduce((best, row) => toNumber(row[primaryMetric]) > toNumber(best?.[primaryMetric]) ? row : best, rows[0])
    : rows[0];
  return [
    { label: "Rows", value: formatNumber(rows.length) },
    { label: primaryMetric ? `Total ${readableLabel(primaryMetric)}` : "Records", value: primaryMetric ? formatNumber(total) : formatNumber(rows.length) },
    { label: "Primary label", value: readableLabel(labelCol) },
    { label: "Top result", value: formatCell(peak?.[labelCol] ?? "Ready") || "Ready" }
  ];
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function hasChartSupport(rows) {
  return Boolean(window.Chart && rows.length && getNumericColumns(rows).length);
}

function getColumns(rows) {
  return Array.from(rows.reduce((set, row) => {
    Object.keys(row || {}).forEach(key => set.add(key));
    return set;
  }, new Set()));
}

function getNumericColumns(rows) {
  return getColumns(rows).filter(col => rows.some(row => isNumericValue(row[col])));
}

function isNumericValue(value) {
  if (typeof value === "number") return Number.isFinite(value);
  if (typeof value !== "string") return false;
  const trimmed = value.trim();
  return trimmed !== "" && Number.isFinite(Number(trimmed));
}

function toNumber(value) {
  return isNumericValue(value) ? Number(value) : 0;
}

function findMessage(messageId) {
  for (const chat of state.chats) {
    const found = chat.messages.find(message => message.id === messageId);
    if (found) return found;
  }
  return null;
}

function downloadCSV(rows) {
  if (!rows.length) return toast("No rows to export", "error");
  const cols = Object.keys(rows[0]);
  const csv = [cols.join(","), ...rows.map(row => cols.map(col => `"${String(row[col] ?? "").replace(/"/g, '""')}"`).join(","))].join("\n");
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  link.download = "datapilot-result.csv";
  link.click();
  URL.revokeObjectURL(link.href);
  toast("CSV exported", "success");
}

async function writeClipboard(value, message) {
  if (!value) return toast("Nothing to copy", "error");
  try {
    await navigator.clipboard.writeText(value);
    toast(message, "success");
  } catch {
    toast("Clipboard permission denied", "error");
  }
}

function toast(message, type = "success") {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.textContent = message;
  dom.toasts.appendChild(node);
  setTimeout(() => node.remove(), 3200);
}

function scrollToBottom() {
  requestAnimationFrame(() => {
    dom.chatScroll.scrollTop = dom.chatScroll.scrollHeight;
  });
}

function escapeHtml(value) {
  return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, "&#096;");
}

function escapeSelector(value) {
  if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(String(value));
  return String(value).replace(/["\\]/g, "\\$&");
}

function stripMarkdown(value) {
  return value.replace(/\*\*([^*]+)\*\*/g, "$1").replace(/`([^`]+)`/g, "$1");
}

function formatCell(value) {
  if (value === null || typeof value === "undefined") return "";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toLocaleString(undefined, { maximumFractionDigits: 3 });
  return String(value);
}

function highlightSQL(sql) {
  const keywords = ["select", "from", "where", "join", "left", "right", "inner", "outer", "on", "and", "or", "not", "in", "as", "order", "by", "group", "having", "limit", "desc", "asc", "count", "sum", "avg", "min", "max", "distinct", "between", "like", "is", "null", "with", "case", "when", "then", "else", "end", "round", "coalesce"];
  const keywordPattern = keywords.join("|");
  const tokenPattern = new RegExp(`('(?:''|[^'])*')|(\\b\\d+(?:\\.\\d+)?\\b)|(\\b(?:${keywordPattern})\\b)`, "gi");
  const source = String(sql ?? "");
  let html = "";
  let cursor = 0;
  for (const match of source.matchAll(tokenPattern)) {
    html += escapeHtml(source.slice(cursor, match.index));
    const safe = escapeHtml(match[0]);
    if (/^'/.test(match[0])) html += `<span class="str">${safe}</span>`;
    else if (/^\d/.test(match[0])) html += `<span class="num">${safe}</span>`;
    else html += `<span class="kw">${safe}</span>`;
    cursor = match.index + match[0].length;
  }
  return html + escapeHtml(source.slice(cursor));
}

function titleFromPrompt(prompt) {
  const compact = prompt.replace(/\s+/g, " ").trim();
  return compact.length > 52 ? `${compact.slice(0, 49)}...` : compact;
}

function readableLabel(value) {
  return String(value || "value").replace(/_/g, " ").replace(/\b\w/g, char => char.toUpperCase());
}

function createId(prefix) {
  return `${prefix}_${Math.random().toString(36).slice(2, 9)}_${Date.now().toString(36)}`;
}

function wait(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

document.addEventListener("click", handleClick);
document.addEventListener("change", handleChange);
document.addEventListener("keydown", function (e) {
  if ((e.ctrlKey || e.metaKey) && e.key === "k") {
    e.preventDefault();
    newChat();
    dom.input.focus();
  }
});
dom.form.addEventListener("submit", handleSubmit);
dom.input.addEventListener("keydown", handleInputKeydown);
dom.input.addEventListener("input", autoSizeInput);

loadState();
render();
loadHealth();
loadSchema();
dom.input.focus();

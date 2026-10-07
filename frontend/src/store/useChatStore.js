import { create } from 'zustand';
import { createConversation } from '../api/client';

const uid = (prefix = 'id') => `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
const titleFromPrompt = (q) => q.slice(0, 42) + (q.length > 42 ? '...' : '');

function loadPersisted() {
  try {
    const raw = localStorage.getItem('plainsql_v2');
    if (!raw) return null;
    return JSON.parse(raw);
  } catch { return null; }
}

function persistState(chats, activeChatId, savedQueries) {
  try {
    const clean = chats.map(c => ({
      ...c,
      messages: c.messages.map(m => ({ ...m, streaming: false, pending: false }))
    }));
    const state = useChatStore?.getState() ?? {};
    localStorage.setItem('plainsql_v2', JSON.stringify({
      chats: clean,
      activeChatId,
      savedQueries,
      sidebarCollapsed: state.sidebarCollapsed ?? false,
      schemaFolderOpen: state.schemaFolderOpen ?? false,
      expandedGroups: state.expandedGroups ?? {},
      expandedTables: state.expandedTables ?? {},
      selectedDbId: state.selectedDbId ?? 'default',
    }));
  } catch { /* quota errors are non-fatal */ }
}

const persisted = loadPersisted();

const useChatStore = create((set, get) => ({
  // ── Boot ──────────────────────────────────────────────
  booted: false,
  setBooted: () => set({ booted: true }),

  // ── Sidebar collapse state ────────────────────────────
  sidebarCollapsed: persisted?.sidebarCollapsed ?? false,
  setSidebarCollapsed: (v) => set(s => {
    setTimeout(() => persistState(get().chats, get().activeChatId, get().savedQueries), 0);
    return { sidebarCollapsed: v };
  }),

  // ── Schema folder (top-level "Tables" section) ────────
  schemaFolderOpen: persisted?.schemaFolderOpen ?? persisted?.tablesExpanded ?? false,
  setSchemaFolderOpen: (v) => set(s => {
    const next = typeof v === 'function' ? v(s.schemaFolderOpen) : v;
    setTimeout(() => persistState(get().chats, get().activeChatId, get().savedQueries), 0);
    return { schemaFolderOpen: next };
  }),

  // ── Expanded groups (per-group collapse state) ────────
  expandedGroups: persisted?.expandedGroups ?? {},
  setExpandedGroup: (groupName, value) => set(s => {
    const next = { ...s.expandedGroups, [groupName]: value };
    setTimeout(() => persistState(get().chats, get().activeChatId, get().savedQueries), 0);
    return { expandedGroups: next };
  }),

  // ── Expanded tables (per-table column panel state) ────
  expandedTables: persisted?.expandedTables ?? {},
  toggleExpandedTable: (tableName) => set(s => {
    const next = { ...s.expandedTables, [tableName]: !s.expandedTables[tableName] };
    setTimeout(() => persistState(get().chats, get().activeChatId, get().savedQueries), 0);
    return { expandedTables: next };
  }),

  // ── System status ──────────────────────────────────────
  health: { status: 'checking', latency: null },
  setHealth: (health) => set({ health }),

  // ── Schema data ────────────────────────────────────────
  schemaTables: [
    'departments',
    'employees',
    'plans',
    'products',
    'feature_catalog',
    'accounts',
    'contacts',
    'workspaces',
    'workspace_users',
    'subscriptions',
    'invoices',
    'payments',
    'opportunities',
    'product_usage_daily',
    'support_tickets',
    'ticket_events',
    'incidents',
    'query_audit_log',
  ],
  setSchemaTables: (tables) => set({ schemaTables: tables }),
  schemaText: '',
  setSchemaText: (text) => set({ schemaText: text }),
  selectedSchema: 'default',
  setSelectedSchema: (s) => set({ selectedSchema: s }),

  // ── Database Context (Multi-Database Support) ────────────
  selectedDbId: persisted?.selectedDbId ?? 'default',
  availableDatabases: [
    {
      db_id: 'default',
      name: 'DataPilot SaaS',
      dialect: 'mysql',
      description: 'Production SaaS schema (accounts, subscriptions, invoices, 22 tables)',
      table_count: 22,
      target: 'TiDB Cloud (chatbot)',
    },
    {
      db_id: 'E_commerce',
      name: 'E-Commerce (Spider)',
      dialect: 'mysql',
      description: 'Spider 2.0-Lite benchmark (orders, products, customers, 11 tables)',
      table_count: 11,
      target: 'TiDB Cloud (ecommerce)',
    },
  ],
  setAvailableDatabases: (databases) => set({ availableDatabases: databases }),
  setSelectedDbId: (dbId) => {
    set(s => {
      setTimeout(() => persistState(get().chats, get().activeChatId, get().savedQueries), 0);
      return { selectedDbId: dbId, selectedSchema: 'default' };
    });
    // Trigger schema reload for the newly selected database
    import('../api/client').then(({ fetchSchema }) => {
      fetchSchema(dbId)
        .then(d => {
          if (d.tables) get().setSchemaTables(d.tables);
          if (d.schema_text) get().setSchemaText(d.schema_text);
        })
        .catch(() => {});
    });
    const dbName = dbId === 'E_commerce' ? 'E-Commerce (Spider)' : 'DataPilot SaaS';
    get().addToast(`Switched active context to ${dbName} (TiDB Cloud)`, 'info');
  },

  // ── Toasts ─────────────────────────────────────────────
  toasts: [],
  addToast: (message, type = 'info') => {
    const id = uid('toast');
    set(s => ({ toasts: [...s.toasts, { id, message, type }] }));
    setTimeout(() => set(s => ({ toasts: s.toasts.filter(t => t.id !== id) })), 3500);
  },
  removeToast: (id) => set(s => ({ toasts: s.toasts.filter(t => t.id !== id) })),

  // ── Chats ──────────────────────────────────────────────
  chats: persisted?.chats ?? [],
  activeChatId: persisted?.activeChatId ?? null,
  savedQueries: persisted?.savedQueries ?? [
    'Show net revenue retention by customer segment for the last 4 quarters',
    'Which accounts have high ARR, low product usage, and unresolved critical tickets?',
    'Rank sales reps by closed-won ARR and average deal cycle length this year',
    'Show weekly query volume, failed executions, and p95 latency by workspace',
  ],
  isSending: false,
  setIsSending: (v) => set({ isSending: v }),

  getActiveChat: () => {
    const { chats, activeChatId } = get();
    return chats.find(c => c.id === activeChatId) ?? null;
  },

  newChat: () => {
    const id = uid('chat');
    const chat = { id, title: 'New analysis', messages: [], context: [] };
    set(s => {
      const next = [chat, ...s.chats];
      persistState(next, id, s.savedQueries);
      return { chats: next, activeChatId: id };
    });
    createConversation('New analysis');
  },

  selectChat: (id) => {
    set(s => { persistState(s.chats, id, s.savedQueries); return { activeChatId: id }; });
  },

  deleteChat: (id) => {
    set(s => {
      const next = s.chats.filter(c => c.id !== id);
      const active = s.activeChatId === id ? (next[0]?.id ?? null) : s.activeChatId;
      persistState(next, active, s.savedQueries);
      return { chats: next, activeChatId: active };
    });
  },

  // Ensure there's an active chat to receive messages
  ensureChat: (title) => {
    const { chats, activeChatId } = get();
    const existing = chats.find(c => c.id === activeChatId);
    if (existing) return existing;
    const id = uid('chat');
    const chat = { id, title: titleFromPrompt(title), messages: [], context: [] };
    set(s => {
      const next = [chat, ...s.chats];
      persistState(next, id, s.savedQueries);
      return { chats: next, activeChatId: id };
    });
    createConversation(chat.title);
    return chat;
  },

  // Push user + pending assistant message
  addUserMessage: (question) => {
    const chat = get().ensureChat(question);
    const userMsgId = uid('msg');
    const pendingId = uid('msg');
    const userMsg = { id: userMsgId, role: 'user', content: question };
    const pending = { id: pendingId, role: 'assistant', pending: true, data: null, _userQuery: question, _chatMode: false };

    // Update title if chat is new
    set(s => {
      const next = s.chats.map(c => {
        if (c.id !== chat.id) return c;
        const newTitle = c.messages.length === 0 ? titleFromPrompt(question) : c.title;
        return { ...c, title: newTitle, messages: [...c.messages, userMsg, pending] };
      });
      persistState(next, s.activeChatId, s.savedQueries);
      return { chats: next };
    });
    return { chat, pendingId };
  },

  // Update an in-flight message
  updateMessage: (chatId, msgId, patch) => {
    set(s => {
      const next = s.chats.map(c => {
        if (c.id !== chatId) return c;
        return { ...c, messages: c.messages.map(m => m.id === msgId ? { ...m, ...patch } : m) };
      });
      persistState(next, s.activeChatId, s.savedQueries);
      return { chats: next };
    });
  },

  // Push context entry after successful SQL
  pushContext: (chatId, entry) => {
    set(s => ({
      chats: s.chats.map(c => {
        if (c.id !== chatId) return c;
        const ctx = [...(c.context ?? []), entry].slice(-8);
        return { ...c, context: ctx };
      })
    }));
  },

  saveSqlQuery: (sql) => {
    if (!sql) return;
    set(s => {
      if (s.savedQueries.includes(sql)) return {};
      const next = [sql, ...s.savedQueries];
      persistState(s.chats, s.activeChatId, next);
      return { savedQueries: next };
    });
  },

  setFeedback: (chatId, msgId, rating) => {
    set(s => ({
      chats: s.chats.map(c => {
        if (c.id !== chatId) return c;
        return { ...c, messages: c.messages.map(m => m.id === msgId ? { ...m, _feedback: rating } : m) };
      })
    }));
  },
}));

export default useChatStore;

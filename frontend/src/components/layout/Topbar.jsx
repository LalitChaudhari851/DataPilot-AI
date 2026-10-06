import { useState, useRef, useEffect } from 'react';
import { Menu, Plus, PanelLeft, PanelLeftClose, Database, ChevronDown, Check, Server, Layers } from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import useChatStore from '../../store/useChatStore';

export default function Topbar({ onMenuClick }) {
  const chat = useChatStore(s => s.getActiveChat());
  const health = useChatStore(s => s.health);
  const newChat = useChatStore(s => s.newChat);
  const selectedSchema = useChatStore(s => s.selectedSchema);
  const sidebarCollapsed = useChatStore(s => s.sidebarCollapsed);
  const setSidebarCollapsed = useChatStore(s => s.setSidebarCollapsed);

  // Database selector state
  const selectedDbId = useChatStore(s => s.selectedDbId);
  const availableDatabases = useChatStore(s => s.availableDatabases);
  const setSelectedDbId = useChatStore(s => s.setSelectedDbId);
  const [dbDropdownOpen, setDbDropdownOpen] = useState(false);
  const dropdownRef = useRef(null);

  // Close dropdown on click outside
  useEffect(() => {
    function handleClickOutside(event) {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target)) {
        setDbDropdownOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const statusText = health.status === 'healthy' ? 'Healthy' :
                     health.status === 'degraded' ? 'Degraded' : 'Connecting...';
  const statusDotClass = health.status === 'healthy' ? 'dot-online' :
                          health.status === 'degraded' ? 'dot-warning' : 'bg-white/20';

  const queryCount = chat?.messages?.filter(m => m.role === 'user').length ?? 0;

  const currentDb = availableDatabases.find(d => d.db_id === selectedDbId) || {
    db_id: selectedDbId,
    name: selectedDbId === 'E_commerce' ? 'E-Commerce (Spider)' : 'PlainSQL SaaS',
    table_count: selectedDbId === 'E_commerce' ? 11 : 22,
    dialect: 'mysql',
    target: selectedDbId === 'E_commerce' ? 'TiDB Cloud (ecommerce)' : 'TiDB Cloud (chatbot)',
  };

  return (
    <header
      className="flex items-center gap-3 px-4 h-[var(--topbar-h)] flex-shrink-0 relative z-30"
      style={{
        background: 'var(--surface-05)',
        borderBottom: '1px solid var(--border-1)',
      }}
    >
      {/* Sidebar toggle for desktop */}
      <button
        onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
        className="hidden lg:flex w-8 h-8 rounded-lg items-center justify-center text-t4 hover:text-t2 hover:bg-white/5 transition-colors focus-ring"
        title={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
        aria-label="Toggle sidebar"
      >
        {sidebarCollapsed ? <PanelLeft size={16} /> : <PanelLeftClose size={16} />}
      </button>

      {/* Mobile menu trigger */}
      <button
        onClick={onMenuClick}
        className="lg:hidden w-8 h-8 rounded-lg flex items-center justify-center text-t3 hover:text-t2 hover:bg-white/5 transition-colors focus-ring"
        aria-label="Open menu"
      >
        <Menu size={16} />
      </button>

      {/* Database Context Selector */}
      <div className="relative" ref={dropdownRef}>
        <button
          onClick={() => setDbDropdownOpen(v => !v)}
          className="flex items-center gap-2 px-2.5 py-1.5 rounded-lg border text-xs font-semibold transition-all focus-ring"
          style={{
            background: dbDropdownOpen ? 'var(--surface-2)' : 'rgba(255, 255, 255, 0.04)',
            borderColor: dbDropdownOpen ? 'var(--brand)' : 'var(--border-2)',
            color: 'var(--text-1)',
          }}
          title="Switch Active Database"
          aria-expanded={dbDropdownOpen}
        >
          <div className="flex items-center justify-center w-5 h-5 rounded bg-brand/15 text-brand-light">
            <Database size={12} />
          </div>
          <span className="font-semibold text-white whitespace-nowrap">
            {currentDb.name}
          </span>
          <span className="hidden sm:inline text-[10px] font-mono font-medium px-1.5 py-0.5 rounded bg-white/[0.06] text-t3 border border-white/[0.06]">
            {currentDb.table_count} tables
          </span>
          <span className="hidden md:inline text-[9px] font-semibold tracking-wide uppercase px-1 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
            TiDB
          </span>
          <ChevronDown size={13} className={`text-t4 transition-transform duration-200 ${dbDropdownOpen ? 'rotate-180' : ''}`} />
        </button>

        {/* Database Dropdown Menu */}
        <AnimatePresence>
          {dbDropdownOpen && (
            <motion.div
              initial={{ opacity: 0, y: 4, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 3, scale: 0.98 }}
              transition={{ duration: 0.15 }}
              className="absolute left-0 top-full mt-1.5 w-80 rounded-xl shadow-2xl border z-50 overflow-hidden"
              style={{
                background: '#12131a',
                borderColor: 'var(--border-2)',
                boxShadow: '0 12px 32px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(255, 255, 255, 0.05)',
              }}
            >
              <div className="px-3.5 py-2.5 border-b border-white/[0.06] flex items-center justify-between bg-white/[0.02]">
                <div className="flex items-center gap-1.5">
                  <Layers size={13} className="text-brand-light" />
                  <span className="text-[11px] font-bold uppercase tracking-wider text-t3">Active Database Context</span>
                </div>
                <span className="text-[10px] font-mono text-emerald-400 bg-emerald-500/10 border border-emerald-500/20 px-1.5 py-0.5 rounded">
                  Live Cluster
                </span>
              </div>

              <div className="p-1.5 space-y-1">
                {availableDatabases.map(db => {
                  const isSelected = db.db_id === selectedDbId;
                  return (
                    <button
                      key={db.db_id}
                      onClick={() => {
                        setSelectedDbId(db.db_id);
                        setDbDropdownOpen(false);
                      }}
                      className={`w-full text-left p-2.5 rounded-lg transition-all flex items-start gap-3 border ${
                        isSelected
                          ? 'bg-brand-dim/30 border-brand/40 text-white'
                          : 'hover:bg-white/[0.04] border-transparent text-t2 hover:text-white'
                      }`}
                    >
                      <div className={`mt-0.5 w-6 h-6 rounded-md flex items-center justify-center flex-shrink-0 ${
                        isSelected ? 'bg-brand text-white shadow-sm' : 'bg-surface-2 text-t3'
                      }`}>
                        <Database size={13} />
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center justify-between mb-0.5">
                          <span className="text-xs font-semibold">{db.name}</span>
                          <span className="text-[10px] font-mono text-t4 bg-white/[0.04] px-1.5 py-0.2 rounded border border-white/[0.06]">
                            {db.table_count || (db.db_id === 'E_commerce' ? 11 : 22)} tbls
                          </span>
                        </div>
                        <p className="text-[11px] text-t4 leading-relaxed line-clamp-1 mb-1">
                          {db.description}
                        </p>
                        <div className="flex items-center gap-2 text-[10px] text-t4">
                          <span className="text-emerald-400 font-mono flex items-center gap-1">
                            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 inline-block animate-pulse" />
                            {db.target || 'TiDB Cloud'}
                          </span>
                          <span>•</span>
                          <span className="uppercase font-mono">{db.dialect || 'mysql'}</span>
                        </div>
                      </div>
                      {isSelected && (
                        <div className="w-4 h-4 rounded-full bg-brand flex items-center justify-center text-white flex-shrink-0 mt-1">
                          <Check size={10} strokeWidth={3} />
                        </div>
                      )}
                    </button>
                  );
                })}
              </div>

              <div className="px-3.5 py-2 border-t border-white/[0.06] bg-white/[0.01] flex items-center justify-between text-[11px] text-t4">
                <span>Cluster: TiDB Cloud AliCloud SG</span>
                <span className="font-mono text-[10px]">Zero Schema Leakage</span>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {/* Conversation title */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <h1 className="text-sm font-semibold text-white truncate">
            {chat?.title ?? 'New Analysis'}
          </h1>
          {selectedSchema !== 'default' && (
            <span className="text-[10px] font-semibold font-mono bg-brand-dim text-brand-light border border-brand/20 px-1.5 py-0.5 rounded uppercase">
              Table: {selectedSchema}
            </span>
          )}
        </div>
        <p className="text-[11px] text-t4 hidden sm:block font-medium">
          {queryCount > 0
            ? `${queryCount} ${queryCount === 1 ? 'query' : 'queries'} in thread`
            : `AI-assisted queries routed to ${currentDb.name}`}
        </p>
      </div>

      {/* Right section */}
      <div className="flex items-center gap-2.5">
        {/* Health status indicator */}
        <div
          className="hidden sm:flex items-center gap-2 px-2.5 py-1.2 rounded-lg bg-surface-1 border border-border-1 hover:border-border-2 transition-colors cursor-help"
          title={`Server latency: ${health.latency || 0}ms`}
        >
          <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${statusDotClass}`} />
          <span className="text-[11px] text-t3 font-medium select-none">{statusText}</span>
          {health.latency && (
            <span className="text-[10px] font-mono text-t4 tabular-nums select-none">{health.latency}ms</span>
          )}
        </div>

        {/* Mobile health dot only */}
        <div className={`sm:hidden w-2 h-2 rounded-full ${statusDotClass}`}
          title={statusText}
          role="status"
          aria-label={statusText}
        />

        {/* New Chat */}
        <button
          onClick={newChat}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold text-t2 hover:text-white transition-all bg-surface-1 hover:bg-surface-2 border border-border-2 hover:border-brand/40 focus-ring"
          title="New chat (Ctrl+N)"
          aria-label="New chat"
        >
          <Plus size={13} className="text-brand-light" />
          <span className="hidden sm:inline">New</span>
        </button>
      </div>
    </header>
  );
}

import { useState, useEffect, useRef, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { 
  Plus, 
  MessageSquare, 
  Bookmark, 
  Trash2, 
  X, 
  Database, 
  Sparkles, 
  Search, 
  PanelLeftClose, 
  PanelLeft, 
  Settings, 
} from 'lucide-react';
import useChatStore from '../../store/useChatStore';
import SchemaTree from './sidebar/SchemaTree';

function SectionHeader({ title, count, collapsed, onToggle, isSidebarCollapsed }) {
  if (isSidebarCollapsed) return <div className="h-px bg-border-1 my-3 mx-2" />;

  return (
    <button
      onClick={onToggle}
      className="flex items-center justify-between px-3 py-2 w-full group mt-4 first:mt-0"
    >
      <div className="flex items-center gap-1.5">
        {onToggle && (
          <svg
            width="11"
            height="11"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            className={`text-t4 transition-transform duration-200 ${!collapsed ? 'rotate-90' : ''}`}
          >
            <path d="m9 18 6-6-6-6" />
          </svg>
        )}
        <span className="text-[10px] font-bold uppercase tracking-widest text-t4 group-hover:text-t3 transition-colors">{title}</span>
      </div>
      {count != null && (
        <span className="text-[10px] text-t4 font-mono tabular-nums px-1.5 py-0.5 rounded bg-white/[0.03] border border-white/[0.04]">{count}</span>
      )}
    </button>
  );
}

function EmptyChats({ onNewChat }) {
  return (
    <div className="px-3 py-6 text-center">
      <div className="w-9 h-9 mx-auto mb-2.5 rounded-xl flex items-center justify-center"
        style={{ background: 'var(--surface-2)', border: '1px solid var(--border-1)' }}>
        <Sparkles size={14} className="text-t3" />
      </div>
      <p className="text-t3 text-xs mb-0.5 font-semibold">No conversations</p>
      <p className="text-t4 text-[11px] mb-3 leading-normal">Start your first AI analysis</p>
      <button
        onClick={onNewChat}
        className="text-[11px] font-semibold px-3 py-1.5 rounded-lg transition-colors border-glow"
        style={{ color: '#818cf8', background: 'var(--brand-dim)', border: '1px solid rgba(99,102,241,0.2)' }}
      >
        New analysis
      </button>
    </div>
  );
}

export default function Sidebar({ open, onClose }) {
  const chats = useChatStore(s => s.chats);
  const activeChatId = useChatStore(s => s.activeChatId);
  const savedQueries = useChatStore(s => s.savedQueries);
  const health = useChatStore(s => s.health);
  const selectedSchema = useChatStore(s => s.selectedSchema);
  const newChat = useChatStore(s => s.newChat);
  const selectChat = useChatStore(s => s.selectChat);
  const deleteChat = useChatStore(s => s.deleteChat);
  const sidebarCollapsed = useChatStore(s => s.sidebarCollapsed);
  const setSidebarCollapsed = useChatStore(s => s.setSidebarCollapsed);

  const [searchQuery, setSearchQuery] = useState('');
  const [hoveredChat, setHoveredChat] = useState(null);
  const [schemaCollapsed, setSchemaCollapsed] = useState(false);
  const [savedCollapsed, setSavedCollapsed] = useState(false);
  
  const searchInputRef = useRef(null);
  const touchStartX = useRef(null);

  // ── Close handler: closes drawer + focuses chat composer ─
  const handleClose = useCallback(() => {
    onClose?.();
    requestAnimationFrame(() => {
      document.querySelector('#composer-input')?.focus();
    });
  }, [onClose]);

  // ── When mobile drawer opens, ensure sidebar isn't collapsed ─
  useEffect(() => {
    if (open && sidebarCollapsed) {
      setSidebarCollapsed(false);
    }
  }, [open, sidebarCollapsed, setSidebarCollapsed]);

  // ── Body scroll lock when mobile drawer is open ──────────
  useEffect(() => {
    if (open) {
      document.body.style.overflow = 'hidden';
    } else {
      document.body.style.overflow = '';
    }
    return () => { document.body.style.overflow = ''; };
  }, [open]);

  // ── Escape key closes mobile drawer ──────────────────────
  useEffect(() => {
    if (!open) return;
    const handler = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        handleClose();
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [open, handleClose]);

  // Global Ctrl+K shortcut to focus search
  useEffect(() => {
    const handleKeyDown = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault();
        if (sidebarCollapsed) {
          setSidebarCollapsed(false);
        }
        setTimeout(() => searchInputRef.current?.focus(), 50);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [sidebarCollapsed, setSidebarCollapsed]);

  const handleSavedQuery = (query) => {
    window.dispatchEvent(new CustomEvent('plainsql:submit', { detail: { query } }));
    if (open) handleClose();
  };

  const handleNewChat = () => {
    newChat();
    if (open) handleClose();
  };

  // Callback for when a table is selected (closes mobile drawer)
  const handleTableSelected = useCallback(() => {
    if (open) handleClose();
  }, [open, handleClose]);

  // ── Touch swipe-left-to-close ────────────────────────────
  const handleTouchStart = useCallback((e) => {
    touchStartX.current = e.touches[0].clientX;
  }, []);

  const handleTouchEnd = useCallback((e) => {
    if (touchStartX.current === null) return;
    const delta = e.changedTouches[0].clientX - touchStartX.current;
    if (delta < -60) handleClose(); // 60px left swipe threshold
    touchStartX.current = null;
  }, [handleClose]);

  // Filter logic — schema filtering is handled by SchemaTree internally
  const filteredChats = chats.filter(c => 
    c.title.toLowerCase().includes(searchQuery.toLowerCase())
  );
  const filteredSaved = savedQueries.filter(q => 
    q.toLowerCase().includes(searchQuery.toLowerCase())
  );

  return (
    <>
      {/* Mobile scrim — dark translucent backdrop */}
      <AnimatePresence>
        {open && (
          <motion.div
            key="scrim"
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            onClick={handleClose}
            className="fixed inset-0 z-30 bg-black/60 backdrop-blur-sm lg:hidden"
            aria-hidden="true"
          />
        )}
      </AnimatePresence>

      <motion.aside
        initial={false}
        animate={{ width: sidebarCollapsed ? 64 : 280 }}
        transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
        onTouchStart={handleTouchStart}
        onTouchEnd={handleTouchEnd}
        className={[
          'flex flex-col h-full flex-shrink-0 border-r',
          // Desktop: normal flow
          'relative z-auto',
          // Mobile (<lg): fixed overlay drawer with CSS transition
          'max-lg:fixed max-lg:inset-y-0 max-lg:left-0 max-lg:z-40',
          'max-lg:transition-transform max-lg:duration-300 max-lg:ease-[cubic-bezier(0.16,1,0.3,1)]',
          open ? 'max-lg:translate-x-0' : 'max-lg:-translate-x-full',
        ].join(' ')}
        style={{
          background: 'var(--surface-0)',
          borderColor: 'var(--border-1)',
        }}
      >
        {/* Brand + Mobile close */}
        <div className={`flex items-center ${sidebarCollapsed ? 'justify-center px-2 py-4' : 'justify-between px-4 py-4'}`} style={{ borderBottom: '1px solid var(--border-1)' }}>
          <div className="flex items-center gap-3 min-w-0">
            <div 
              onClick={() => sidebarCollapsed && setSidebarCollapsed(false)}
              className="w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0 shadow-lg cursor-pointer bg-glow border-glow"
              style={{ background: 'linear-gradient(135deg, var(--brand), var(--brand-cyan))' }}
            >
              <span className="text-white font-extrabold text-sm tracking-tight">D</span>
            </div>
            {!sidebarCollapsed && (
              <div className="flex-1 min-w-0">
                <p className="font-bold text-white text-sm leading-tight tracking-tight">DataPilot</p>
                <p className="text-[10px] text-t4 font-semibold uppercase tracking-wider">Agentic AI Data Analyst</p>
              </div>
            )}
          </div>
          
          <div className="flex items-center gap-1">
            {/* New chat (desktop only) */}
            {!sidebarCollapsed && (
              <button
                onClick={handleNewChat}
                className="hidden lg:flex w-7 h-7 rounded-lg items-center justify-center transition-colors hover:bg-white/5 text-t3 hover:text-t2 focus-ring"
                title="New chat (Ctrl+N)"
                aria-label="New chat"
              >
                <Plus size={15} />
              </button>
            )}
            {/* Mobile close button — always rendered on mobile regardless of sidebarCollapsed */}
            <button
              onClick={handleClose}
              className="lg:hidden w-7 h-7 rounded-lg flex items-center justify-center transition-colors hover:bg-white/5 text-t3 hover:text-t2 focus-ring"
              aria-label="Close sidebar"
              style={{ pointerEvents: 'auto' }}
            >
              <X size={15} />
            </button>
          </div>
        </div>

        {/* Search Input Area */}
        {!sidebarCollapsed ? (
          <div className="px-3 pt-3">
            <div className="sidebar-search flex items-center gap-2 px-2.5 py-1.5 text-t3 focus-within:text-t2 relative">
              <Search size={13} className="text-t4 flex-shrink-0" />
              <input
                ref={searchInputRef}
                id="sidebar-search-input"
                type="text"
                placeholder="Search resources..."
                value={searchQuery}
                onChange={e => setSearchQuery(e.target.value)}
                className="bg-transparent border-0 outline-none text-xs w-full text-white placeholder-t4"
              />
              {searchQuery ? (
                <button 
                  onClick={() => setSearchQuery('')}
                  className="w-4 h-4 rounded-full hover:bg-white/10 flex items-center justify-center text-t4 hover:text-white"
                >
                  <X size={10} />
                </button>
              ) : (
                <span className="hidden sm:inline text-[9px] font-mono tracking-wider bg-white/[0.04] border border-white/[0.06] text-t4 px-1 py-0.5 rounded leading-none">⌘K</span>
              )}
            </div>
          </div>
        ) : (
          <div className="flex justify-center pt-3 px-2">
            <button
              onClick={() => { setSidebarCollapsed(false); setTimeout(() => searchInputRef.current?.focus(), 100); }}
              className="w-8 h-8 rounded-lg flex items-center justify-center text-t4 hover:text-t2 hover:bg-white/5 transition-all focus-ring"
              title="Search (Ctrl+K)"
            >
              <Search size={15} />
            </button>
          </div>
        )}

        {/* Scrollable body */}
        <div className="flex-1 overflow-y-auto scrollbar-none py-2 px-1">
          
          {/* New Chat Button (Standard size) */}
          {!sidebarCollapsed ? (
            <div className="px-2 pb-2">
              <button
                onClick={handleNewChat}
                className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-xs font-semibold text-t2 hover:text-white transition-all border border-dashed border-border-2 hover:border-brand/40 bg-surface-1 focus-ring"
              >
                <Plus size={13} className="text-brand-light" />
                New analysis
              </button>
            </div>
          ) : (
            <div className="flex justify-center pb-2 px-1">
              <button
                onClick={handleNewChat}
                className="w-8 h-8 rounded-lg flex items-center justify-center text-brand-light hover:text-white bg-brand-dim border border-brand/20 hover:bg-brand/25 transition-all focus-ring"
                title="New analysis (Ctrl+N)"
              >
                <Plus size={15} />
              </button>
            </div>
          )}

          {/* ── Schema Explorer ────────────────────────────── */}
          {!sidebarCollapsed ? (
            <SchemaTree
              schemaCollapsed={schemaCollapsed}
              onToggleSchema={() => setSchemaCollapsed(v => !v)}
              onTableSelected={handleTableSelected}
              searchQuery={searchQuery}
            />
          ) : (
            <div className="flex justify-center py-2">
              <button
                onClick={() => { setSidebarCollapsed(false); setSchemaCollapsed(false); }}
                className={`w-8 h-8 rounded-lg flex items-center justify-center transition-all focus-ring ${
                  selectedSchema !== 'default' ? 'text-brand-light bg-brand-dim border border-brand/20' : 'text-t4 hover:text-t2 hover:bg-white/5'
                }`}
                title={selectedSchema === 'default' ? "Database schema (All tables)" : `Selected table: ${selectedSchema}`}
              >
                <Database size={15} />
              </button>
            </div>
          )}

          {/* History */}
          {!sidebarCollapsed ? (
            <>
              <SectionHeader title="Conversations" count={filteredChats.length || null} />
              <div className="px-1 space-y-0.5 pb-2">
                {filteredChats.length === 0 && searchQuery && (
                  <p className="text-t4 text-center text-xs py-4">No results match query</p>
                )}
                {filteredChats.length === 0 && !searchQuery && (
                  <EmptyChats onNewChat={handleNewChat} />
                )}
                {filteredChats.map(chat => {
                  const isActive = chat.id === activeChatId;
                  const queryCount = chat.messages.filter(m => m.role === 'user').length;
                  return (
                    <motion.div
                      key={chat.id}
                      layout
                      onHoverStart={() => setHoveredChat(chat.id)}
                      onHoverEnd={() => setHoveredChat(null)}
                      onClick={() => { selectChat(chat.id); if (open) handleClose(); }}
                      className={`
                        nav-item relative group flex items-center gap-2.5 px-3 py-2 cursor-pointer
                        ${isActive ? 'active text-white font-medium' : 'text-t2'}
                      `}
                    >
                      <MessageSquare size={13} className={isActive ? 'text-brand-light flex-shrink-0' : 'text-t4 flex-shrink-0'} />
                      <div className="flex-1 min-w-0">
                        <span className="text-xs truncate block">
                          {chat.title}
                        </span>
                      </div>
                      {queryCount > 0 && hoveredChat !== chat.id && (
                        <span className="text-t4 text-[10px] font-mono bg-white/[0.02] border border-white/[0.04] px-1 rounded tabular-nums">{queryCount}</span>
                      )}
                      {hoveredChat === chat.id && (
                        <button
                          onClick={e => { e.stopPropagation(); deleteChat(chat.id); }}
                          className="flex-shrink-0 w-4 h-4 rounded flex items-center justify-center hover:bg-danger/10 text-t4 hover:text-danger transition-colors"
                          aria-label={`Delete chat: ${chat.title}`}
                        >
                          <Trash2 size={11} />
                        </button>
                      )}
                    </motion.div>
                  );
                })}
              </div>
            </>
          ) : (
            <div className="py-2">
              <div className="h-px bg-border-1 my-1 mx-2" />
              <div className="flex flex-col gap-1 items-center">
                {filteredChats.slice(0, 8).map(chat => {
                  const isActive = chat.id === activeChatId;
                  return (
                    <button
                      key={chat.id}
                      onClick={() => selectChat(chat.id)}
                      className={`w-8 h-8 rounded-lg flex items-center justify-center transition-all focus-ring relative ${
                        isActive ? 'text-brand-light bg-brand-dim border border-brand/20' : 'text-t4 hover:text-t2 hover:bg-white/5'
                      }`}
                      title={chat.title}
                    >
                      <MessageSquare size={14} />
                      {isActive && <span className="absolute left-0 top-1/2 -translate-y-1/2 w-1 h-3 rounded-r bg-brand-light" />}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {/* Saved queries */}
          {!sidebarCollapsed ? (
            <>
              <SectionHeader
                title="Saved Queries"
                count={filteredSaved.length || null}
                collapsed={savedCollapsed}
                onToggle={() => setSavedCollapsed(v => !v)}
              />
              <AnimatePresence initial={false}>
                {!savedCollapsed && (
                  <motion.div
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: 'auto', opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    className="overflow-hidden"
                  >
                    <div className="px-1 space-y-0.5 pb-4">
                      {filteredSaved.map((q, i) => (
                        <button
                          key={i}
                          onClick={() => handleSavedQuery(q)}
                          className="w-full flex items-start gap-2.5 px-3 py-2 rounded-lg text-left hover:bg-white/[0.03] transition-all group/saved focus-ring"
                        >
                          <Bookmark size={12} className="text-t4 mt-0.5 flex-shrink-0 group-hover/saved:text-brand-light transition-colors" />
                          <span className="text-xs text-t3 group-hover/saved:text-t2 transition-colors line-clamp-2 leading-normal">{q}</span>
                        </button>
                      ))}
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </>
          ) : (
            <div className="py-1">
              <div className="h-px bg-border-1 my-1 mx-2" />
              <div className="flex flex-col gap-1 items-center">
                <button
                  onClick={() => { setSidebarCollapsed(false); setSavedCollapsed(false); }}
                  className="w-8 h-8 rounded-lg flex items-center justify-center text-t4 hover:text-t2 hover:bg-white/5 transition-all focus-ring"
                  title="Saved queries"
                >
                  <Bookmark size={14} />
                </button>
              </div>
            </div>
          )}
        </div>

        {/* Footer — Profile, Settings, and Sidebar Collapse Trigger */}
        <div style={{ borderTop: '1px solid var(--border-1)' }} className="p-3 bg-surface-05">
          {/* Health status indicator */}
          {!sidebarCollapsed ? (
            <div className="flex items-center gap-2 px-2 mb-3">
              <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                health.status === 'healthy' ? 'dot-online' :
                health.status === 'degraded' ? 'dot-warning' : 'bg-white/20'
              }`} />
              <span className="text-[10px] text-t4 font-semibold uppercase tracking-wider flex-1">
                {health.status === 'healthy' ? 'All systems active' :
                 health.status === 'degraded' ? 'Degraded service' : 'Connecting...'}
              </span>
              {health.latency && (
                <span className="text-[10px] font-mono text-t4 opacity-60 tabular-nums">{health.latency}ms</span>
              )}
            </div>
          ) : (
            <div className="flex justify-center mb-3">
              <div 
                className={`w-2 h-2 rounded-full flex-shrink-0 cursor-help ${
                  health.status === 'healthy' ? 'dot-online' :
                  health.status === 'degraded' ? 'dot-warning' : 'bg-white/20'
                }`}
                title={health.status === 'healthy' ? `All systems active (${health.latency || 0}ms)` : 'Connecting...'}
              />
            </div>
          )}

          {/* Profile row */}
          {!sidebarCollapsed ? (
            <div className="flex items-center justify-between gap-1 px-1">
              <div className="flex items-center gap-2.5 min-w-0">
                <div className="w-7 h-7 rounded-full flex items-center justify-center text-[10px] font-black text-white uppercase shadow-md flex-shrink-0"
                  style={{ background: 'linear-gradient(135deg, var(--brand), var(--brand-violet))' }}>
                  LC
                </div>
                <div className="flex flex-col min-w-0">
                  <span className="text-xs text-t2 font-semibold truncate leading-tight">Lalit Chaudhari</span>
                  <span className="text-[10px] text-t4 leading-none">Developer</span>
                </div>
              </div>
              <div className="flex items-center gap-0.5">
                <button
                  className="w-6 h-6 rounded flex items-center justify-center text-t4 hover:text-t2 hover:bg-white/5 transition-colors focus-ring"
                  title="Settings"
                  aria-label="Settings"
                >
                  <Settings size={13} />
                </button>
                <button
                  onClick={() => setSidebarCollapsed(true)}
                  className="hidden lg:flex w-6 h-6 rounded items-center justify-center text-t4 hover:text-t2 hover:bg-white/5 transition-colors focus-ring"
                  title="Collapse sidebar"
                  aria-label="Collapse sidebar"
                >
                  <PanelLeftClose size={13} />
                </button>
              </div>
            </div>
          ) : (
            <div className="flex flex-col items-center gap-2">
              <div className="w-7 h-7 rounded-full flex items-center justify-center text-[10px] font-black text-white uppercase shadow-md"
                title="Lalit Chaudhari"
                style={{ background: 'linear-gradient(135deg, var(--brand), var(--brand-violet))' }}>
                LC
              </div>
              <button
                className="w-7 h-7 rounded-lg flex items-center justify-center text-t4 hover:text-t2 hover:bg-white/5 transition-colors focus-ring"
                title="Settings"
                aria-label="Settings"
              >
                <Settings size={14} />
              </button>
              <button
                onClick={() => setSidebarCollapsed(false)}
                className="w-7 h-7 rounded-lg flex items-center justify-center text-t4 hover:text-brand-light hover:bg-brand-dim/30 transition-colors focus-ring"
                title="Expand sidebar"
                aria-label="Expand sidebar"
              >
                <PanelLeft size={14} />
              </button>
            </div>
          )}
        </div>
      </motion.aside>
    </>
  );
}

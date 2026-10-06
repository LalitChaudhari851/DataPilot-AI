import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Copy, Bookmark, ChevronDown, ChevronRight, Code2, Check, Play, Edit3, Loader2 } from 'lucide-react';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism';
import useChatStore from '../../store/useChatStore';
import { formatSQL } from '../../utils/sqlFormatter';
import { executeManualQuery } from '../../api/client';

const customStyle = {
  ...oneDark,
  'pre[class*="language-"]': {
    ...oneDark['pre[class*="language-"]'],
    background: 'transparent',
    padding: '0',
    margin: '0',
    fontSize: '12.5px',
    lineHeight: '1.7',
  },
  'code[class*="language-"]': {
    ...oneDark['code[class*="language-"]'],
    background: 'transparent',
    fontSize: '12.5px',
  },
};

export default function SQLBlock({ sql, messageId, dbId = 'default', onResultUpdate }) {
  const addToast = useChatStore(s => s.addToast);
  const saveSqlQuery = useChatStore(s => s.saveSqlQuery);
  const [collapsed, setCollapsed] = useState(false);
  const [copied, setCopied] = useState(false);
  const [saved, setSaved] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [editedSql, setEditedSql] = useState(() => formatSQL(sql));
  const [isRunning, setIsRunning] = useState(false);
  const [editError, setEditError] = useState(null);

  if (!sql) return null;

  const prettySql = formatSQL(sql);
  const lineCount = prettySql.split('\n').length;

  const handleCopy = async () => {
    await navigator.clipboard.writeText(isEditing ? editedSql : prettySql).catch(() => {});
    setCopied(true);
    addToast('SQL copied to clipboard', 'success');
    setTimeout(() => setCopied(false), 2000);
  };

  const handleSave = () => {
    saveSqlQuery(isEditing ? editedSql : prettySql);
    setSaved(true);
    addToast('Query saved to library', 'success');
    setTimeout(() => setSaved(false), 2000);
  };

  const handleRunManual = async () => {
    if (!editedSql.trim() || isRunning) return;
    setIsRunning(true);
    setEditError(null);
    try {
      const res = await executeManualQuery({ sql: editedSql.trim(), db_id: dbId });
      addToast(`Executed successfully (${res.row_count} rows)`, 'success');
      if (onResultUpdate) {
        onResultUpdate(res);
      }
      setIsEditing(false);
    } catch (err) {
      setEditError(err.message || 'Execution failed');
    } finally {
      setIsRunning(false);
    }
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-xl overflow-hidden mb-4"
      style={{
        border: '1px solid rgba(99,102,241,0.18)',
        background: 'rgba(99,102,241,0.03)',
      }}
    >
      {/* Header */}
      <div
        className="flex items-center justify-between px-4 py-2.5"
        style={{
          background: 'rgba(99,102,241,0.06)',
          borderBottom: '1px solid rgba(99,102,241,0.10)',
        }}
      >
        <div className="flex items-center gap-2">
          <Code2 size={13} className="text-brand-light" />
          <span className="text-xs font-bold text-brand-light">SQL Query</span>
          <span className="text-[10px] text-t4 font-mono font-medium bg-white/[0.03] border border-white/[0.04] px-1 rounded tabular-nums">
            {lineCount} {lineCount === 1 ? 'line' : 'lines'}
          </span>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => {
              setIsEditing(v => !v);
              if (!isEditing) setEditedSql(prettySql);
            }}
            className={`flex items-center gap-1 px-2.5 py-1 rounded-lg text-xs font-semibold transition-all ${
              isEditing ? 'bg-brand/20 text-brand-light' : 'text-t3 hover:text-white hover:bg-white/10'
            }`}
            aria-label="Edit and re-run SQL"
          >
            <Edit3 size={11} />
            <span className="hidden sm:inline">{isEditing ? 'Cancel' : 'Edit & Re-run'}</span>
          </button>
          <button
            onClick={handleSave}
            className="flex items-center gap-1 px-2.5 py-1 rounded-lg text-xs font-semibold text-t3 hover:text-white hover:bg-white/10 transition-all"
            aria-label="Save query"
          >
            {saved ? <Check size={11} className="text-success" /> : <Bookmark size={11} />}
            <span className="hidden sm:inline">{saved ? 'Saved' : 'Save'}</span>
          </button>
          <button
            onClick={handleCopy}
            className="flex items-center gap-1 px-2.5 py-1 rounded-lg text-xs font-semibold text-t3 hover:text-white hover:bg-white/10 transition-all"
            aria-label="Copy SQL"
          >
            {copied ? <Check size={11} className="text-success" /> : <Copy size={11} />}
            <span className="hidden sm:inline">{copied ? 'Copied' : 'Copy'}</span>
          </button>
          <button
            onClick={() => setCollapsed(v => !v)}
            className="p-1 rounded-lg text-t4 hover:text-white hover:bg-white/10 transition-all"
            aria-label={collapsed ? 'Expand SQL' : 'Collapse SQL'}
          >
            {collapsed ? <ChevronRight size={13} /> : <ChevronDown size={13} />}
          </button>
        </div>
      </div>

      {/* Code Editor / Display */}
      <AnimatePresence initial={false}>
        {!collapsed && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            {isEditing ? (
              <div className="p-3.5 bg-black/40">
                <textarea
                  value={editedSql}
                  onChange={(e) => setEditedSql(e.target.value)}
                  className="w-full h-36 bg-black/50 border border-brand/30 rounded-lg p-3 font-mono text-xs text-white leading-relaxed focus:outline-none focus:border-brand resize-y"
                  placeholder="SELECT ..."
                />
                {editError && (
                  <div className="mt-2 text-xs text-danger font-medium p-2 rounded bg-red-500/10 border border-red-500/20">
                    {editError}
                  </div>
                )}
                <div className="mt-2.5 flex items-center justify-between">
                  <span className="text-[10px] text-t4">
                    Re-running validates safety rules and guardrails before executing.
                  </span>
                  <button
                    onClick={handleRunManual}
                    disabled={isRunning}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-brand text-white hover:bg-brand-light transition-all disabled:opacity-50"
                  >
                    {isRunning ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
                    <span>Run Query</span>
                  </button>
                </div>
              </div>
            ) : (
              <div className="px-4 py-3.5 overflow-x-auto font-mono bg-white/[0.015]">
                <SyntaxHighlighter
                  language="sql"
                  style={customStyle}
                  customStyle={{ background: 'transparent', padding: 0, margin: 0 }}
                  wrapLongLines={false}
                  showLineNumbers={lineCount > 1}
                  lineNumberStyle={{
                    color: 'rgba(255,255,255,0.14)',
                    fontSize: '11px',
                    paddingRight: '12px',
                    minWidth: '2em',
                    userSelect: 'none',
                  }}
                >
                  {prettySql}
                </SyntaxHighlighter>
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

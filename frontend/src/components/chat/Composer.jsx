import { useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { ArrowUp, Loader2, X } from 'lucide-react';
import useChatStore from '../../store/useChatStore';

const SAAS_QUICK_PROMPTS = [
  { text: 'Show net revenue retention by customer segment for the last 4 quarters', label: 'NRR by Segment' },
  { text: 'Churn risk with open P1 tickets', label: 'Churn Risk' },
  { text: 'Slowest SQL executions this week', label: 'Performance' },
];

const ECOMMERCE_QUICK_PROMPTS = [
  { text: 'What are the top 5 product categories by sales?', label: 'Top Categories' },
  { text: 'Find top 5 customer cities by total orders', label: 'Top Cities' },
  { text: 'Find average freight value for delivered orders', label: 'Delivered Freight' },
];

export default function Composer({ onSubmit }) {
  const isSending = useChatStore(s => s.isSending);
  const selectedDbId = useChatStore(s => s.selectedDbId);
  const quickPrompts = selectedDbId === 'E_commerce' ? ECOMMERCE_QUICK_PROMPTS : SAAS_QUICK_PROMPTS;
  const [value, setValue] = useState('');
  const [isFocused, setIsFocused] = useState(false);
  const textareaRef = useRef(null);

  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = 'auto';
    ta.style.height = `${Math.min(ta.scrollHeight, 150)}px`;
  }, [value]);

  useEffect(() => {
    const handler = (e) => {
      setValue(e.detail.query);
      textareaRef.current?.focus();
    };
    window.addEventListener('plainsql:submit', handler);
    return () => window.removeEventListener('plainsql:submit', handler);
  }, []);

  const handleSubmit = (e) => {
    e?.preventDefault();
    const q = value.trim();
    if (!q || isSending) return;
    onSubmit(q);
    setValue('');
    if (textareaRef.current) textareaRef.current.style.height = 'auto';
  };

  const canSend = value.trim().length > 0 && !isSending;

  return (
    <div
      className="flex-shrink-0 px-4 pb-4 pt-3 relative z-10"
      style={{
        background: 'var(--surface-05)',
        borderTop: '1px solid var(--border-1)',
      }}
    >
      <div className="mx-auto max-w-4xl">
        {/* Quick prompts */}
        <div className={`mb-3.5 flex gap-2 overflow-x-auto scrollbar-none transition-opacity duration-200 ${isSending ? 'opacity-30 pointer-events-none' : ''}`}>
          {isSending && (
            <motion.div
              initial={{ opacity: 0, width: 0 }}
              animate={{ opacity: 1, width: 'auto' }}
              className="flex items-center gap-2 flex-shrink-0 pr-2 border-r border-border-1 mr-1"
            >
              <Loader2 size={12} className="animate-spin text-brand-light" />
              <span className="text-xs text-t3 font-semibold whitespace-nowrap">Analyzing...</span>
            </motion.div>
          )}
          {quickPrompts.map(prompt => (
            <button
              key={prompt.text}
              type="button"
              onClick={() => setValue(prompt.text)}
              disabled={isSending}
              className="glass-interactive flex-shrink-0 rounded-full px-3 py-1.5 text-xs text-t3 hover:text-t2 font-medium border border-border-1 disabled:cursor-not-allowed"
            >
              <span className="text-brand-light font-bold text-[9px] uppercase tracking-wider mr-1">{prompt.label}</span>
              {prompt.text}
            </button>
          ))}
        </div>

        <form onSubmit={handleSubmit}>
          <div
            className={`flex items-end gap-3 rounded-2xl px-4 py-3 border transition-all duration-300 ${
              isFocused 
                ? 'border-brand/40 bg-surface-hover shadow-[0_0_24px_rgba(99,102,241,0.08),0_8px_32px_rgba(0,0,0,0.25)]' 
                : 'border-border-2 bg-surface-1 shadow-[0_8px_24px_rgba(0,0,0,0.15)]'
            }`}
          >
            <textarea
              id="composer-input"
              ref={textareaRef}
              value={value}
              onChange={e => setValue(e.target.value)}
              onFocus={() => setIsFocused(true)}
              onBlur={() => setIsFocused(false)}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  handleSubmit();
                }
              }}
              disabled={isSending}
              rows={1}
              placeholder={
                selectedDbId === 'E_commerce'
                  ? 'Ask about E-Commerce sales, categories, orders, customers, or sellers...'
                  : 'Ask about SaaS metrics, revenue retention, tickets, ARR, or performance...'
              }
              className="min-h-[24px] flex-1 resize-none bg-transparent text-sm leading-relaxed text-white outline-none placeholder:text-t4 disabled:opacity-50 font-medium"
              aria-label="Type your query"
            />

            {value && (
              <button 
                type="button"
                onClick={() => { setValue(''); textareaRef.current?.focus(); }}
                className="mb-1.5 w-6 h-6 rounded-full hover:bg-white/5 flex items-center justify-center text-t4 hover:text-white"
              >
                <X size={12} />
              </button>
            )}

            <motion.button
              type="submit"
              disabled={!canSend}
              whileHover={canSend ? { scale: 1.05 } : {}}
              whileTap={canSend ? { scale: 0.95 } : {}}
              className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-xl transition-all duration-200 disabled:opacity-25"
              style={{
                background: canSend
                  ? 'linear-gradient(135deg, var(--brand), var(--brand-cyan))'
                  : 'var(--surface-3)',
                boxShadow: canSend
                  ? '0 4px 16px rgba(99,102,241,0.25)'
                  : 'none',
              }}
              aria-label="Send query"
            >
              <ArrowUp size={14} className="text-white" />
            </motion.button>
          </div>
        </form>

        <div className="mt-2 flex justify-between px-1 font-medium">
          <span className="text-[10px] text-t4">
            Press <kbd className="bg-white/[0.04] px-1 py-0.5 rounded font-mono text-[9px] border border-white/[0.06]">Enter</kbd> to run · <kbd className="bg-white/[0.04] px-1 py-0.5 rounded font-mono text-[9px] border border-white/[0.06]">Shift+Enter</kbd> for newline
          </span>
          <span className="hidden text-[10px] text-t4 sm:inline flex items-center gap-1">
            <span className="w-1 h-1 rounded-full bg-success inline-block" /> Read-only sandbox environment
          </span>
        </div>
      </div>
    </div>
  );
}

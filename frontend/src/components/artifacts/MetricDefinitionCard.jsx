import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Info, ChevronDown } from 'lucide-react';

export default function MetricDefinitionCard({ columns = [], userQuery = '', sql = '' }) {
  const [expanded, setExpanded] = useState(false);

  const isNRR = columns.some(c => /nrr/i.test(c)) || /net revenue retention|nrr/i.test(userQuery) || /nrr/i.test(sql);
  if (!isNRR) return null;

  return (
    <div className="mb-4 rounded-xl border border-indigo-500/20 bg-indigo-500/[0.04] p-3 text-xs">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Info size={13} className="text-brand-light flex-shrink-0" />
          <span className="font-semibold text-t2">
            Metric Definition: <span className="text-white font-mono font-bold">NRR (Proxy Retention)</span> = Active ARR / Total ARR × 100
          </span>
        </div>
        <button
          onClick={() => setExpanded(v => !v)}
          className="flex items-center gap-1 font-semibold text-brand-light hover:text-white transition-colors focus-ring rounded px-1.5 py-0.5"
          aria-label="Toggle metric definition details"
        >
          <span>{expanded ? 'Hide formula' : 'View formula'}</span>
          <ChevronDown size={12} className={`transition-transform duration-200 ${expanded ? 'rotate-180' : ''}`} />
        </button>
      </div>

      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="mt-2.5 pt-2.5 border-t border-indigo-500/15 text-t3 space-y-1.5 leading-relaxed font-sans">
              <p>
                <strong className="text-t2 font-semibold">Canonical SaaS NRR:</strong>{' '}
                <code className="text-brand-light bg-black/30 px-1 py-0.5 rounded font-mono text-[11px]">
                  (Starting ARR + Expansion - Contraction - Churn) / Starting ARR
                </code>
              </p>
              <p>
                <strong className="text-t2 font-semibold">Database Implementation:</strong>{' '}
                <code className="text-brand-light bg-black/30 px-1 py-0.5 rounded font-mono text-[11px]">
                  ROUND(SUM(CASE WHEN status = 'active' THEN contracted_arr ELSE 0 END) * 100.0 / SUM(contracted_arr), 2)
                </code>
              </p>
              <p className="text-[11px] text-t4">
                Because this relational schema tracks active vs cancelled subscription snapshots rather than cohort expansion delta tables, this calculation represents the active contracted ARR retention proxy.
              </p>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { 
  ChevronDown, 
  Code2, 
  Cpu, 
  GitBranch, 
  Lightbulb, 
  Info,
  Terminal
} from 'lucide-react';
import SQLBlock from './SQLBlock';
import PipelineTrace from '../pipeline/PipelineTrace';
import MetaBadges from './MetaBadges';

export default function TechnicalDetailsAccordion({ 
  sql, 
  messageId, 
  explanation, 
  pipelineStep, 
  isChatMode, 
  stageText,
  insights = [],
  metadata = {},
  dbId = 'default',
  onResultUpdate
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [activeTab, setActiveTab] = useState('sql'); // 'sql' | 'reasoning' | 'trace' | 'insights' | 'meta'

  if (!sql && !explanation && !insights.length) {
    return null;
  }

  const tabs = [
    { id: 'sql', label: 'SQL Query', icon: Code2, count: null },
    { id: 'reasoning', label: 'How Computed', icon: Cpu, count: null },
    { id: 'trace', label: 'Pipeline Trace', icon: GitBranch, count: null },
    { id: 'insights', label: 'Observations', icon: Lightbulb, count: insights.length || null },
    { id: 'meta', label: 'Execution Meta', icon: Info, count: null },
  ];

  return (
    <div className="mt-4 pt-3 border-t border-border-1">
      {/* Accordion Toggle Header */}
      <button
        onClick={() => setIsOpen(v => !v)}
        className="w-full flex items-center justify-between py-2 px-3 rounded-xl bg-surface-1 hover:bg-surface-hover border border-border-1 text-xs text-t3 hover:text-white transition-all focus-ring"
        aria-label="Toggle technical execution details"
      >
        <div className="flex items-center gap-2">
          <Terminal size={13} className="text-brand-light" />
          <span className="font-semibold text-t2">Technical & Execution Details</span>
          <span className="text-[10px] text-t4 font-mono">
            {isOpen ? 'Expanded' : '(SQL, Pipeline Trace, Reasoning)'}
          </span>
        </div>
        <ChevronDown size={13} className={`transition-transform duration-200 ${isOpen ? 'rotate-180' : ''}`} />
      </button>

      {/* Accordion Body */}
      <AnimatePresence>
        {isOpen && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.25 }}
            className="overflow-hidden"
          >
            <div className="mt-3 rounded-xl p-3 bg-surface-05 border border-border-1">
              {/* Tab Navigation */}
              <div className="flex items-center gap-1 overflow-x-auto pb-2 mb-3 border-b border-border-1 scrollbar-none">
                {tabs.map((tab) => {
                  const Icon = tab.icon;
                  const isActive = activeTab === tab.id;
                  return (
                    <button
                      key={tab.id}
                      onClick={() => setActiveTab(tab.id)}
                      className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-semibold whitespace-nowrap transition-all ${
                        isActive
                          ? 'bg-brand/15 text-brand-light border border-brand/30'
                          : 'text-t4 hover:text-t2 hover:bg-white/[0.03] border border-transparent'
                      }`}
                    >
                      <Icon size={12} />
                      <span>{tab.label}</span>
                      {tab.count !== null && (
                        <span className="text-[9px] px-1 py-0.2 rounded bg-white/10 font-mono">
                          {tab.count}
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>

              {/* Tab Content */}
              <div>
                {activeTab === 'sql' && (
                  <div>
                    <SQLBlock 
                      sql={sql} 
                      messageId={messageId} 
                      dbId={dbId}
                      onResultUpdate={onResultUpdate}
                    />
                  </div>
                )}

                {activeTab === 'reasoning' && (
                  <div className="rounded-lg p-3.5 bg-surface-1 border border-border-1 text-xs text-t2 leading-relaxed">
                    <p className="font-semibold text-white mb-1.5">Compilation & Reasoning:</p>
                    <p className="text-t3">{explanation || 'SQL compiled directly from schema relationships and query constraints.'}</p>
                  </div>
                )}

                {activeTab === 'trace' && (
                  <div>
                    <PipelineTrace 
                      activeStep={pipelineStep} 
                      isChatMode={isChatMode} 
                      stageText={stageText} 
                    />
                  </div>
                )}

                {activeTab === 'insights' && (
                  <div className="space-y-2">
                    {insights.length > 0 ? (
                      insights.map((item, idx) => (
                        <div key={idx} className="flex items-start gap-2.5 p-2.5 rounded-lg bg-surface-1 border border-border-1 text-xs text-t2">
                          <span className="w-4 h-4 rounded-full bg-brand/15 text-brand-light font-bold flex items-center justify-center text-[10px] flex-shrink-0 mt-0.5">
                            {idx + 1}
                          </span>
                          <span className="leading-relaxed">{String(item)}</span>
                        </div>
                      ))
                    ) : (
                      <p className="text-xs text-t4 italic p-2">No additional insights available.</p>
                    )}
                  </div>
                )}

                {activeTab === 'meta' && (
                  <div className="p-2 space-y-2">
                    <MetaBadges 
                      intent={metadata.intent}
                      executionTimeMs={metadata.executionTimeMs}
                      rowCount={metadata.rowCount}
                      dbId={dbId}
                    />
                    <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 pt-2 text-[11px] text-t3 font-mono">
                      <div className="p-2 rounded bg-surface-1 border border-border-1">
                        <span className="text-t4 block text-[9px] uppercase">Trace ID</span>
                        <span className="truncate block">{metadata.traceId || 'trace-active'}</span>
                      </div>
                      <div className="p-2 rounded bg-surface-1 border border-border-1">
                        <span className="text-t4 block text-[9px] uppercase">Database ID</span>
                        <span className="truncate block">{dbId}</span>
                      </div>
                      <div className="p-2 rounded bg-surface-1 border border-border-1">
                        <span className="text-t4 block text-[9px] uppercase">Latency</span>
                        <span>{metadata.executionTimeMs ? `${metadata.executionTimeMs.toFixed(1)}ms` : '0ms'}</span>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

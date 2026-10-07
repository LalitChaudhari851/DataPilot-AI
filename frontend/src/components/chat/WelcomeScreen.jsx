import { motion } from 'framer-motion';
import { 
  BarChart3, 
  DatabaseZap, 
  GitBranch, 
  ShieldCheck, 
  Sparkles, 
  Workflow, 
  ArrowRight,
  Search,
  Zap,
  Eye
} from 'lucide-react';

const PROMPTS = [
  {
    icon: BarChart3,
    title: 'Expansion revenue',
    query: 'Show net revenue retention by customer segment for the last 4 quarters',
    color: '#6366f1',
  },
  {
    icon: Workflow,
    title: 'Pipeline quality',
    query: 'Which opportunities have high ARR but stalled for more than 30 days?',
    color: '#06b6d4',
  },
  {
    icon: GitBranch,
    title: 'Support impact',
    query: 'Compare churn risk for customers with critical tickets versus healthy accounts',
    color: '#a78bfa',
  },
  {
    icon: DatabaseZap,
    title: 'Product usage',
    query: 'Rank workspaces by query volume, failed executions, and active users this month',
    color: '#34d399',
  },
];

const CAPABILITIES = [
  { icon: Search, label: 'Hybrid RAG', desc: 'Vector + BM25 retrieval combined' },
  { icon: Zap, label: 'Smart routing', desc: 'Picks the best LLM for the task' },
  { icon: ShieldCheck, label: 'SQL guardrails', desc: 'Read-only, validated before execution' },
  { icon: Eye, label: 'Full transparency', desc: 'See every step of the pipeline' },
];

export default function WelcomeScreen({ onPrompt }) {
  return (
    <div className="relative flex min-h-full flex-col items-center justify-center px-4 py-10">
      
      {/* Background radial glow */}
      <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[500px] h-[500px] pointer-events-none opacity-20 filter blur-[120px]"
        style={{ background: 'radial-gradient(circle, var(--brand) 0%, transparent 70%)' }} />

      {/* Hero section */}
      <div className="w-full max-w-2xl mx-auto text-center mb-8 relative z-10">
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="inline-flex items-center gap-2 rounded-full px-3.5 py-1.5 text-xs font-semibold mb-6"
          style={{
            background: 'var(--brand-dim)',
            border: '1px solid rgba(99, 102, 241, 0.2)',
            color: 'var(--brand-light)',
          }}
        >
          <Sparkles size={12} className="text-brand-light" />
          Agentic AI Data Analyst
        </motion.div>

        <motion.h2
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.04 }}
          className="text-3xl md:text-4xl font-extrabold leading-tight text-white mb-4 tracking-tight"
        >
          Ask the business question.
          <br />
          <span className="text-gradient">DataPilot constructs the answer.</span>
        </motion.h2>

        <motion.p
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.08 }}
          className="text-t3 text-sm leading-relaxed max-w-lg mx-auto font-medium"
        >
          Retrieving schemas, compiling SQL, validating queries, and synthesizing reports — 
          monitored in real-time with full transparency.
        </motion.p>
      </div>

      {/* Capabilities row */}
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.12 }}
        className="w-full max-w-2xl mx-auto grid grid-cols-2 sm:grid-cols-4 gap-2 mb-8 relative z-10"
      >
        {CAPABILITIES.map((item, i) => {
          const IconComp = item.icon;
          return (
            <motion.div
              key={item.label}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.14 + i * 0.04 }}
              className="rounded-xl p-3 text-center bg-surface-1 border border-border-1 hover:border-border-2 transition-all hover:bg-surface-hover"
            >
              <div className="w-8 h-8 rounded-lg bg-white/[0.03] border border-white/[0.05] flex items-center justify-center mx-auto mb-2 text-brand-light">
                <IconComp size={15} />
              </div>
              <p className="text-xs font-semibold text-t2 mb-0.5">{item.label}</p>
              <p className="text-[10px] text-t4 leading-normal font-medium hidden sm:block">{item.desc}</p>
            </motion.div>
          );
        })}
      </motion.div>

      {/* Prompt cards */}
      <div className="w-full max-w-2xl mx-auto relative z-10">
        <motion.p
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.2 }}
          className="text-[10px] font-bold uppercase tracking-widest text-t4 mb-3 px-1"
        >
          Featured templates
        </motion.p>

        <div className="grid gap-2 sm:grid-cols-2">
          {PROMPTS.map(({ icon: Icon, title, query, color }, i) => (
            <motion.button
              key={query}
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.22 + i * 0.05, duration: 0.3 }}
              whileHover={{ y: -2, boxShadow: `0 8px 30px ${color}10` }}
              whileTap={{ scale: 0.99 }}
              onClick={() => onPrompt(query)}
              className="group rounded-xl p-4 text-left transition-all border border-border-1 bg-surface-1 hover:bg-surface-hover focus-ring"
              onMouseEnter={e => {
                e.currentTarget.style.borderColor = `${color}40`;
              }}
              onMouseLeave={e => {
                e.currentTarget.style.borderColor = 'var(--border-1)';
              }}
            >
              <div className="flex items-start gap-3">
                <div
                  className="mt-0.5 rounded-lg p-2 flex-shrink-0 transition-colors"
                  style={{
                    background: `${color}12`,
                    border: `1px solid ${color}20`,
                  }}
                >
                  <Icon size={15} style={{ color }} />
                </div>
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-bold text-white mb-1 flex items-center gap-1.5 uppercase tracking-wider">
                    {title}
                    <ArrowRight size={11} className="text-brand-light group-hover:translate-x-0.5 transition-all opacity-0 group-hover:opacity-100" />
                  </p>
                  <p className="text-xs leading-relaxed text-t3 group-hover:text-t2 transition-colors font-medium">{query}</p>
                </div>
              </div>
            </motion.button>
          ))}
        </div>

        {/* Security note */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.5 }}
          className="mt-5 flex items-center justify-center gap-2 py-2 text-xs text-t4 font-semibold"
        >
          <ShieldCheck size={14} className="text-success" />
          Strict read-only safety guardrails & SQL parsing applied
        </motion.div>
      </div>
    </div>
  );
}

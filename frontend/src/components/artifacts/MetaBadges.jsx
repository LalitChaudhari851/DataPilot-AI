import { motion } from 'framer-motion';
import { Tag, Clock, Hash, Search, Database } from 'lucide-react';

const intentColors = {
  analytical: { bg: 'rgba(99,102,241,0.10)', border: 'rgba(99,102,241,0.22)', text: '#818cf8' },
  lookup:     { bg: 'rgba(6,182,212,0.10)',  border: 'rgba(6,182,212,0.22)',  text: '#22d3ee' },
  aggregate:  { bg: 'rgba(167,139,250,0.10)', border: 'rgba(167,139,250,0.22)', text: '#c4b5fd' },
  sql:        { bg: 'rgba(99,102,241,0.10)', border: 'rgba(99,102,241,0.22)', text: '#818cf8' },
  default:    { bg: 'var(--surface-1)', border: 'var(--border-1)', text: 'rgba(255,255,255,0.6)' },
};

function Badge({ icon: Icon, label, value, colors, delay = 0 }) {
  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.9 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ delay, duration: 0.2 }}
      className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-semibold"
      style={{ background: colors.bg, border: `1px solid ${colors.border}` }}
    >
      <Icon size={10} style={{ color: colors.text }} />
      <span className="text-t4">{label}</span>
      <span style={{ color: colors.text }}>{value}</span>
    </motion.div>
  );
}

export default function MetaBadges({ intent, executionTimeMs, rowCount, dbId }) {
  const colors = intentColors[intent?.toLowerCase()] ?? intentColors.default;
  const badges = [];

  const dbName = dbId === 'E_commerce' ? 'E-Commerce' : 'PlainSQL SaaS';
  badges.push({
    icon: Database,
    label: 'DB',
    value: `${dbName} (TiDB)`,
    colors: { bg: 'rgba(129,140,248,0.10)', border: 'rgba(129,140,248,0.25)', text: '#a5b4fc' },
  });

  badges.push({ icon: Search, label: 'RAG', value: 'Active', colors: { bg: 'rgba(6,182,212,0.08)', border: 'rgba(6,182,212,0.18)', text: '#22d3ee' } });

  if (intent) badges.push({ icon: Tag, label: 'Intent', value: intent, colors });
  if (executionTimeMs != null) badges.push({
    icon: Clock, label: 'Latency', value: `${Math.round(executionTimeMs)}ms`,
    colors: { bg: 'rgba(52,211,153,0.08)', border: 'rgba(52,211,153,0.18)', text: '#34d399' }
  });
  if (rowCount != null) badges.push({
    icon: Hash, label: 'Rows', value: rowCount,
    colors: { bg: 'var(--surface-1)', border: 'var(--border-1)', text: 'rgba(255,255,255,0.5)' }
  });

  if (badges.length <= 1) return null; // Don't show just "RAG Active" alone

  return (
    <div className="flex flex-wrap gap-1.5 mb-4">
      {badges.map((b, i) => <Badge key={i} {...b} delay={i * 0.04} />)}
    </div>
  );
}

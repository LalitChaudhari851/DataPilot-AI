import { motion } from 'framer-motion';
import { TrendingUp, TrendingDown, DollarSign, Percent, Award, BarChart2 } from 'lucide-react';
import { 
  classifyDataset, 
  SEMANTIC_TYPES, 
  MEASURE_SUBTYPES, 
  formatSemanticValue,
  toHumanHeader,
  normalizeTimeRows
} from '../../utils/semanticClassifier';

export default function SemanticKPICards({ rows = [] }) {
  if (!rows || rows.length === 0) return null;

  const classification = classifyDataset(rows);
  const cols = Object.keys(rows[0] || {});

  // Extract true measures and dimensions
  const measureCols = cols.filter(c => classification[c]?.type === SEMANTIC_TYPES.MEASURE);
  const dimCols = cols.filter(c => classification[c]?.type === SEMANTIC_TYPES.DIMENSION);
  const timeCols = cols.filter(c => classification[c]?.type === SEMANTIC_TYPES.TIME);

  // If no true measures exist, DO NOT SHOW KPI ROW!
  if (measureCols.length === 0) {
    return null;
  }

  const primaryMeasure = measureCols[0];
  const primaryMeasureInfo = classification[primaryMeasure];
  const primaryDim = dimCols[0];

  const cards = [];

  // 1. Top Performing Entity Card (if a dimension exists)
  if (primaryDim && rows.length > 1) {
    try {
      const bestRow = [...rows].sort((a, b) => {
        const aVal = Number(a[primaryMeasure] || 0);
        const bVal = Number(b[primaryMeasure] || 0);
        return bVal - aVal;
      })[0];

      if (bestRow) {
        const topLabel = String(bestRow[primaryDim] || '').replace(/_/g, ' ');
        const topVal = Number(bestRow[primaryMeasure] || 0);
        cards.push({
          icon: Award,
          label: `Top ${toHumanHeader(primaryDim)}`,
          value: topLabel.length > 18 ? `${topLabel.slice(0, 18)}…` : topLabel,
          subValue: formatSemanticValue(topVal, primaryMeasureInfo),
          highlight: true,
        });
      }
    } catch {
      /* ignore */
    }
  }

  // 2. Primary Measure Total / Highlight Card
  if (primaryMeasureInfo.subType === MEASURE_SUBTYPES.PERCENTAGE) {
    // For percentages: show highest or average percentage
    const validVals = rows.map(r => Number(r[primaryMeasure])).filter(Number.isFinite);
    if (validVals.length > 0) {
      const maxVal = Math.max(...validVals);
      cards.push({
        icon: Percent,
        label: `Peak ${toHumanHeader(primaryMeasure)}`,
        value: formatSemanticValue(maxVal, primaryMeasureInfo),
        subValue: `Across ${rows.length} records`,
      });
    }
  } else {
    // For currency or count: show cumulative total across returned items
    const total = rows.reduce((sum, r) => sum + Number(r[primaryMeasure] || 0), 0);
    cards.push({
      icon: primaryMeasureInfo.subType === MEASURE_SUBTYPES.CURRENCY ? DollarSign : BarChart2,
      label: `Total ${toHumanHeader(primaryMeasure)}`,
      value: formatSemanticValue(total, primaryMeasureInfo),
      subValue: `Across ${rows.length} ${toHumanHeader(primaryDim || 'entries').toLowerCase()}`,
    });
  }

  // 3. Secondary Measure or QoQ Period Comparison Card
  const secondaryMeasure = measureCols[1];
  if (secondaryMeasure) {
    const secInfo = classification[secondaryMeasure];
    const totalSec = rows.reduce((sum, r) => sum + Number(r[secondaryMeasure] || 0), 0);
    cards.push({
      icon: secInfo.subType === MEASURE_SUBTYPES.PERCENTAGE ? Percent : DollarSign,
      label: `Total ${toHumanHeader(secondaryMeasure)}`,
      value: formatSemanticValue(totalSec, secInfo),
      subValue: 'Aggregate',
    });
  } else if (timeCols.length > 0 && rows.length >= 2) {
    // Check if we can calculate meaningful QoQ comparison
    try {
      const { normalizedRows } = normalizeTimeRows(rows, classification);
      const distinctTimes = Array.from(new Set(normalizedRows.map(r => r.__time_sort || r[timeCols[0]])));
      if (distinctTimes.length >= 2) {
        const lastTime = distinctTimes[distinctTimes.length - 1];
        const prevTime = distinctTimes[distinctTimes.length - 2];
        const lastRows = normalizedRows.filter(r => (r.__time_sort || r[timeCols[0]]) === lastTime);
        const prevRows = normalizedRows.filter(r => (r.__time_sort || r[timeCols[0]]) === prevTime);

        const lastSum = lastRows.reduce((acc, r) => acc + Number(r[primaryMeasure] || 0), 0);
        const prevSum = prevRows.reduce((acc, r) => acc + Number(r[primaryMeasure] || 0), 0);

        if (prevSum > 0) {
          const diffPct = ((lastSum - prevSum) / prevSum) * 100;
          const isUp = diffPct >= 0;
          cards.push({
            icon: isUp ? TrendingUp : TrendingDown,
            label: 'Period Trend',
            value: `${isUp ? '+' : ''}${diffPct.toFixed(1)}%`,
            subValue: 'Latest Period vs Prior',
            trendColor: isUp ? 'text-success' : 'text-danger',
          });
        }
      }
    } catch {
      /* ignore */
    }
  }

  // If after checks we have no meaningful cards, don't show row
  if (cards.length === 0) return null;

  return (
    <div className="mb-4 grid gap-3 grid-cols-2 sm:grid-cols-3">
      {cards.map((card, i) => {
        const Icon = card.icon;
        return (
          <motion.div
            key={card.label}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: i * 0.05 }}
            className={`rounded-xl p-3 border transition-all ${
              card.highlight
                ? 'bg-indigo-500/[0.06] border-indigo-500/25 shadow-[0_2px_12px_rgba(99,102,241,0.08)]'
                : 'bg-surface-1 border-border-1'
            }`}
          >
            <div className="mb-1 flex items-center justify-between">
              <span className="text-[10px] font-bold uppercase tracking-wider text-t4">
                {card.label}
              </span>
              <Icon size={12} className={card.highlight ? 'text-brand-light' : 'text-t4'} />
            </div>
            <p className={`truncate font-mono text-base sm:text-lg font-bold ${card.trendColor || 'text-white'}`}>
              {card.value}
            </p>
            {card.subValue && (
              <p className="text-[10px] text-t4 truncate font-medium mt-0.5">
                {card.subValue}
              </p>
            )}
          </motion.div>
        );
      })}
    </div>
  );
}

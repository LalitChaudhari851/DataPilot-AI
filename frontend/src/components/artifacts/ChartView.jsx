import { useState } from 'react';
import { motion } from 'framer-motion';
import { BarChart3, TrendingUp } from 'lucide-react';
import {
  Chart as ChartJS,
  CategoryScale, LinearScale, BarElement, LineElement,
  PointElement, ArcElement, Tooltip, Legend, Filler
} from 'chart.js';
import { Bar, Line } from 'react-chartjs-2';
import {
  classifyDataset,
  SEMANTIC_TYPES,
  toHumanHeader,
  normalizeTimeRows
} from '../../utils/semanticClassifier';

ChartJS.register(CategoryScale, LinearScale, BarElement, LineElement, PointElement, ArcElement, Tooltip, Legend, Filler);

const PALETTE = [
  'rgba(99,102,241,0.85)',   // brand indigo
  'rgba(6,182,212,0.85)',   // cyan
  'rgba(167,139,250,0.85)',  // violet
  'rgba(52,211,153,0.85)',   // emerald
  'rgba(251,191,36,0.85)',   // amber
  'rgba(248,113,113,0.85)',  // rose
  'rgba(59,130,246,0.85)',   // blue
  'rgba(236,72,153,0.85)',   // pink
];

function buildSemanticChartData(rows, chartTypeOverride, backendConfig) {
  const classification = classifyDataset(rows);
  const cols = Object.keys(rows[0] || {});

  const timeCols = cols.filter(c => classification[c]?.type === SEMANTIC_TYPES.TIME);
  const dimCols = cols.filter(c => classification[c]?.type === SEMANTIC_TYPES.DIMENSION);
  const measureCols = cols.filter(c => classification[c]?.type === SEMANTIC_TYPES.MEASURE);

  // If no true measures exist, or single measure with 1 row: no chart!
  if (measureCols.length === 0 || (measureCols.length === 1 && rows.length <= 1)) {
    return null;
  }

  // ─────────────────────────────────────────────────────────────
  // 1. TIME + DIMENSION + MEASURE → Multi-series Time Series Chart
  // ─────────────────────────────────────────────────────────────
  if (timeCols.length > 0 && dimCols.length > 0 && measureCols.length > 0) {
    const primaryDim = dimCols[0];
    const primaryMeasure = measureCols[0];
    const { normalizedRows, timeCol } = normalizeTimeRows(rows, classification);

    // Extract sorted unique time points
    const distinctTimes = Array.from(new Set(normalizedRows.map(r => r[timeCol]))).filter(Boolean);
    const distinctSeries = Array.from(new Set(normalizedRows.map(r => String(r[primaryDim] || '')))).filter(Boolean);

    // Build series datasets
    const datasets = distinctSeries.slice(0, 8).map((seriesName, idx) => {
      const color = PALETTE[idx % PALETTE.length];
      const seriesRows = normalizedRows.filter(r => String(r[primaryDim] || '') === seriesName);
      const dataMap = new Map();
      seriesRows.forEach(r => {
        dataMap.set(r[timeCol], Number(r[primaryMeasure] || 0));
      });

      const data = distinctTimes.map(t => dataMap.has(t) ? dataMap.get(t) : null);

      return {
        label: toHumanHeader(seriesName),
        data,
        borderColor: color,
        backgroundColor: color.replace('0.85', '0.15'),
        pointBackgroundColor: color,
        borderWidth: 2.5,
        tension: 0.3,
        spanGaps: true,
        fill: false,
        pointRadius: 4,
        pointHoverRadius: 6,
      };
    });

    return {
      type: chartTypeOverride || 'line',
      isTimeSeries: true,
      data: {
        labels: distinctTimes,
        datasets,
      },
      measureLabel: toHumanHeader(primaryMeasure),
    };
  }

  // ─────────────────────────────────────────────────────────────
  // 2. DIMENSION + MEASURE → Sorted Bar Chart
  // ─────────────────────────────────────────────────────────────
  if (dimCols.length > 0 && measureCols.length > 0) {
    const primaryDim = dimCols[0];
    const primaryMeasure = measureCols[0];

    // Sort descending by measure value
    const sorted = [...rows].sort((a, b) => Number(b[primaryMeasure] || 0) - Number(a[primaryMeasure] || 0)).slice(0, 20);

    const labels = sorted.map(r => {
      const val = String(r[primaryDim] || '');
      return val.length > 25 ? `${val.slice(0, 25)}…` : val;
    });

    const data = sorted.map(r => Number(r[primaryMeasure] || 0));

    return {
      type: chartTypeOverride || 'bar',
      isTimeSeries: false,
      data: {
        labels,
        datasets: [{
          label: toHumanHeader(primaryMeasure),
          data,
          backgroundColor: PALETTE.slice(0, labels.length),
          borderColor: 'rgba(255,255,255,0.08)',
          borderWidth: 1,
          borderRadius: 6,
        }],
      },
      measureLabel: toHumanHeader(primaryMeasure),
    };
  }

  // ─────────────────────────────────────────────────────────────
  // 3. TIME + MEASURE (Single series over time)
  // ─────────────────────────────────────────────────────────────
  if (timeCols.length > 0 && measureCols.length > 0) {
    const primaryMeasure = measureCols[0];
    const { normalizedRows, timeCol } = normalizeTimeRows(rows, classification);

    const labels = normalizedRows.map(r => String(r[timeCol] || ''));
    const data = normalizedRows.map(r => Number(r[primaryMeasure] || 0));

    return {
      type: chartTypeOverride || 'line',
      isTimeSeries: true,
      data: {
        labels,
        datasets: [{
          label: toHumanHeader(primaryMeasure),
          data,
          borderColor: PALETTE[0],
          backgroundColor: 'rgba(99,102,241,0.1)',
          pointBackgroundColor: PALETTE[1],
          borderWidth: 2.5,
          tension: 0.35,
          fill: true,
          pointRadius: 4,
          pointHoverRadius: 6,
        }],
      },
      measureLabel: toHumanHeader(primaryMeasure),
    };
  }

  return null;
}

export default function ChartView({ rows = [], backendChartConfig = null }) {
  const [chartType, setChartType] = useState(null);

  if (!rows || rows.length < 2) return null;

  const chartModel = buildSemanticChartData(rows, chartType, backendChartConfig);
  if (!chartModel || !chartModel.data || !chartModel.data.datasets.length) {
    return null;
  }

  const effectiveType = chartType || chartModel.type;

  const chartOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: {
        display: chartModel.data.datasets.length > 1,
        position: 'top',
        align: 'end',
        labels: {
          color: 'rgba(255,255,255,0.7)',
          font: { family: 'Inter', size: 11, weight: '500' },
          boxWidth: 10,
          boxHeight: 10,
          borderRadius: 2,
          useBorderRadius: true,
          padding: 12,
        },
      },
      tooltip: {
        backgroundColor: '#0f172a',
        borderColor: 'rgba(255,255,255,0.12)',
        borderWidth: 1,
        titleColor: '#ffffff',
        bodyColor: 'rgba(255,255,255,0.85)',
        padding: 10,
        cornerRadius: 8,
        titleFont: { weight: '700', family: 'Inter', size: 11 },
        bodyFont: { family: 'Inter', size: 11 },
        displayColors: true,
      },
    },
    scales: {
      x: {
        ticks: { color: 'rgba(255,255,255,0.4)', font: { size: 10, family: 'Inter' }, maxRotation: 45 },
        grid: { color: 'rgba(255,255,255,0.03)', drawBorder: false },
      },
      y: {
        title: {
          display: true,
          text: chartModel.measureLabel || 'Value',
          color: 'rgba(255,255,255,0.45)',
          font: { size: 10, family: 'Inter', weight: '600' }
        },
        ticks: { color: 'rgba(255,255,255,0.4)', font: { size: 10, family: 'Inter' } },
        grid: { color: 'rgba(255,255,255,0.04)', drawBorder: false },
      },
    },
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-xl overflow-hidden mb-4 border border-border-1"
    >
      <div
        className="flex items-center justify-between px-4 py-2.5"
        style={{
          background: 'var(--surface-05)',
          borderBottom: '1px solid var(--border-1)',
        }}
      >
        <div className="flex items-center gap-2">
          <BarChart3 size={13} className="text-brand-light" />
          <span className="text-xs font-bold text-t2">
            Visualization: <span className="text-white font-mono">{chartModel.measureLabel}</span>
          </span>
        </div>
        <div className="flex gap-1 p-0.5 rounded-lg bg-surface-1 border border-border-1">
          {[
            { key: 'bar', icon: BarChart3, label: 'Bar' },
            { key: 'line', icon: TrendingUp, label: 'Line' },
          ].map(t => (
            <button
              key={t.key}
              onClick={() => setChartType(t.key)}
              className="flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-semibold transition-all focus-ring"
              style={effectiveType === t.key
                ? { background: 'var(--brand-dim)', color: 'var(--brand-light)', border: '1px solid rgba(99,102,241,0.2)' }
                : { color: 'rgba(255,255,255,0.35)', border: '1px solid transparent' }
              }
              aria-label={`${t.label} chart`}
            >
              <t.icon size={11} />
              {t.label}
            </button>
          ))}
        </div>
      </div>

      <div className="p-4" style={{ height: '300px', background: 'var(--surface-0)' }}>
        {effectiveType === 'line' ? (
          <Line data={chartModel.data} options={chartOptions} />
        ) : (
          <Bar data={chartModel.data} options={chartOptions} />
        )}
      </div>
    </motion.div>
  );
}

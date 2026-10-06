/**
 * frontend/src/utils/semanticClassifier.js
 * 
 * Enterprise Semantic Column Classifier & Time Normalizer
 * Categorizes SQL result columns into semantic roles:
 * - TIME
 * - DIMENSION
 * - MEASURE
 * - IDENTIFIER
 * - TEXT
 * - BOOLEAN
 * - UNKNOWN
 */

export const SEMANTIC_TYPES = {
  TIME: 'TIME',
  DIMENSION: 'DIMENSION',
  MEASURE: 'MEASURE',
  IDENTIFIER: 'IDENTIFIER',
  TEXT: 'TEXT',
  BOOLEAN: 'BOOLEAN',
  UNKNOWN: 'UNKNOWN',
};

export const MEASURE_SUBTYPES = {
  CURRENCY: 'currency',
  PERCENTAGE: 'percentage',
  COUNT: 'count',
  GENERAL: 'general',
};

const ACRONYMS = new Set(['ARR', 'MRR', 'NRR', 'SQL', 'ID', 'UUID', 'QOQ', 'YOY', 'AOV', 'GMV', 'CRM', 'API', 'DB', 'KPI']);

/**
 * Format raw column name into human-readable header:
 * e.g. 'active_arr' -> 'Active ARR', 'nrr_pct' -> 'NRR %', 'product_category_name' -> 'Product Category'
 */
export function toHumanHeader(col) {
  if (!col) return '';
  const lower = String(col).toLowerCase().trim();

  // Special aliases
  if (lower === 'yr' || lower === 'year') return 'Year';
  if (lower === 'qtr' || lower === 'quarter') return 'Quarter';
  if (lower === 'nrr_pct' || lower === 'nrr') return 'NRR %';
  if (lower === 'active_arr') return 'Active ARR';
  if (lower === 'total_arr') return 'Total ARR';
  if (lower === 'total_sales') return 'Total Sales';
  if (lower === 'product_category_name') return 'Product Category';

  return col
    .split('_')
    .map(word => {
      const upper = word.toUpperCase();
      if (ACRONYMS.has(upper)) return upper;
      if (word.toLowerCase() === 'pct') return '%';
      if (word.toLowerCase() === 'cnt') return 'Count';
      if (word.toLowerCase() === 'qty') return 'Qty';
      return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
    })
    .join(' ');
}

/**
 * Classify a single column from its name and sampled values.
 */
export function classifyColumn(colName, sampleValues = []) {
  const name = String(colName).toLowerCase().trim();
  const validVals = sampleValues.filter(v => v !== null && v !== undefined && v !== '');

  // 1. IDENTIFIERS (IDs, UUIDs, Foreign Keys)
  const isIdPattern = /(^(id|uuid|guid|pk|fk)$)|(_id$)|(_uuid$)|(_key$)|(^id_)/i.test(name);
  if (isIdPattern) {
    return {
      type: SEMANTIC_TYPES.IDENTIFIER,
      subType: 'id',
      label: toHumanHeader(colName),
      rawName: colName,
    };
  }

  // 2. TIME COLUMNS (Year, Quarter, Dates, Timestamps)
  const isTimePattern = /(^(yr|year|qtr|quarter|month|mon|day|date|dt|week|time|timestamp|quarter_year|year_quarter)$)|(_(date|dt|time|timestamp|year|yr|qtr|quarter|month|day)$)|(^(year_|month_|qtr_|quarter_))/i.test(name);
  
  // Value-based year detection (e.g. numeric 2020..2030)
  const isAllYears = validVals.length > 0 && validVals.every(v => {
    const num = Number(v);
    return Number.isInteger(num) && num >= 1970 && num <= 2099;
  });

  // Value-based quarter detection (1..4 with quarter-related name)
  const isAllQuarters = (name.includes('qtr') || name.includes('quarter')) && validVals.length > 0 && validVals.every(v => {
    const num = Number(v);
    return Number.isInteger(num) && num >= 1 && num <= 4;
  });

  // Date string check (e.g. 2024-05-12 or 2024-05)
  const isDateString = validVals.length > 0 && validVals.every(v => {
    return typeof v === 'string' && /^\d{4}-\d{2}(-\d{2})?/.test(v);
  });

  if (isTimePattern || isAllYears || isAllQuarters || isDateString) {
    let timeUnit = 'general';
    if (name === 'yr' || name === 'year' || isAllYears) timeUnit = 'year';
    else if (name === 'qtr' || name === 'quarter' || isAllQuarters) timeUnit = 'quarter';
    else if (name.includes('month')) timeUnit = 'month';
    else if (name.includes('date') || isDateString) timeUnit = 'date';

    return {
      type: SEMANTIC_TYPES.TIME,
      subType: timeUnit,
      label: toHumanHeader(colName),
      rawName: colName,
    };
  }

  // 3. BOOLEAN COLUMNS
  const isBoolPattern = /(^(is_|has_|can_|should_))/i.test(name);
  const isAllBoolVals = validVals.length > 0 && validVals.every(v => 
    typeof v === 'boolean' || v === 0 || v === 1 || v === '0' || v === '1' || v === 'true' || v === 'false'
  );
  if (isBoolPattern || isAllBoolVals) {
    return {
      type: SEMANTIC_TYPES.BOOLEAN,
      subType: 'boolean',
      label: toHumanHeader(colName),
      rawName: colName,
    };
  }

  // Check if values are numeric
  const numericCount = validVals.filter(v => Number.isFinite(Number(v))).length;
  const isNumeric = validVals.length > 0 && (numericCount / validVals.length) >= 0.8;

  // 4. MEASURE COLUMNS (Aggregations, Amounts, Metrics)
  if (isNumeric) {
    // Detect measure subtype
    const isCurrency = /(arr|mrr|revenue|sales|price|cost|amount|fee|freight|value|spend|income|payment|turnover|gmv)/i.test(name);
    const isPercentage = /(pct|percent|percentage|rate|ratio|margin|retention|churn_rate|nrr)/i.test(name);
    const isCount = /(count|cnt|qty|quantity|volume|units|orders|tickets|users|subscribers|items|leads|customers|sellers)/i.test(name);

    let subType = MEASURE_SUBTYPES.GENERAL;
    if (isCurrency) subType = MEASURE_SUBTYPES.CURRENCY;
    else if (isPercentage) subType = MEASURE_SUBTYPES.PERCENTAGE;
    else if (isCount) subType = MEASURE_SUBTYPES.COUNT;

    return {
      type: SEMANTIC_TYPES.MEASURE,
      subType,
      label: toHumanHeader(colName),
      rawName: colName,
    };
  }

  // 5. DIMENSION COLUMNS (Categories, Segments, States, Names)
  const isDimensionPattern = /(name|title|category|segment|tier|status|type|state|city|country|region|department|channel|industry|plan|role)/i.test(name);
  if (isDimensionPattern || validVals.some(v => typeof v === 'string')) {
    // Check if it's long text (descriptions, notes, logs)
    const avgLen = validVals.reduce((acc, v) => acc + String(v).length, 0) / (validVals.length || 1);
    if (avgLen > 60 || /(description|comment|review|notes|text|body|message|reason)/i.test(name)) {
      return {
        type: SEMANTIC_TYPES.TEXT,
        subType: 'long_text',
        label: toHumanHeader(colName),
        rawName: colName,
      };
    }

    return {
      type: SEMANTIC_TYPES.DIMENSION,
      subType: 'category',
      label: toHumanHeader(colName),
      rawName: colName,
    };
  }

  return {
    type: SEMANTIC_TYPES.UNKNOWN,
    subType: 'unknown',
    label: toHumanHeader(colName),
    rawName: colName,
  };
}

/**
 * Classify all columns of a dataset.
 */
export function classifyDataset(rows) {
  if (!rows || !rows.length) return {};
  const cols = Object.keys(rows[0] || {});
  const classification = {};

  for (const col of cols) {
    const sampleValues = rows.slice(0, 30).map(r => r[col]);
    classification[col] = classifyColumn(col, sampleValues);
  }

  return classification;
}

/**
 * Filter columns by semantic type.
 */
export function getColumnsByType(classification, type) {
  return Object.keys(classification).filter(col => classification[col].type === type);
}

/**
 * Semantic value formatter for UI presentation.
 */
export function formatSemanticValue(val, colInfo) {
  if (val === null || val === undefined || val === '') {
    return '—';
  }

  const num = Number(val);
  const isNum = Number.isFinite(num);

  if (!colInfo) {
    return isNum ? num.toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(val);
  }

  // TIME FORMATTING
  if (colInfo.type === SEMANTIC_TYPES.TIME) {
    if (colInfo.subType === 'year') {
      return String(val); // Never format year as 2,024!
    }
    if (colInfo.subType === 'quarter') {
      return `Q${val}`;
    }
    return String(val);
  }

  // IDENTIFIER FORMATTING
  if (colInfo.type === SEMANTIC_TYPES.IDENTIFIER) {
    return String(val);
  }

  // MEASURE FORMATTING
  if (colInfo.type === SEMANTIC_TYPES.MEASURE && isNum) {
    if (colInfo.subType === MEASURE_SUBTYPES.CURRENCY) {
      if (Math.abs(num) >= 1_000_000) {
        return `$${(num / 1_000_000).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}M`;
      }
      if (Math.abs(num) >= 1_000) {
        return `$${(num / 1_000).toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}K`;
      }
      return `$${num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    }

    if (colInfo.subType === MEASURE_SUBTYPES.PERCENTAGE) {
      return `${num.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 2 })}%`;
    }

    if (colInfo.subType === MEASURE_SUBTYPES.COUNT) {
      return Math.round(num).toLocaleString();
    }

    return num.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }

  return String(val);
}

/**
 * Normalize time-series rows combining 'yr' and 'qtr' or sorting chronologically.
 */
export function normalizeTimeRows(rows, classification) {
  if (!rows || !rows.length) return { normalizedRows: [], timeCol: null };

  const timeCols = getColumnsByType(classification, SEMANTIC_TYPES.TIME);
  const yrCol = timeCols.find(c => classification[c].subType === 'year' || /yr|year/i.test(c));
  const qtrCol = timeCols.find(c => classification[c].subType === 'quarter' || /qtr|quarter/i.test(c));

  // If both year and quarter exist: combine into normalized 'time_label' (e.g. 'Q1 2024')
  if (yrCol && qtrCol) {
    const withNormalized = rows.map(r => {
      const yr = Number(r[yrCol]);
      const qtr = Number(r[qtrCol]);
      const sortKey = (yr * 10) + qtr;
      return {
        ...r,
        __time_label: `Q${qtr} ${yr}`,
        __time_sort: sortKey,
      };
    });

    // Sort chronologically ascending
    const sorted = [...withNormalized].sort((a, b) => a.__time_sort - b.__time_sort);
    return {
      normalizedRows: sorted,
      timeCol: '__time_label',
      yrCol,
      qtrCol,
    };
  }

  // Single date/time column: sort chronologically
  if (timeCols.length > 0) {
    const primaryTime = timeCols[0];
    const sorted = [...rows].sort((a, b) => {
      const aVal = a[primaryTime];
      const bVal = b[primaryTime];
      if (typeof aVal === 'number' && typeof bVal === 'number') return aVal - bVal;
      return String(aVal ?? '').localeCompare(String(bVal ?? ''));
    });
    return {
      normalizedRows: sorted,
      timeCol: primaryTime,
    };
  }

  return { normalizedRows: rows, timeCol: null };
}

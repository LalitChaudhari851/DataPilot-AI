/**
 * frontend/src/utils/sqlFormatter.js
 * Client-side SQL prettifier for clean multi-line display
 */

const MAJOR_KEYWORDS = [
  'SELECT',
  'FROM',
  'LEFT JOIN',
  'RIGHT JOIN',
  'INNER JOIN',
  'OUTER JOIN',
  'CROSS JOIN',
  'JOIN',
  'WHERE',
  'GROUP BY',
  'HAVING',
  'ORDER BY',
  'LIMIT',
  'OFFSET',
  'UNION ALL',
  'UNION',
];

export function formatSQL(sql) {
  if (!sql) return '';
  const trimmed = String(sql).trim();

  // If already formatted with multiple lines, return trimmed
  if (trimmed.includes('\n')) {
    return trimmed;
  }

  // Prettify single-line SQL
  let formatted = trimmed;

  // Protect string literals
  const stringLiterals = [];
  formatted = formatted.replace(/'(?:''|[^'])*'/g, (match) => {
    stringLiterals.push(match);
    return `__STR_${stringLiterals.length - 1}__`;
  });

  // Insert line breaks before major clauses
  for (const kw of MAJOR_KEYWORDS) {
    const re = new RegExp(`\\b${kw}\\b`, 'gi');
    formatted = formatted.replace(re, `\n${kw.toUpperCase()}`);
  }

  // Handle ON clauses under JOINs (indented)
  formatted = formatted.replace(/\bON\b/gi, '\n    ON');

  // Handle SELECT column list formatting if long
  const selectIdx = formatted.indexOf('SELECT');
  const fromIdx = formatted.indexOf('\nFROM');
  if (selectIdx !== -1 && fromIdx !== -1 && fromIdx > selectIdx) {
    const selectClause = formatted.slice(selectIdx + 6, fromIdx);
    if (selectClause.includes(',')) {
      const items = selectClause.split(',').map(item => item.trim()).filter(Boolean);
      const multilineItems = items.map((it, idx) => `    ${it}${idx < items.length - 1 ? ',' : ''}`).join('\n');
      formatted = `SELECT\n${multilineItems}\n${formatted.slice(fromIdx).trim()}`;
    }
  }

  // Restore string literals
  stringLiterals.forEach((lit, idx) => {
    formatted = formatted.replace(`__STR_${idx}__`, lit);
  });

  return formatted.trim();
}

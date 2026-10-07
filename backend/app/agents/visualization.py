"""
backend/app/agents/visualization.py
Visualization Agent — Semantic chart configs, structured chart specs, and executive insights.
"""

import re
import structlog
from typing import Optional, Dict, Any, List

from app.agents.state import AgentState

logger = structlog.get_logger()

# Chart type selection thresholds
MAX_PIE_CATEGORIES = 6
MIN_LINE_POINTS = 2


def visualization_node(state: AgentState) -> dict:
    """
    Analyze query results using semantic column typing and generate
    validated chart configs, chart specs, and executive insights.
    """
    results = state.get("query_results", [])
    columns = state.get("column_names", [])
    user_query = state.get("user_query", "")
    trace_id = state.get("trace_id", "unknown")

    logger.info("agent_started", agent="visualization", trace_id=trace_id)

    if not results or not columns:
        return {
            "chart_config": None,
            "chart_type": None,
            "chart_spec": None,
            "insights": ["No data returned from the query."],
            "follow_up_questions": _generate_followups_empty(user_query),
        }

    # ── 1. Semantic Column Classification ─────────────────
    classified = _classify_columns(columns, results)
    time_cols = classified["time"]
    dimension_cols = classified["dimension"]
    measure_cols = classified["measure"]

    # ── 2. Determine Chart Type & Configuration ───────────
    chart_config = None
    chart_type = None
    chart_spec = None

    # RULE 1: TIME + DIMENSION + MEASURE → Multi-series Time-Series Chart
    if time_cols and dimension_cols and measure_cols:
        chart_type = "line"
        primary_dim = dimension_cols[0]
        primary_measure = measure_cols[0]
        
        # Normalize and sort chronologically
        normalized_data, x_labels = _normalize_and_sort_time_series(
            results, time_cols, primary_dim, primary_measure
        )

        chart_spec = {
            "type": "line",
            "x": "quarter" if any("qtr" in c.lower() or "quarter" in c.lower() for c in time_cols) else "time",
            "y": primary_measure,
            "series": primary_dim,
        }

        colors = [
            "#6366f1", "#06b6d4", "#a855f7", "#10b981", "#f59e0b",
            "#ec4899", "#3b82f6", "#14b8a6", "#f43f5e", "#84cc16",
        ]

        datasets = []
        for i, (series_name, series_vals) in enumerate(normalized_data.items()):
            color = colors[i % len(colors)]
            datasets.append({
                "label": str(series_name).replace("_", " ").title(),
                "data": series_vals,
                "borderColor": color,
                "backgroundColor": color,
                "tension": 0.3,
                "fill": False,
                "borderWidth": 2.5,
                "pointRadius": 4,
                "pointHoverRadius": 6,
            })

        chart_config = {
            "type": "line",
            "data": {
                "labels": x_labels,
                "datasets": datasets,
            },
            "options": _default_chart_options(primary_measure.replace("_", " ").title()),
        }

    # RULE 2: DIMENSION + MEASURE → Sorted Bar or Doughnut Chart
    elif dimension_cols and measure_cols:
        primary_dim = dimension_cols[0]
        primary_measure = measure_cols[0]

        # Sort descending by measure
        sorted_rows = sorted(
            results,
            key=lambda r: _safe_float(r.get(primary_measure)),
            reverse=True
        )[:20]

        labels = [str(r.get(primary_dim, "")).replace("_", " ").title() for r in sorted_rows]
        values = [_safe_float(r.get(primary_measure)) for r in sorted_rows]

        if len(sorted_rows) <= MAX_PIE_CATEGORIES and not any(v < 0 for v in values):
            chart_type = "doughnut"
        else:
            chart_type = "bar"

        chart_spec = {
            "type": chart_type,
            "x": primary_dim,
            "y": primary_measure,
            "series": None,
        }

        colors = [
            "#6366f1", "#06b6d4", "#a855f7", "#10b981", "#f59e0b",
            "#ec4899", "#3b82f6", "#14b8a6", "#f43f5e", "#84cc16",
        ]

        chart_config = {
            "type": chart_type,
            "data": {
                "labels": labels,
                "datasets": [{
                    "label": primary_measure.replace("_", " ").title(),
                    "data": values,
                    "backgroundColor": colors[:len(labels)],
                    "borderColor": "#1e293b",
                    "borderWidth": 1.5,
                    "borderRadius": 6 if chart_type == "bar" else 0,
                }],
            },
            "options": _default_chart_options(primary_measure.replace("_", " ").title()),
        }

    # RULE 3: Multiple MEASURES without Dimension → Comparison Bar Chart
    elif len(measure_cols) >= 2 and len(results) == 1:
        row = results[0]
        labels = [m.replace("_", " ").title() for m in measure_cols]
        values = [_safe_float(row.get(m)) for m in measure_cols]
        chart_type = "bar"
        chart_spec = {
            "type": "bar",
            "x": "metric",
            "y": "value",
            "series": None,
        }
        chart_config = {
            "type": "bar",
            "data": {
                "labels": labels,
                "datasets": [{
                    "label": "Metric Value",
                    "data": values,
                    "backgroundColor": "#6366f1",
                    "borderRadius": 6,
                }],
            },
            "options": _default_chart_options("Value"),
        }

    # RULE 4: Single MEASURE, TIME only, or IDENTIFIER only → No chart
    else:
        chart_config = None
        chart_type = None
        chart_spec = None

    # ── 3. Executive Insights ─────────────────────────────
    insights = _generate_executive_insights(results, classified)

    # ── 4. Follow-up Suggestions ──────────────────────────
    follow_ups = _generate_followups(user_query, columns, results, measure_cols)

    logger.info(
        "visualization_complete",
        chart_type=chart_type,
        insights_count=len(insights),
        followups_count=len(follow_ups),
    )

    return {
        "chart_config": chart_config,
        "chart_type": chart_type,
        "chart_spec": chart_spec,
        "insights": insights,
        "follow_up_questions": follow_ups,
    }


def _classify_columns(columns: List[str], results: List[dict]) -> Dict[str, List[str]]:
    """Classify columns strictly into semantic types."""
    time_cols = []
    id_cols = []
    measure_cols = []
    dimension_cols = []

    for col in columns:
        col_lower = col.lower().strip()
        sample_vals = [r.get(col) for r in results[:10] if r.get(col) is not None]

        # 1. Identifiers
        if re.search(r"(^(id|uuid|guid|pk|fk)$)|(_id$)|(_uuid$)|(_key$)|(^id_)", col_lower):
            id_cols.append(col)
            continue

        # 2. Time fields
        num_vals = [v for v in sample_vals if isinstance(v, (int, float))]
        is_year = col_lower in ["yr", "year"] or (bool(num_vals) and all(isinstance(v, int) and 1980 <= v <= 2099 for v in num_vals))
        is_quarter = col_lower in ["qtr", "quarter"] or ("quarter" in col_lower and bool(sample_vals) and all(1 <= _safe_float(v) <= 4 for v in sample_vals))
        is_time = any(t in col_lower for t in ["date", "time", "month", "day", "week", "created", "updated"]) or is_year or is_quarter
        if is_time:
            time_cols.append(col)
            continue

        # 3. Measures (Numeric metrics that are not IDs or time)
        numeric_count = sum(1 for v in sample_vals if _is_number(v))
        if sample_vals and (numeric_count / len(sample_vals)) >= 0.8:
            measure_cols.append(col)
            continue

        # 4. Dimension / Categorical
        dimension_cols.append(col)

    # Reorder measures to place primary metrics (nrr_pct, sales, revenue, arr) first
    def _measure_priority(col_name: str) -> int:
        c = col_name.lower()
        if "nrr" in c or "sales" in c or "revenue" in c or "arr" in c:
            return 0
        if "pct" in c or "rate" in c or "price" in c:
            return 1
        return 2

    measure_cols.sort(key=_measure_priority)

    return {
        "time": time_cols,
        "id": id_cols,
        "measure": measure_cols,
        "dimension": dimension_cols,
    }


def _normalize_and_sort_time_series(results, time_cols, dim_col, measure_col):
    """Normalize time fields (e.g. combine yr + qtr) and sort chronologically."""
    yr_col = next((c for c in time_cols if c.lower() in ["yr", "year"]), None)
    qtr_col = next((c for c in time_cols if c.lower() in ["qtr", "quarter"]), None)

    # Extract distinct time points sorted chronologically
    time_sort_map = {}
    row_time_keys = []

    for r in results:
        if yr_col and qtr_col:
            yr = int(_safe_float(r.get(yr_col)))
            qtr = int(_safe_float(r.get(qtr_col)))
            t_label = f"Q{qtr} {yr}"
            t_key = (yr * 10) + qtr
        elif yr_col:
            yr = int(_safe_float(r.get(yr_col)))
            t_label = str(yr)
            t_key = yr
        else:
            raw_time = str(r.get(time_cols[0], ""))
            t_label = raw_time
            t_key = raw_time
        time_sort_map[t_key] = t_label
        row_time_keys.append((t_key, t_label, r))

    sorted_t_keys = sorted(time_sort_map.keys())
    x_labels = [time_sort_map[k] for k in sorted_t_keys]

    # Group series by dimension
    distinct_series = sorted(list(set(str(r.get(dim_col, "")) for r in results)))
    series_data = {s: [None] * len(sorted_t_keys) for s in distinct_series}

    for t_key, t_label, r in row_time_keys:
        idx = sorted_t_keys.index(t_key)
        series_name = str(r.get(dim_col, ""))
        val = _safe_float(r.get(measure_col))
        series_data[series_name][idx] = val

    return series_data, x_labels


def _generate_executive_insights(results: List[dict], classified: Dict[str, List[str]]) -> List[str]:
    """Generate executive insights based strictly on measures grouped by dimensions."""
    insights = []
    measure_cols = classified["measure"]
    dim_cols = classified["dimension"]
    time_cols = classified["time"]

    if not measure_cols:
        return [f"Query returned {len(results)} rows for analysis."]

    primary_measure = measure_cols[0]
    m_label = primary_measure.replace("_", " ").title()

    # 1. Top dimension performer
    if dim_cols:
        dim = dim_cols[0]
        d_label = dim.replace("_", " ").title()
        
        valid_rows = [r for r in results if r.get(dim) is not None and r.get(primary_measure) is not None]
        if valid_rows:
            best_row = max(valid_rows, key=lambda r: _safe_float(r.get(primary_measure)))
            best_dim = str(best_row.get(dim)).replace("_", " ").title()
            best_val = _safe_float(best_row.get(primary_measure))

            val_str = f"{best_val:,.2f}"
            if "pct" in primary_measure.lower() or "rate" in primary_measure.lower():
                val_str = f"{best_val:.1f}%"
            elif any(c in primary_measure.lower() for c in ["sales", "arr", "revenue", "price", "spend"]):
                val_str = f"${best_val:,.2f}"

            time_ctx = ""
            yr_col = next((c for c in time_cols if c.lower() in ["yr", "year"]), None)
            qtr_col = next((c for c in time_cols if c.lower() in ["qtr", "quarter"]), None)
            if yr_col and qtr_col and best_row.get(yr_col) and best_row.get(qtr_col):
                time_ctx = f" in Q{int(_safe_float(best_row.get(qtr_col)))} {int(_safe_float(best_row.get(yr_col)))}"

            insights.append(f"{best_dim} recorded the highest {m_label} at {val_str}{time_ctx}.")

    # 2. Aggregate measure summary
    values = [_safe_float(r.get(primary_measure)) for r in results if r.get(primary_measure) is not None]
    if values and len(values) > 1 and not ("pct" in primary_measure.lower() or "rate" in primary_measure.lower()):
        total_val = sum(values)
        if any(c in primary_measure.lower() for c in ["sales", "arr", "revenue", "price"]):
            insights.append(f"Cumulative {m_label} across reported segments totaled ${total_val:,.2f}.")

    return insights[:3] if insights else [f"Analysis completed across {len(results)} records."]


def _generate_followups(query: str, columns: list, results: list, measure_cols: list) -> list[str]:
    """Generate context-aware follow-up suggestions."""
    followups = []
    q_lower = query.lower()

    if "top" in q_lower or "highest" in q_lower:
        followups.append("Show the bottom performers for comparison")
    if "quarter" in q_lower or "quarters" in q_lower:
        followups.append("Break down month-over-month trend")
    if "segment" in q_lower:
        followups.append("Filter by enterprise accounts only")
    if "category" in q_lower or "categories" in q_lower:
        followups.append("Analyze average order value by category")

    followups.append("Export data to CSV")
    return followups[:4]


def _generate_followups_empty(query: str) -> list[str]:
    return [
        "Show all available records from this table",
        "Inspect available table schema",
        "Try a broader date range",
    ]


def _default_chart_options(y_title: str) -> dict:
    return {
        "responsive": True,
        "maintainAspectRatio": False,
        "plugins": {
            "legend": {
                "display": True,
                "position": "top",
                "align": "end",
                "labels": {"color": "rgba(255,255,255,0.7)", "font": {"family": "Inter", "size": 11}},
            },
            "tooltip": {
                "backgroundColor": "#0f172a",
                "borderColor": "rgba(255,255,255,0.1)",
                "borderWidth": 1,
            },
        },
        "scales": {
            "x": {
                "ticks": {"color": "rgba(255,255,255,0.5)", "font": {"family": "Inter", "size": 10}},
                "grid": {"color": "rgba(255,255,255,0.03)"},
            },
            "y": {
                "title": {"display": True, "text": y_title, "color": "rgba(255,255,255,0.5)"},
                "ticks": {"color": "rgba(255,255,255,0.5)", "font": {"family": "Inter", "size": 10}},
                "grid": {"color": "rgba(255,255,255,0.04)"},
            },
        },
    }


def _safe_float(val) -> float:
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _is_number(val) -> bool:
    if val is None:
        return False
    try:
        float(val)
        return True
    except (ValueError, TypeError):
        return False

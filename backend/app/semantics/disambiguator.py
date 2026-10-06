"""
Semantic Column Disambiguator for PlainSQL.
Analyzes query phrasing and column metadata/values to resolve semantic ambiguities.
"""

import re
import logging
from typing import List, Dict, Any, Optional

try:
    from app.semantics.models import SemanticRole, ColumnSemanticMetadata, DisambiguationResult
except ImportError:
    from backend.app.semantics.models import SemanticRole, ColumnSemanticMetadata, DisambiguationResult

logger = logging.getLogger(__name__)


class SemanticDisambiguator:
    """Disambiguates columns representing overlapping or easily confused business concepts."""

    @staticmethod
    def identify_ambiguities(
        query: str,
        columns_metadata: List[ColumnSemanticMetadata],
    ) -> List[Dict[str, Any]]:
        """Identify potential semantic collisions between columns relevant to the query."""
        q_lower = query.lower()
        ambiguities = []

        # Group columns by table
        tables: Dict[str, List[ColumnSemanticMetadata]] = {}
        for col in columns_metadata:
            tables.setdefault(col.table_name, []).append(col)

        for tbl, cols in tables.items():
            status_cols = [c for c in cols if c.semantic_role == SemanticRole.STATUS]
            date_time_cols = [c for c in cols if c.semantic_role in (SemanticRole.TIMESTAMP, SemanticRole.DATE)]
            amount_cols = [c for c in cols if c.semantic_role == SemanticRole.AMOUNT]
            quantity_cols = [c for c in cols if c.semantic_role == SemanticRole.QUANTITY]

            # 1. Delivery ambiguity: status column vs delivery timestamp column
            delivery_terms = ["deliver", "delivered", "delivery"]
            if any(term in q_lower for term in delivery_terms):
                matching_status = [
                    c for c in status_cols 
                    if any("deliver" in v.lower() for v in c.sample_values) or "status" in c.column_name.lower()
                ]
                matching_time = [
                    c for c in date_time_cols 
                    if any(term in c.column_name.lower() for term in delivery_terms)
                ]
                if matching_status and matching_time:
                    ambiguities.append({
                        "concept": "delivery",
                        "table": tbl,
                        "status_col": matching_status[0],
                        "time_col": matching_time[0],
                    })

            # 2. Revenue vs Volume ambiguity
            volume_terms = ["volume", "units", "items sold", "unit sales", "number of items"]
            revenue_terms = ["revenue", "sales value", "sales turnover", "total sales", "total revenue"]
            if any(term in q_lower for term in volume_terms) or any(term in q_lower for term in revenue_terms):
                if amount_cols and (quantity_cols or any(c.column_name.endswith("_id") for c in cols)):
                    ambiguities.append({
                        "concept": "revenue_vs_volume",
                        "table": tbl,
                        "amount_cols": amount_cols,
                        "quantity_cols": quantity_cols,
                    })

        return ambiguities

    @classmethod
    def disambiguate(
        cls,
        query: str,
        columns_metadata: List[ColumnSemanticMetadata],
    ) -> List[DisambiguationResult]:
        """Perform contextual disambiguation on competing columns."""
        q_lower = query.lower()
        results: List[DisambiguationResult] = []
        ambiguities = cls.identify_ambiguities(query, columns_metadata)

        for amb in ambiguities:
            concept = amb["concept"]
            tbl = amb["table"]

            if concept == "delivery":
                status_col = amb["status_col"]
                time_col = amb["time_col"]

                # Check query intent:
                # Does query ask for delivery date/time/duration/calendar?
                asks_for_date = any(w in q_lower for w in ["date", "time", "when", "duration", "day", "calendar", "timestamp"])
                asks_for_status = any(w in q_lower for w in ["status", "state", "lifecycle", "delivered order", "delivered orders", "orders delivered", "for delivered"])

                if asks_for_date and not asks_for_status:
                    results.append(DisambiguationResult(
                        concept="delivery_event",
                        selected_column=time_col.column_name,
                        table_name=tbl,
                        semantic_role=time_col.semantic_role,
                        reason=f"Query requests date/timing information; prefer timestamp column '{time_col.column_name}' over lifecycle status.",
                        confidence=0.92,
                        alternatives_considered=[status_col.column_name],
                    ))
                else:
                    # Delivered as condition/state
                    has_delivered_val = any("delivered" in v.lower() for v in status_col.sample_values)
                    val_hint = f" (verified value: 'delivered')" if has_delivered_val else ""
                    results.append(DisambiguationResult(
                        concept="delivered_condition",
                        selected_column=status_col.column_name,
                        table_name=tbl,
                        semantic_role=status_col.semantic_role,
                        reason=f"Query filters entities by order lifecycle state{val_hint}; prefer status column '{status_col.column_name}' over timestamp '{time_col.column_name}'.",
                        confidence=0.94 if has_delivered_val else 0.85,
                        alternatives_considered=[time_col.column_name],
                    ))

            elif concept == "revenue_vs_volume":
                amount_cols = amb["amount_cols"]
                quantity_cols = amb["quantity_cols"]
                is_volume = any(w in q_lower for w in ["volume", "units", "items sold", "unit sales", "number of items", "quantity"])
                is_revenue = any(w in q_lower for w in ["revenue", "sales value", "turnover", "total sales", "total revenue", "monetary"])

                if is_volume and not is_revenue:
                    selected = quantity_cols[0].column_name if quantity_cols else "COUNT(item_id)"
                    results.append(DisambiguationResult(
                        concept="sales_volume",
                        selected_column=selected,
                        table_name=tbl,
                        semantic_role=SemanticRole.QUANTITY,
                        reason="Query asks for unit/item sales volume; use quantity column or count of items, NOT monetary amount.",
                        confidence=0.90,
                        alternatives_considered=[c.column_name for c in amount_cols],
                    ))
                elif is_revenue:
                    selected = amount_cols[0].column_name
                    results.append(DisambiguationResult(
                        concept="revenue",
                        selected_column=selected,
                        table_name=tbl,
                        semantic_role=SemanticRole.AMOUNT,
                        reason="Query asks for monetary revenue/sales value; use monetary amount column.",
                        confidence=0.92,
                        alternatives_considered=[c.column_name for c in quantity_cols],
                    ))

        return results

    @classmethod
    def generate_semantic_context_block(
        cls,
        query: str,
        columns_metadata: List[ColumnSemanticMetadata],
    ) -> str:
        """Produce a concise, high-value semantic guidance block for SQL generation."""
        if not columns_metadata:
            return ""

        disambiguations = cls.disambiguate(query, columns_metadata)

        # Collect columns with sample values or high relevance
        q_lower = query.lower()
        relevant_cols: List[ColumnSemanticMetadata] = []
        for col in columns_metadata:
            # Include if involved in disambiguation
            in_disambig = any(
                col.column_name == d.selected_column or col.column_name in d.alternatives_considered
                for d in disambiguations
            )
            # Include if sample values match query tokens
            value_match = any(v.lower() in q_lower for v in col.sample_values if len(v) > 2)
            # Include if column name or synonyms match query
            syn_match = any(syn in q_lower for syn in col.synonyms if len(syn) > 3)

            if in_disambig or value_match or syn_match:
                relevant_cols.append(col)

        if not relevant_cols and not disambiguations:
            return ""

        lines = ["-- BUSINESS SEMANTIC GUIDANCE --"]
        
        # 1. Output explicit disambiguation guidance first
        if disambiguations:
            lines.append("Semantic Column Disambiguation:")
            for d in disambiguations:
                lines.append(f"  * [{d.table_name}] Concept '{d.concept}': Use `{d.selected_column}` ({d.semantic_role.value}). Reason: {d.reason}")

        # 2. Output relevant column semantic roles and known distinct values
        seen = set()
        guidance_lines = []
        for col in relevant_cols:
            key = f"{col.table_name}.{col.column_name}"
            if key in seen:
                continue
            seen.add(key)

            info = f"  * `{col.table_name}`.`{col.column_name}` [role: {col.semantic_role.value}]"
            if col.business_meaning:
                info += f": {col.business_meaning}"
            if col.sample_values:
                info += f" | Known values: {col.sample_values[:8]}"
            guidance_lines.append(info)

        if guidance_lines:
            lines.append("Relevant Field Meanings & Verified Values:")
            lines.extend(guidance_lines)

        return "\n".join(lines)

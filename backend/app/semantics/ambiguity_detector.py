"""
Ambiguity Detector & Clarification Resolver for PlainSQL (Phase 10).
Analyzes queries against semantic metadata to detect semantic collisions,
evaluates confidence thresholds, and resolves user clarification responses.
"""

import re
import uuid
import logging
from typing import List, Dict, Any, Optional, Tuple

try:
    from app.semantics.models import (
        SemanticRole,
        ColumnSemanticMetadata,
        BusinessDefinition,
        AmbiguityType,
        ClarificationCandidate,
        SemanticAmbiguity,
        ClarificationState,
        SemanticAssumption,
    )
    from app.config import get_settings
except ImportError:
    from backend.app.semantics.models import (
        SemanticRole,
        ColumnSemanticMetadata,
        BusinessDefinition,
        AmbiguityType,
        ClarificationCandidate,
        SemanticAmbiguity,
        ClarificationState,
        SemanticAssumption,
    )
    from backend.app.config import get_settings

try:
    import structlog
    logger = structlog.get_logger()
except ImportError:
    import logging
    logger = logging.getLogger(__name__)



class AmbiguityDetector:
    """Detects semantic collisions and resolves interactive user clarifications."""

    @classmethod
    def detect_ambiguities(
        cls,
        query: str,
        columns_metadata: List[ColumnSemanticMetadata],
        business_definitions: Optional[List[BusinessDefinition]] = None,
        auto_execute_threshold: Optional[float] = None,
        clarification_threshold: Optional[float] = None,
    ) -> List[SemanticAmbiguity]:
        """
        Analyze user query and schema metadata to detect semantic ambiguities.
        Returns a list of structured SemanticAmbiguity objects.
        """
        settings = get_settings()
        if auto_execute_threshold is None:
            auto_execute_threshold = getattr(
                settings, "PLAINSQL_SEMANTIC_AUTO_EXECUTE_THRESHOLD", 0.85
            )
        if clarification_threshold is None:
            clarification_threshold = getattr(
                settings, "PLAINSQL_SEMANTIC_CLARIFICATION_THRESHOLD", 0.65
            )

        q_lower = query.lower()
        business_definitions = business_definitions or []
        ambiguities: List[SemanticAmbiguity] = []

        # ── Group columns by table ────────────────────────────────────
        tables: Dict[str, List[ColumnSemanticMetadata]] = {}
        for col in columns_metadata:
            tables.setdefault(col.table_name, []).append(col)

        # ── Check Explicit Business Definitions Precedence (Part 9) ───
        # If an explicit definition matches a query term or synonym, it overrides ambiguous inference
        matched_defs = [
            d for d in business_definitions
            if getattr(d, "active", True) and (
                d.term.lower() in q_lower
                or any(s.lower() in q_lower for s in getattr(d, "synonyms", []))
            )
        ]

        for tbl, cols in tables.items():
            # 1. MULTIPLE TIMESTAMP AMBIGUITY
            cls._check_multiple_timestamp_ambiguity(
                q_lower=q_lower,
                table_name=tbl,
                cols=cols,
                matched_defs=matched_defs,
                auto_thresh=auto_execute_threshold,
                clarif_thresh=clarification_threshold,
                out_list=ambiguities,
            )

            # 2. REVENUE VS VOLUME AMBIGUITY
            cls._check_revenue_vs_volume_ambiguity(
                q_lower=q_lower,
                table_name=tbl,
                cols=cols,
                matched_defs=matched_defs,
                auto_thresh=auto_execute_threshold,
                clarif_thresh=clarification_threshold,
                out_list=ambiguities,
            )

            # 3. STATUS VS TIMESTAMP AMBIGUITY
            cls._check_status_vs_timestamp_ambiguity(
                q_lower=q_lower,
                table_name=tbl,
                cols=cols,
                matched_defs=matched_defs,
                auto_thresh=auto_execute_threshold,
                clarif_thresh=clarification_threshold,
                out_list=ambiguities,
            )

            # 4. MULTIPLE CANDIDATE COLUMNS (e.g. status vs is_active)
            cls._check_multiple_candidate_columns_ambiguity(
                q_lower=q_lower,
                table_name=tbl,
                cols=cols,
                matched_defs=matched_defs,
                auto_thresh=auto_execute_threshold,
                clarif_thresh=clarification_threshold,
                out_list=ambiguities,
            )

        return ambiguities

    @classmethod
    def _check_multiple_timestamp_ambiguity(
        cls,
        q_lower: str,
        table_name: str,
        cols: List[ColumnSemanticMetadata],
        matched_defs: List[BusinessDefinition],
        auto_thresh: float,
        clarif_thresh: float,
        out_list: List[SemanticAmbiguity],
    ):
        """Check for multiple competing timestamp columns for an event (e.g. delivery date)."""
        time_cols = [
            c for c in cols if c.semantic_role in (SemanticRole.TIMESTAMP, SemanticRole.DATE)
        ]
        if len(time_cols) < 2:
            return

        # Check delivery timestamps: customer vs carrier
        delivery_terms = ["deliver", "delivered", "delivery"]
        if any(term in q_lower for term in delivery_terms):
            matching_delivery_times = [
                c for c in time_cols
                if any(t in c.column_name.lower() for t in delivery_terms)
            ]
            if len(matching_delivery_times) >= 2:
                candidates: List[ClarificationCandidate] = []
                for c in matching_delivery_times:
                    c_name = c.column_name.lower()
                    if "customer" in c_name:
                        cid = "customer_delivery"
                        label = "Customer delivery date"
                        desc = "Date and time when the customer received the order"
                        keywords = ["customer", "received", "client", "delivered to customer", "customer delivery"]
                    elif "carrier" in c_name:
                        cid = "carrier_delivery"
                        label = "Carrier delivery date"
                        desc = "Date and time when the carrier delivered/handled the package"
                        keywords = ["carrier", "shipping", "courier", "postal", "carrier delivery", "transit"]
                    else:
                        cid = c.column_name
                        label = c.column_name.replace("_", " ").title()
                        desc = f"Timestamp column {c.column_name}"
                        keywords = [c.column_name]

                    # Score confidence based on query wording
                    has_kw = any(kw in q_lower for kw in keywords)
                    conf = 0.95 if has_kw else 0.60
                    candidates.append(
                        ClarificationCandidate(
                            candidate_id=cid,
                            table_name=table_name,
                            column_name=c.column_name,
                            semantic_role=c.semantic_role,
                            label=label,
                            description=desc,
                            sample_values=c.sample_values,
                            confidence=conf,
                            synonyms_or_keywords=keywords,
                        )
                    )

                # Precedence: check if explicit business definition matches
                for bdef in matched_defs:
                    for cand in candidates:
                        cand_col = cand.column_name.lower()
                        cand_qual = f"{cand.table_name.lower()}.{cand_col}"
                        is_pref = (
                            cand_col in [p.lower() for p in bdef.preferred_columns]
                            or cand_qual in [p.lower() for p in bdef.preferred_columns]
                            or any(cand_col == p.lower().split(".")[-1] for p in bdef.preferred_columns)
                        )
                        if is_pref:
                            cand.confidence = 0.98

                # Sort candidates by confidence descending
                candidates.sort(key=lambda x: x.confidence, reverse=True)
                top_conf = candidates[0].confidence
                gap = top_conf - candidates[1].confidence if len(candidates) > 1 else top_conf

                # Clarification decision logic (Part 4)
                needs_clarification = (top_conf < auto_thresh and gap < 0.15) or (top_conf < clarif_thresh)

                out_list.append(
                    SemanticAmbiguity(
                        ambiguity_id=str(uuid.uuid4())[:8],
                        ambiguity_type=AmbiguityType.MULTIPLE_TIMESTAMP,
                        concept="delivery_date",
                        question="I found multiple possible delivery dates. Which one do you mean?",
                        candidates=candidates,
                        recommended_candidate_id=candidates[0].candidate_id,
                        confidence_gap=round(gap, 3),
                        top_confidence=round(top_conf, 3),
                        requires_clarification=needs_clarification,
                        reason="Multiple delivery timestamps exist without explicit qualifier in query"
                        if needs_clarification
                        else f"Dominant delivery timestamp identified with confidence {top_conf:.2f}",
                    )
                )

    @classmethod
    def _check_revenue_vs_volume_ambiguity(
        cls,
        q_lower: str,
        table_name: str,
        cols: List[ColumnSemanticMetadata],
        matched_defs: List[BusinessDefinition],
        auto_thresh: float,
        clarif_thresh: float,
        out_list: List[SemanticAmbiguity],
    ):
        """Check for revenue (monetary) vs volume (units/items count) ambiguity."""
        amount_cols = [c for c in cols if c.semantic_role == SemanticRole.AMOUNT]
        qty_cols = [c for c in cols if c.semantic_role == SemanticRole.QUANTITY]
        id_cols = [c for c in cols if c.column_name.endswith("_item_id") or c.column_name == "item_id"]

        if not amount_cols or (not qty_cols and not id_cols):
            return

        volume_terms = ["units", "items sold", "unit sales", "number of items", "volume", "item count"]
        revenue_terms = ["revenue", "sales value", "sales turnover", "total sales amount", "monetary", "dollar", "amount"]
        general_sales_terms = ["sales", "top products", "best sellers", "highest selling"]

        is_volume_explicit = any(term in q_lower for term in volume_terms)
        is_revenue_explicit = any(term in q_lower for term in revenue_terms)
        is_sales_ambiguous = any(term in q_lower for term in general_sales_terms) and not (is_volume_explicit or is_revenue_explicit)

        if is_sales_ambiguous or is_volume_explicit or is_revenue_explicit:
            amount_col = amount_cols[0]
            qty_col_name = qty_cols[0].column_name if qty_cols else (id_cols[0].column_name if id_cols else "quantity")
            qty_role = qty_cols[0].semantic_role if qty_cols else SemanticRole.QUANTITY

            rev_conf = 0.95 if is_revenue_explicit else (0.55 if is_sales_ambiguous else 0.20)
            vol_conf = 0.95 if is_volume_explicit else (0.55 if is_sales_ambiguous else 0.20)

            cand_rev = ClarificationCandidate(
                candidate_id="revenue",
                table_name=table_name,
                column_name=amount_col.column_name,
                semantic_role=amount_col.semantic_role,
                label="Total revenue generated (monetary value)",
                description=f"Sum of monetary amount ({amount_col.column_name})",
                sql_expression=f"SUM({amount_col.column_name})",
                confidence=rev_conf,
                synonyms_or_keywords=["revenue", "dollars", "sales value", "money", "amount", "monetary"],
            )

            cand_vol = ClarificationCandidate(
                candidate_id="volume",
                table_name=table_name,
                column_name=qty_col_name,
                semantic_role=qty_role,
                label="Number of units/items sold (volume)",
                description=f"Count or quantity of items sold ({qty_col_name})",
                sql_expression=f"COUNT({qty_col_name})" if "_id" in qty_col_name else f"SUM({qty_col_name})",
                confidence=vol_conf,
                synonyms_or_keywords=["volume", "units", "items", "count", "items sold", "unit sales", "quantity"],
            )

            candidates = [cand_rev, cand_vol]

            # Precedence: check if explicit business definition matches
            for bdef in matched_defs:
                for cand in candidates:
                    cand_col = cand.column_name.lower()
                    cand_qual = f"{cand.table_name.lower()}.{cand_col}"
                    is_pref = (
                        cand_col in [p.lower() for p in bdef.preferred_columns]
                        or cand_qual in [p.lower() for p in bdef.preferred_columns]
                        or any(cand_col == p.lower().split(".")[-1] for p in bdef.preferred_columns)
                    )
                    if is_pref:
                        cand.confidence = 0.98

            candidates.sort(key=lambda x: x.confidence, reverse=True)
            top_conf = candidates[0].confidence
            gap = top_conf - candidates[1].confidence if len(candidates) > 1 else top_conf

            needs_clarification = is_sales_ambiguous and (top_conf < auto_thresh and gap < 0.15)

            out_list.append(
                SemanticAmbiguity(
                    ambiguity_id=str(uuid.uuid4())[:8],
                    ambiguity_type=AmbiguityType.REVENUE_VS_VOLUME,
                    concept="sales_metric",
                    question="Do you mean total revenue or number of units/items sold?",
                    candidates=candidates,
                    recommended_candidate_id=candidates[0].candidate_id,
                    confidence_gap=round(gap, 3),
                    top_confidence=round(top_conf, 3),
                    requires_clarification=needs_clarification,
                    reason="Sales query does not distinguish monetary revenue from unit volume"
                    if needs_clarification
                    else f"Metric resolved with confidence {top_conf:.2f}",
                )
            )

    @classmethod
    def _check_status_vs_timestamp_ambiguity(
        cls,
        q_lower: str,
        table_name: str,
        cols: List[ColumnSemanticMetadata],
        matched_defs: List[BusinessDefinition],
        auto_thresh: float,
        clarif_thresh: float,
        out_list: List[SemanticAmbiguity],
    ):
        """Check for status vs timestamp ambiguity (e.g. order_status='delivered' vs timestamp IS NOT NULL)."""
        status_cols = [c for c in cols if c.semantic_role == SemanticRole.STATUS]
        time_cols = [c for c in cols if c.semantic_role in (SemanticRole.TIMESTAMP, SemanticRole.DATE)]

        if not status_cols or not time_cols:
            return

        delivery_terms = ["delivered", "delivery"]
        if any(term in q_lower for term in delivery_terms):
            matching_status = [
                c for c in status_cols
                if any("deliver" in v.lower() for v in c.sample_values) or "status" in c.column_name.lower()
            ]
            matching_time = [
                c for c in time_cols
                if any(t in c.column_name.lower() for t in delivery_terms)
            ]
            if matching_status and matching_time:
                st_col = matching_status[0]
                tm_col = matching_time[0]

                has_delivered_sample = any("deliver" in v.lower() for v in st_col.sample_values)
                asks_for_date = any(w in q_lower for w in ["date", "time", "when", "duration", "day", "calendar", "timestamp"])

                if asks_for_date:
                    time_conf = 0.94
                    status_conf = 0.30
                elif has_delivered_sample:
                    status_conf = 0.94
                    time_conf = 0.40
                else:
                    status_conf = 0.60
                    time_conf = 0.58

                cand_status = ClarificationCandidate(
                    candidate_id="status_filter",
                    table_name=table_name,
                    column_name=st_col.column_name,
                    semantic_role=SemanticRole.STATUS,
                    label="Lifecycle status (order_status = 'delivered')",
                    description=f"Filters orders by status code in `{st_col.column_name}`",
                    sample_values=st_col.sample_values,
                    confidence=status_conf,
                    synonyms_or_keywords=["status", "state", "lifecycle", "order status"],
                )
                cand_time = ClarificationCandidate(
                    candidate_id="timestamp_presence",
                    table_name=table_name,
                    column_name=tm_col.column_name,
                    semantic_role=tm_col.semantic_role,
                    label=f"Delivery timestamp (`{tm_col.column_name}` IS NOT NULL)",
                    description=f"Checks whether delivery date has been recorded in `{tm_col.column_name}`",
                    sample_values=tm_col.sample_values,
                    confidence=time_conf,
                    synonyms_or_keywords=["timestamp", "date", "recorded", "delivered date", "delivery time"],
                )

                candidates = [cand_status, cand_time]

                # Precedence: explicit business definitions
                for bdef in matched_defs:
                    for cand in candidates:
                        cand_col = cand.column_name.lower()
                        cand_qual = f"{cand.table_name.lower()}.{cand_col}"
                        is_pref = (
                            cand_col in [p.lower() for p in bdef.preferred_columns]
                            or cand_qual in [p.lower() for p in bdef.preferred_columns]
                            or any(cand_col == p.lower().split(".")[-1] for p in bdef.preferred_columns)
                        )
                        if is_pref:
                            cand.confidence = 0.98

                candidates.sort(key=lambda x: x.confidence, reverse=True)
                top_conf = candidates[0].confidence
                gap = top_conf - candidates[1].confidence if len(candidates) > 1 else top_conf

                needs_clarification = (top_conf < auto_thresh and gap < 0.15) or (top_conf < clarif_thresh)

                out_list.append(
                    SemanticAmbiguity(
                        ambiguity_id=str(uuid.uuid4())[:8],
                        ambiguity_type=AmbiguityType.STATUS_VS_TIMESTAMP,
                        concept="delivered_condition",
                        question="Which definition of delivered orders should I use?",
                        candidates=candidates,
                        recommended_candidate_id=candidates[0].candidate_id,
                        confidence_gap=round(gap, 3),
                        top_confidence=round(top_conf, 3),
                        requires_clarification=needs_clarification,
                        reason="Status vs timestamp ambiguity detected"
                        if needs_clarification
                        else f"Dominant interpretation identified with confidence {top_conf:.2f}",
                    )
                )

    @classmethod
    def _check_multiple_candidate_columns_ambiguity(
        cls,
        q_lower: str,
        table_name: str,
        cols: List[ColumnSemanticMetadata],
        matched_defs: List[BusinessDefinition],
        auto_thresh: float,
        clarif_thresh: float,
        out_list: List[SemanticAmbiguity],
    ):
        """Check for general competing columns (e.g. status vs is_active for 'active customers')."""
        active_terms = ["active customer", "active user", "active member"]
        if any(term in q_lower for term in active_terms):
            status_cols = [c for c in cols if c.semantic_role == SemanticRole.STATUS]
            bool_cols = [c for c in cols if c.semantic_role == SemanticRole.BOOLEAN and "active" in c.column_name.lower()]

            if status_cols and bool_cols:
                cand_stat = ClarificationCandidate(
                    candidate_id="status_active",
                    table_name=table_name,
                    column_name=status_cols[0].column_name,
                    semantic_role=SemanticRole.STATUS,
                    label=f"Customers with {status_cols[0].column_name} = 'active'",
                    description="Filters by status column",
                    confidence=0.60,
                    synonyms_or_keywords=["status", "active status"],
                )
                cand_bool = ClarificationCandidate(
                    candidate_id="boolean_active",
                    table_name=table_name,
                    column_name=bool_cols[0].column_name,
                    semantic_role=SemanticRole.BOOLEAN,
                    label=f"Customers where {bool_cols[0].column_name} is true",
                    description="Filters by boolean active flag",
                    confidence=0.60,
                    synonyms_or_keywords=["flag", "boolean", "is active", "active flag"],
                )
                candidates = [cand_stat, cand_bool]

                for bdef in matched_defs:
                    for cand in candidates:
                        if cand.column_name in bdef.preferred_columns:
                            cand.confidence = 0.98

                candidates.sort(key=lambda x: x.confidence, reverse=True)
                top_conf = candidates[0].confidence
                gap = top_conf - candidates[1].confidence
                needs_clarification = (top_conf < auto_thresh and gap < 0.15) or (top_conf < clarif_thresh)

                out_list.append(
                    SemanticAmbiguity(
                        ambiguity_id=str(uuid.uuid4())[:8],
                        ambiguity_type=AmbiguityType.MULTIPLE_CANDIDATE_COLUMNS,
                        concept="active_entity",
                        question="Which definition of active customers should I use?",
                        candidates=candidates,
                        recommended_candidate_id=candidates[0].candidate_id,
                        confidence_gap=round(gap, 3),
                        top_confidence=round(top_conf, 3),
                        requires_clarification=needs_clarification,
                        reason="Multiple active candidate columns exist" if needs_clarification else "Resolved",
                    )
                )

    # ── Clarification Response Resolver (Part 7 & Part 10 Safety) ──

    @classmethod
    def resolve_clarification(
        cls,
        user_response: str,
        ambiguity: SemanticAmbiguity,
    ) -> Optional[ClarificationCandidate]:
        """
        Deterministically resolve user response against known semantic candidates.
        Never injects user input directly into SQL (SQL injection safe).
        Returns matching ClarificationCandidate, or None if response cannot be mapped safely.
        """
        if not user_response or not ambiguity or not ambiguity.candidates:
            return None

        resp = user_response.strip().lower()

        # 1. Numeric index matching ("1", "2", "option 1", "#1", "first", "second")
        num_match = re.search(r"\b(\d+)\b", resp)
        if num_match:
            idx = int(num_match.group(1)) - 1
            if 0 <= idx < len(ambiguity.candidates):
                logger.info(
                    "clarification_resolved_by_index",
                    index=idx + 1,
                    candidate=ambiguity.candidates[idx].candidate_id,
                )
                return ambiguity.candidates[idx]

        ordinal_map = {
            "first": 0,
            "1st": 0,
            "one": 0,
            "second": 1,
            "2nd": 1,
            "two": 1,
            "third": 2,
            "3rd": 2,
            "three": 2,
        }
        for ord_word, idx in ordinal_map.items():
            if re.search(rf"\b{ord_word}\b", resp):
                if 0 <= idx < len(ambiguity.candidates):
                    return ambiguity.candidates[idx]

        # 2. Exact candidate ID or column name match
        for cand in ambiguity.candidates:
            if resp == cand.candidate_id.lower() or resp == cand.column_name.lower():
                return cand

        # 3. Keyword / synonym matching against candidate definitions
        scored_candidates: List[Tuple[ClarificationCandidate, int]] = []
        for cand in ambiguity.candidates:
            score = 0
            label_lower = cand.label.lower()
            col_lower = cand.column_name.lower()

            # Match label substring
            if resp in label_lower or label_lower in resp:
                score += 5

            # Match column name words
            for part in col_lower.split("_"):
                if len(part) > 2 and re.search(rf"\b{re.escape(part)}\b", resp):
                    score += 3

            # Match synonyms / keywords
            for kw in cand.synonyms_or_keywords:
                if re.search(rf"\b{re.escape(kw.lower())}\b", resp):
                    score += 4

            if score > 0:
                scored_candidates.append((cand, score))

        if scored_candidates:
            scored_candidates.sort(key=lambda x: x[1], reverse=True)
            top_cand, top_score = scored_candidates[0]
            if len(scored_candidates) == 1 or top_score > scored_candidates[1][1]:
                logger.info(
                    "clarification_resolved_by_keyword",
                    candidate=top_cand.candidate_id,
                    score=top_score,
                )
                return top_cand

        logger.warning("clarification_resolution_failed", response_preview=resp[:40])
        return None

    @classmethod
    def apply_clarification(
        cls,
        clarification_candidate: ClarificationCandidate,
        existing_semantic_context: str = "",
    ) -> str:
        """
        Generate an unambiguous semantic guidance block from the resolved clarification
        to be injected into SQL generation.
        """
        guidance_line = (
            f"  * USER CLARIFIED PREFERENCE: Use `{clarification_candidate.table_name}`.`{clarification_candidate.column_name}` "
            f"({clarification_candidate.semantic_role.value}) for '{clarification_candidate.label}'. "
            f"This interpretation was explicitly confirmed by the user."
        )
        if clarification_candidate.sql_expression:
            guidance_line += f" Formula/Expression: {clarification_candidate.sql_expression}"

        header = "-- RESOLVED BUSINESS SEMANTIC CLARIFICATION --\n"
        if existing_semantic_context:
            return header + guidance_line + "\n\n" + existing_semantic_context
        return header + guidance_line

    @classmethod
    def format_user_clarification_message(
        cls,
        ambiguity: SemanticAmbiguity,
    ) -> str:
        """Format a clean, user-friendly clarification prompt without exposing internal details."""
        lines = [ambiguity.question + "\n"]
        for i, cand in enumerate(ambiguity.candidates, 1):
            lines.append(f"{i}. {cand.label}")
        return "\n".join(lines)

import os
import re
import threading
import logging
from typing import Dict, List, Optional, Any

try:
    from app.semantics.models import (
        ColumnSemanticMetadata,
        BusinessDefinition,
        SemanticAmbiguity,
        SemanticAssumption,
        ClarificationCandidate,
        DefinitionStatus,
        BusinessKnowledgeSource,
    )
    from app.semantics.analyzer import SemanticSchemaAnalyzer
    from app.semantics.disambiguator import SemanticDisambiguator
    from app.semantics.ambiguity_detector import AmbiguityDetector
    from app.semantics.loader import BusinessKnowledgeLoader
except ImportError:
    from backend.app.semantics.models import (
        ColumnSemanticMetadata,
        BusinessDefinition,
        SemanticAmbiguity,
        SemanticAssumption,
        ClarificationCandidate,
        DefinitionStatus,
        BusinessKnowledgeSource,
    )
    from backend.app.semantics.analyzer import SemanticSchemaAnalyzer
    from backend.app.semantics.disambiguator import SemanticDisambiguator
    from backend.app.semantics.ambiguity_detector import AmbiguityDetector
    from backend.app.semantics.loader import BusinessKnowledgeLoader


logger = logging.getLogger(__name__)

_registry_instance: Optional["SemanticRegistry"] = None
_instance_lock = threading.Lock()


class SemanticRegistry:
    """Thread-safe registry for database semantic metadata and business definitions.
    Strictly partitions metadata by db_id.
    """

    def __init__(self, glossary_file: Optional[str] = None, auto_load_glossary: bool = False):
        # db_id -> {table_name: [ColumnSemanticMetadata]}
        self._columns_cache: Dict[str, Dict[str, List[ColumnSemanticMetadata]]] = {}
        # db_id -> [BusinessDefinition]
        self._definitions: Dict[str, List[BusinessDefinition]] = {}
        # cache_key -> BusinessDefinition
        self._knowledge_cache: Dict[str, Any] = {}
        self._lock = threading.Lock()
        if auto_load_glossary or glossary_file:
            self.load_glossary(glossary_file)

    def load_glossary(self, file_path: Optional[str] = None):
        """Load external business glossary from YAML or JSON file."""
        if not file_path:
            # Default to backend/app/semantics/business_glossary.yaml
            file_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "business_glossary.yaml",
            )

        if os.path.exists(file_path):
            loaded = BusinessKnowledgeLoader.load_from_file(file_path)
            with self._lock:
                for db_id, defs in loaded.items():
                    existing = self._definitions.setdefault(db_id, [])
                    # Append unique terms
                    existing_terms = {d.term.lower().strip() for d in existing}
                    for d in defs:
                        if d.term.lower().strip() not in existing_terms:
                            existing.append(d)
                            existing_terms.add(d.term.lower().strip())
            logger.info("business_glossary_loaded", file_path=file_path, databases=list(loaded.keys()))

    def clear(self, db_id: Optional[str] = None):
        """Clear cache for a specific db_id or all databases."""
        with self._lock:
            if db_id:
                self._columns_cache.pop(db_id, None)
                self._definitions.pop(db_id, None)
                keys_to_del = [k for k in self._knowledge_cache if k.startswith(f"business_definition:{db_id}:")]
                for k in keys_to_del:
                    self._knowledge_cache.pop(k, None)
            else:
                self._columns_cache.clear()
                self._definitions.clear()
                self._knowledge_cache.clear()

    def register_business_definition(self, definition: BusinessDefinition):
        """Register an explicit business definition under a specific db_id or 'global'."""
        db_key = definition.database_id or "global"
        with self._lock:
            existing = self._definitions.setdefault(db_key, [])
            # Replace if term already exists
            term_key = definition.term.lower().strip()
            idx = next((i for i, d in enumerate(existing) if d.term.lower().strip() == term_key), None)
            if idx is not None:
                existing[idx] = definition
            else:
                existing.append(definition)

            # Invalidate cached entry
            cache_key = f"business_definition:{db_key}:{term_key}"
            self._knowledge_cache.pop(cache_key, None)

    def get_business_definitions(
        self,
        db_id: str,
        pool: Optional[Any] = None,
    ) -> List[BusinessDefinition]:
        """Get business definitions applicable to db_id (including global definitions)."""
        with self._lock:
            defs = list(self._definitions.get(db_id, []))
            if db_id != "global":
                defs.extend(self._definitions.get("global", []))

        # Validate against database schema if pool is available
        validated_defs = []
        for d in defs:
            if pool:
                status = BusinessKnowledgeLoader.validate_definition(d, db_pool=pool)
                d.status = status.value
            validated_defs.append(d)

        # Sort by priority descending (enterprise_glossary 100 > user_confirmed 50)
        validated_defs.sort(key=lambda x: getattr(x, "priority", 100), reverse=True)
        return validated_defs

    def register_column_metadata(self, metadata: ColumnSemanticMetadata):
        """Explicitly register metadata for a column."""
        with self._lock:
            db_tables = self._columns_cache.setdefault(metadata.database_id, {})
            cols = db_tables.setdefault(metadata.table_name, [])
            # Replace if already present
            existing_idx = next((i for i, c in enumerate(cols) if c.column_name == metadata.column_name), None)
            if existing_idx is not None:
                cols[existing_idx] = metadata
            else:
                cols.append(metadata)

    def get_table_metadata(
        self,
        db_id: str,
        table_name: str,
        pool: Optional[Any] = None,
    ) -> List[ColumnSemanticMetadata]:
        """Fetch column semantic metadata for a table with db_id isolation."""
        with self._lock:
            cached_tables = self._columns_cache.get(db_id, {})
            if table_name in cached_tables:
                return cached_tables[table_name]

        # Not cached: analyze on the fly and cache
        columns_meta: List[ColumnSemanticMetadata] = []
        db_path = getattr(pool, "db_path", None) if pool else None

        # If pool not passed, attempt to resolve from DatabaseRegistry
        if pool is None:
            try:
                from app.db.registry import get_database_registry
                reg = get_database_registry()
                if reg.has_database(db_id):
                    pool = reg.get_pool(db_id)
                    db_path = getattr(pool, "db_path", None)
            except Exception:
                pool = None

        if pool and hasattr(pool, "get_table_schema"):
            try:
                analyzer = SemanticSchemaAnalyzer(db_path=db_path)
                schema_cols = pool.get_table_schema(table_name)
                for col in schema_cols:
                    col_name = col.get("name")
                    col_type = col.get("type", "")
                    meta = analyzer.analyze_column(
                        database_id=db_id,
                        table_name=table_name,
                        column_name=col_name,
                        data_type=col_type,
                    )
                    columns_meta.append(meta)
            except Exception as e:
                logger.warning("semantic_analysis_failed", db_id=db_id, table=table_name, error=str(e))

        with self._lock:
            self._columns_cache.setdefault(db_id, {})[table_name] = columns_meta

        return columns_meta

    def get_semantic_context(
        self,
        query: str,
        db_id: str,
        relevant_tables: List[str],
        pool: Optional[Any] = None,
        retrieved_business_defs: Optional[List[BusinessDefinition]] = None,
    ) -> str:
        """Resolve semantic disambiguation and guidance for query across relevant tables."""
        all_metadata: List[ColumnSemanticMetadata] = []
        for tbl in relevant_tables:
            tbl_meta = self.get_table_metadata(db_id, tbl, pool=pool)
            all_metadata.extend(tbl_meta)

        context_block = SemanticDisambiguator.generate_semantic_context_block(query, all_metadata)

        # Check explicit and retrieved business definitions
        defs = self.get_business_definitions(db_id, pool=pool)
        q_lower = query.lower()

        # Combine loaded definitions with any explicitly passed retrieved definitions
        combined_defs = list(defs)
        if retrieved_business_defs:
            existing_terms = {d.term.lower().strip() for d in combined_defs}
            for rd in retrieved_business_defs:
                # Enforce db_id match
                if rd.database_id == db_id and rd.term.lower().strip() not in existing_terms:
                    combined_defs.append(rd)
                    existing_terms.add(rd.term.lower().strip())

        matched_defs: List[BusinessDefinition] = []
        for d in combined_defs:
            if not getattr(d, "active", True):
                continue
            d_term = d.term.lower().strip()
            # Match term, plural/inflection, or synonyms in query
            is_match = (
                d_term in q_lower
                or (d_term.endswith("s") and d_term[:-1] in q_lower)
                or f"{d_term}s" in q_lower
                or any(syn.lower().strip() in q_lower for syn in getattr(d, "synonyms", []))
            )
            if is_match:
                matched_defs.append(d)

        # Sort by priority descending (enterprise_glossary 100 > user_confirmed 50)
        matched_defs.sort(key=lambda x: getattr(x, "priority", 100), reverse=True)

        if matched_defs:
            def_lines = [
                "\n-- BUSINESS KNOWLEDGE & GLOSSARY (PRIORITY GUIDANCE) --",
                "Note: Business guidance is semantic context, not executable SQL. The SQL generator must verify against the active schema.\n"
            ]
            for d in matched_defs:
                if d.status == DefinitionStatus.STALE.value:
                    def_lines.append(
                        f"  * Business Concept '{d.term}': Notice - Referenced columns are not available in current database schema. Defaulting to general schema reasoning."
                    )
                else:
                    line = f"  * Concept '{d.term}': {d.meaning}"
                    if d.preferred_columns:
                        line += f" [Preferred Column(s): {', '.join(d.preferred_columns)}]"
                    if d.formula_or_hint:
                        line += f" [Formula Hint: {d.formula_or_hint}]"
                    line += f" (Priority: {getattr(d, 'priority', 100)}, Source: {getattr(d, 'source', 'glossary')})"
                    def_lines.append(line)

            if context_block:
                context_block += "\n" + "\n".join(def_lines)
            else:
                context_block = "\n".join(def_lines)

        return context_block

    def detect_ambiguities(
        self,
        query: str,
        db_id: str,
        relevant_tables: List[str],
        pool: Optional[Any] = None,
        auto_execute_threshold: Optional[float] = None,
        clarification_threshold: Optional[float] = None,
    ) -> List[SemanticAmbiguity]:
        """Detect semantic ambiguities for query across relevant tables with db_id isolation."""
        all_metadata: List[ColumnSemanticMetadata] = []
        for tbl in relevant_tables:
            tbl_meta = self.get_table_metadata(db_id, tbl, pool=pool)
            all_metadata.extend(tbl_meta)

        # Only pass non-stale definitions so stale definitions don't falsely override ambiguity
        bdefs = self.get_business_definitions(db_id, pool=pool)
        active_bdefs = [d for d in bdefs if getattr(d, "status", DefinitionStatus.VALID.value) != DefinitionStatus.STALE.value]
        return AmbiguityDetector.detect_ambiguities(
            query=query,
            columns_metadata=all_metadata,
            business_definitions=active_bdefs,
            auto_execute_threshold=auto_execute_threshold,
            clarification_threshold=clarification_threshold,
        )

    def resolve_clarification(
        self,
        user_response: str,
        ambiguity: SemanticAmbiguity,
    ) -> Optional[ClarificationCandidate]:
        """Resolve user response against ambiguous candidate options."""
        return AmbiguityDetector.resolve_clarification(user_response, ambiguity)

    def apply_clarification(
        self,
        candidate: ClarificationCandidate,
        existing_semantic_context: str = "",
    ) -> str:
        """Apply resolved clarification into semantic context block."""
        return AmbiguityDetector.apply_clarification(candidate, existing_semantic_context)



def get_semantic_registry() -> SemanticRegistry:
    """Get or create singleton SemanticRegistry instance."""
    global _registry_instance
    if _registry_instance is None:
        with _instance_lock:
            if _registry_instance is None:
                _registry_instance = SemanticRegistry(auto_load_glossary=True)
    return _registry_instance

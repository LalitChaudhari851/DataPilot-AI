"""
Business Knowledge Loader & Validator for DataPilot.
Loads, parses, validates, and normalizes enterprise business glossaries from YAML and JSON.
Ensures strict db_id isolation and verifies referenced schema objects against database pools.
"""

import os
import re
import json
import structlog
from typing import Dict, List, Optional, Any, Tuple
import yaml

from app.semantics.models import (
    BusinessDefinition,
    DefinitionStatus,
    BusinessKnowledgeSource,
)

logger = structlog.get_logger()

# Dangerous SQL keywords that must never appear in business formulas/hints
FORBIDDEN_SQL_KEYWORDS = re.compile(
    r"\b(DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE|EXEC|EXECUTE|CREATE|GRANT|REVOKE|MERGE)\b",
    re.IGNORECASE,
)


class BusinessKnowledgeLoader:
    """Production loader and validator for external business definitions."""

    @staticmethod
    def normalize_term(term: str) -> str:
        """Normalize a business term for consistent lookup."""
        if not term:
            return ""
        # Lowercase, replace hyphens and underscores with spaces or clean tokens
        return term.strip().lower()

    @staticmethod
    def validate_formula_safety(formula: Optional[str]) -> Tuple[bool, str]:
        """Validate that a formula/hint does not contain destructive SQL statements."""
        if not formula:
            return True, ""
        
        # Semicolons can be used for statement chaining
        if ";" in formula:
            return False, "Formula contains disallowed statement terminator ';'"

        # Destructive DDL / DML
        match = FORBIDDEN_SQL_KEYWORDS.search(formula)
        if match:
            return False, f"Formula contains forbidden SQL keyword: '{match.group(1)}'"

        return True, ""

    @classmethod
    def validate_definition(
        cls,
        definition: BusinessDefinition,
        db_pool: Optional[Any] = None,
    ) -> DefinitionStatus:
        """
        Validate a BusinessDefinition against safety rules and database schema.
        Returns DefinitionStatus: VALID, INVALID, STALE, or UNRESOLVED.
        """
        # 1. Term check
        if not definition.term or not definition.term.strip():
            logger.warning("invalid_business_definition_empty_term")
            return DefinitionStatus.INVALID

        # 2. Formula safety check
        safe, err = cls.validate_formula_safety(definition.formula_or_hint)
        if not safe:
            logger.warning(
                "invalid_business_definition_unsafe_formula",
                term=definition.term,
                error=err,
            )
            return DefinitionStatus.INVALID

        # 3. Confidence range check
        if definition.confidence < 0.0 or definition.confidence > 1.0:
            return DefinitionStatus.INVALID

        # 4. Schema verification if database pool is available
        if db_pool and hasattr(db_pool, "get_tables") and hasattr(db_pool, "get_table_schema"):
            try:
                tables = set(db_pool.get_tables())
                if not tables:
                    return DefinitionStatus.UNRESOLVED

                if not definition.preferred_columns:
                    return DefinitionStatus.VALID

                # Check all preferred columns
                all_found = True
                any_found = False

                for col_ref in definition.preferred_columns:
                    parts = col_ref.split(".")
                    if len(parts) == 2:
                        tbl, col = parts[0].strip(), parts[1].strip()
                        # Case-insensitive table match
                        matched_tbl = next((t for t in tables if t.lower() == tbl.lower()), None)
                        if not matched_tbl:
                            all_found = False
                            continue
                        cols_meta = db_pool.get_table_schema(matched_tbl)
                        col_names = {c.get("name", "").lower() for c in cols_meta}
                        if col.lower() in col_names:
                            any_found = True
                        else:
                            all_found = False
                    elif len(parts) == 1:
                        # Naked column name: check if exists in any known table
                        col = parts[0].strip().lower()
                        found = False
                        for t in tables:
                            cols_meta = db_pool.get_table_schema(t)
                            if any(c.get("name", "").lower() == col for c in cols_meta):
                                found = True
                                break
                        if found:
                            any_found = True
                        else:
                            all_found = False

                if all_found:
                    return DefinitionStatus.VALID
                elif any_found:
                    # Partially stale or partially matching
                    return DefinitionStatus.STALE
                else:
                    # Completely missing referenced columns in current schema
                    return DefinitionStatus.STALE

            except Exception as e:
                logger.warning("definition_schema_check_failed", error=str(e), term=definition.term)
                return DefinitionStatus.UNRESOLVED

        return DefinitionStatus.UNRESOLVED

    @classmethod
    def load_from_dict(
        cls,
        data: Dict[str, Any],
        source: str = BusinessKnowledgeSource.ENTERPRISE_GLOSSARY.value,
        default_db_id: Optional[str] = None,
    ) -> Dict[str, List[BusinessDefinition]]:
        """
        Parse structured dictionary into database-partitioned BusinessDefinitions.
        Expected format:
        databases:
          db_id_1:
            definitions: [...]
        """
        result: Dict[str, List[BusinessDefinition]] = {}
        databases_map = data.get("databases", {})

        # If data is directly a list of definitions under a default or specified db
        if not databases_map and "definitions" in data:
            databases_map = {default_db_id or "global": data}

        for db_id, db_content in databases_map.items():
            if not isinstance(db_content, dict):
                continue
            raw_defs = db_content.get("definitions", [])
            db_defs = []

            for raw in raw_defs:
                if not isinstance(raw, dict):
                    continue
                try:
                    term = raw.get("term", "").strip()
                    if not term:
                        continue
                    
                    bdef = BusinessDefinition(
                        term=term,
                        meaning=raw.get("meaning", "").strip(),
                        database_id=db_id,
                        preferred_columns=raw.get("preferred_columns", []),
                        formula_or_hint=raw.get("formula_or_hint"),
                        synonyms=[s.strip() for s in raw.get("synonyms", []) if s.strip()],
                        examples=[e.strip() for e in raw.get("examples", []) if e.strip()],
                        related_terms=[r.strip() for r in raw.get("related_terms", []) if r.strip()],
                        source=raw.get("source", source),
                        priority=int(raw.get("priority", 100)),
                        confidence=float(raw.get("confidence", 1.0)),
                        active=bool(raw.get("active", True)),
                        status=DefinitionStatus.VALID.value,
                    )
                    # Initial validation
                    status = cls.validate_definition(bdef)
                    if status != DefinitionStatus.INVALID:
                        bdef.status = status.value
                        db_defs.append(bdef)
                    else:
                        logger.warning("rejected_invalid_definition", term=term, db_id=db_id)
                except Exception as e:
                    logger.warning("failed_to_parse_definition", error=str(e), raw=raw)

            if db_defs:
                result.setdefault(db_id, []).extend(db_defs)

        return result

    @classmethod
    def load_from_file(
        cls,
        file_path: str,
        source: str = BusinessKnowledgeSource.ENTERPRISE_GLOSSARY.value,
    ) -> Dict[str, List[BusinessDefinition]]:
        """Load business definitions from YAML or JSON file."""
        if not os.path.exists(file_path):
            logger.warning("business_glossary_file_not_found", file_path=file_path)
            return {}

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            if file_path.endswith((".yaml", ".yml")):
                data = yaml.safe_load(content) or {}
            elif file_path.endswith(".json"):
                data = json.loads(content) or {}
            else:
                try:
                    data = yaml.safe_load(content) or {}
                except Exception:
                    data = json.loads(content) or {}

            return cls.load_from_dict(data, source=source)

        except Exception as e:
            logger.error("failed_to_load_business_glossary", file_path=file_path, error=str(e))
            return {}

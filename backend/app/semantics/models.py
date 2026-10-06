"""
Semantic Models for PlainSQL Business Semantic Layer.
"""

from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class SemanticRole(str, Enum):
    """Semantic roles for database columns."""
    IDENTIFIER = "identifier"
    FOREIGN_KEY = "foreign_key"
    NAME = "name"
    DATE = "date"
    TIMESTAMP = "timestamp"
    STATUS = "status"
    AMOUNT = "amount"
    QUANTITY = "quantity"
    PERCENTAGE = "percentage"
    CATEGORY = "category"
    BOOLEAN = "boolean"
    METRIC = "metric"
    DIMENSION = "dimension"
    DESCRIPTIVE_TEXT = "descriptive_text"


class ColumnSemanticMetadata(BaseModel):
    """Lightweight business semantic representation for a schema field."""
    database_id: str
    table_name: str
    column_name: str
    data_type: str
    semantic_role: SemanticRole
    description: Optional[str] = ""
    business_meaning: Optional[str] = ""
    synonyms: List[str] = Field(default_factory=list)
    sample_values: List[str] = Field(default_factory=list)
    related_columns: List[str] = Field(default_factory=list)
    confidence: float = 1.0


class DefinitionStatus(str, Enum):
    """Validation and lifecycle status for business definitions."""
    VALID = "VALID"
    INVALID = "INVALID"
    STALE = "STALE"
    UNRESOLVED = "UNRESOLVED"


class BusinessKnowledgeSource(str, Enum):
    """Origin source of business definitions."""
    ENTERPRISE_GLOSSARY = "enterprise_glossary"
    USER_CONFIRMED = "user_confirmed"
    IMPORTED_DEFINITION = "imported_definition"
    INFERRED = "inferred"
    SYSTEM = "system"


class BusinessDefinition(BaseModel):
    """Explicit business concept definition with database scope and validation metadata."""
    term: str
    meaning: str
    database_id: Optional[str] = None
    preferred_columns: List[str] = Field(default_factory=list)
    formula_or_hint: Optional[str] = None
    synonyms: List[str] = Field(default_factory=list)
    examples: List[str] = Field(default_factory=list)
    related_terms: List[str] = Field(default_factory=list)
    source: str = "enterprise_glossary"
    priority: int = 100
    confidence: float = 1.0
    active: bool = True
    status: str = "VALID"


class SemanticLearningEvent(BaseModel):
    """Record of a user-confirmed disambiguation event."""
    event_id: Optional[str] = None
    database_id: str
    original_term: str
    selected_candidate: str
    rejected_candidates: List[str] = Field(default_factory=list)
    user_confirmation: str
    timestamp: float
    confidence: float = 1.0
    source: str = "user_clarification"


class ProposedBusinessDefinition(BaseModel):
    """Candidate business definition awaiting or eligible for promotion."""
    proposal_id: Optional[str] = None
    database_id: str
    term: str
    meaning: str
    preferred_columns: List[str] = Field(default_factory=list)
    formula_or_hint: Optional[str] = None
    confirmation_count: int = 1
    confidence: float = 1.0
    source: str = "user_confirmed"
    status: str = "PROPOSED"  # PROPOSED | APPROVED | REJECTED
    created_at: float = 0.0
    updated_at: float = 0.0


class DisambiguationResult(BaseModel):
    """Result of disambiguating between competing columns for a concept."""
    concept: str
    selected_column: str
    table_name: str
    semantic_role: SemanticRole
    reason: str
    confidence: float
    alternatives_considered: List[str] = Field(default_factory=list)


class AmbiguityType(str, Enum):
    """Types of semantic ambiguity detected in user queries."""
    MULTIPLE_TIMESTAMP = "multiple_timestamp"
    STATUS_VS_TIMESTAMP = "status_vs_timestamp"
    MULTIPLE_METRICS = "multiple_metrics"
    REVENUE_VS_VOLUME = "revenue_vs_volume"
    MULTIPLE_CANDIDATE_COLUMNS = "multiple_candidate_columns"
    BUSINESS_TERM_AMBIGUITY = "business_term_ambiguity"


class ClarificationCandidate(BaseModel):
    """A concrete candidate option for disambiguating a query."""
    candidate_id: str
    table_name: str
    column_name: str
    semantic_role: SemanticRole
    label: str
    description: str = ""
    sample_values: List[str] = Field(default_factory=list)
    sql_expression: Optional[str] = None
    confidence: float = 0.5
    synonyms_or_keywords: List[str] = Field(default_factory=list)


class SemanticAmbiguity(BaseModel):
    """Structured representation of a semantic collision/ambiguity."""
    ambiguity_id: str
    ambiguity_type: AmbiguityType
    concept: str
    question: str
    candidates: List[ClarificationCandidate]
    recommended_candidate_id: Optional[str] = None
    confidence_gap: float = 0.0
    top_confidence: float = 0.0
    requires_clarification: bool = False
    reason: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ClarificationState(BaseModel):
    """Interactive state for a paused clarification loop."""
    clarification_id: str
    ambiguity: SemanticAmbiguity
    original_query: str
    db_id: Optional[str] = None
    created_at: float
    resolved: bool = False
    selected_candidate_id: Optional[str] = None
    selected_column: Optional[str] = None


class SemanticAssumption(BaseModel):
    """Internally tracked assumption when query proceeds with medium/high confidence."""
    ambiguity_type: AmbiguityType
    concept: str
    selected_column: str
    table_name: str
    semantic_role: SemanticRole
    confidence: float
    reason: str
    alternatives_considered: List[str] = Field(default_factory=list)


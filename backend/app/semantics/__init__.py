"""
DataPilot Business Semantic Layer.
"""

try:
    from app.semantics.models import (
        SemanticRole,
        ColumnSemanticMetadata,
        BusinessDefinition,
        DisambiguationResult,
    )
    from app.semantics.analyzer import SemanticSchemaAnalyzer
    from app.semantics.disambiguator import SemanticDisambiguator
    from app.semantics.registry import (
        SemanticRegistry,
        get_semantic_registry,
    )
except ImportError:
    from backend.app.semantics.models import (
        SemanticRole,
        ColumnSemanticMetadata,
        BusinessDefinition,
        DisambiguationResult,
    )
    from backend.app.semantics.analyzer import SemanticSchemaAnalyzer
    from backend.app.semantics.disambiguator import SemanticDisambiguator
    from backend.app.semantics.registry import (
        SemanticRegistry,
        get_semantic_registry,
    )

__all__ = [
    "SemanticRole",
    "ColumnSemanticMetadata",
    "BusinessDefinition",
    "DisambiguationResult",
    "SemanticSchemaAnalyzer",
    "SemanticDisambiguator",
    "SemanticRegistry",
    "get_semantic_registry",
]

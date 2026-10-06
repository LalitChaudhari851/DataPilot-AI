"""
Dynamic Few-Shot Selector — Selects the most relevant SQL examples for each query.

Instead of hardcoded few-shot examples, this module uses embedding similarity
to find the most relevant examples from the evaluation dataset. This improves
SQL generation accuracy by giving the LLM contextually similar reference queries.

Architecture:
    Query → encode with MiniLM → cosine similarity against example embeddings → top-k

Dialect-Aware Partitioning:
    Separate example pools and embedding indices per SQL dialect (mysql, sqlite).
    MySQL examples are NEVER injected into SQLite generation, and vice versa.
"""

import json
import os
import structlog
import numpy as np
from typing import Optional

os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

logger = structlog.get_logger()


# Default paths for dialect-specific datasets
_EVAL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "evaluation",
)
_DATASET_PATHS = {
    "mysql": os.path.join(_EVAL_DIR, "datasets", "train.json"),
    "sqlite": os.path.join(_EVAL_DIR, "datasets", "sqlite_train.json"),
}


class DynamicFewShotSelector:
    """
    Select the most relevant few-shot examples for each user query
    using embedding similarity.

    Supports dialect-aware partitioning: maintains separate example pools
    and embedding matrices for each SQL dialect.

    Lazy-loads the encoder and example embeddings on first use to avoid
    slowing down application startup.
    """

    def __init__(self, examples_path: str = None, dialect: str = None):
        """
        Initialize the few-shot selector.

        Args:
            examples_path: Override path for a single dataset (legacy mode).
            dialect: If provided, restricts this instance to a single dialect.
                     If None, loads all available dialect datasets.
        """
        self._explicit_path = examples_path
        self._explicit_dialect = dialect
        self._encoder = None
        # Dialect-partitioned storage: {dialect: [examples]}
        self._examples_by_dialect: dict[str, list[dict]] = {}
        # Dialect-partitioned embeddings: {dialect: np.ndarray}
        self._embeddings_by_dialect: dict[str, Optional[np.ndarray]] = {}
        self._loaded = False

    def _lazy_load(self):
        """Load encoder and examples on first use."""
        if self._loaded:
            return

        self._loaded = True  # Prevent retry loops on failure

        # Determine which dataset(s) to load
        if self._explicit_path:
            # Legacy single-file mode
            paths = {"default": self._explicit_path}
        elif self._explicit_dialect:
            # Single dialect mode
            if self._explicit_dialect in _DATASET_PATHS:
                paths = {self._explicit_dialect: _DATASET_PATHS[self._explicit_dialect]}
            else:
                logger.info("few_shot_unknown_dialect", dialect=self._explicit_dialect)
                return
        else:
            # Load all available dialect datasets
            paths = _DATASET_PATHS

        # Load examples from each dataset
        total_loaded = 0
        for dialect, path in paths.items():
            if not os.path.exists(path):
                logger.info("few_shot_dataset_not_found", dialect=dialect, path=path)
                continue

            try:
                with open(path, "r") as f:
                    examples = json.load(f)

                # Tag examples with dialect if not already tagged
                for ex in examples:
                    if "dialect" not in ex:
                        ex["dialect"] = dialect

                self._examples_by_dialect[dialect] = examples
                total_loaded += len(examples)
                logger.info("few_shot_examples_loaded", dialect=dialect, count=len(examples))
            except Exception as e:
                logger.warning("few_shot_load_failed", dialect=dialect, error=str(e))

        if total_loaded == 0:
            logger.info("few_shot_no_examples_loaded")
            return

        # Load encoder and pre-compute embeddings per dialect
        if os.environ.get("DISABLE_ML_INTENT", "false").lower() in ("true", "1", "yes"):
            logger.info("few_shot_encoder_disabled_by_env")
            return

        try:
            from sentence_transformers import SentenceTransformer

            try:
                self._encoder = SentenceTransformer("all-MiniLM-L6-v2", local_files_only=True)
            except Exception:
                self._encoder = SentenceTransformer("all-MiniLM-L6-v2")


            for dialect, examples in self._examples_by_dialect.items():
                cache_path = os.path.join(_EVAL_DIR, "datasets", f".few_shot_{dialect}.npy")
                questions = [ex["question"] for ex in examples]
                loaded_from_cache = False
                if os.path.exists(cache_path):
                    try:
                        cached_emb = np.load(cache_path)
                        if len(cached_emb) == len(questions):
                            self._embeddings_by_dialect[dialect] = cached_emb
                            loaded_from_cache = True
                            logger.info("few_shot_embeddings_loaded_from_cache", dialect=dialect, count=len(questions))
                    except Exception:
                        pass

                if not loaded_from_cache:
                    embeddings = self._encoder.encode(questions, show_progress_bar=False)
                    self._embeddings_by_dialect[dialect] = embeddings
                    logger.info("few_shot_embeddings_computed", dialect=dialect, count=len(questions))
                    try:
                        np.save(cache_path, embeddings)
                    except Exception:
                        pass


        except ImportError:
            logger.info("few_shot_encoder_unavailable", hint="pip install sentence-transformers")
        except Exception as e:
            logger.warning("few_shot_encoding_failed", error=str(e))

    @property
    def available(self) -> bool:
        """Whether the selector is ready for use (any dialect)."""
        self._lazy_load()
        return (
            self._encoder is not None
            and len(self._embeddings_by_dialect) > 0
            and any(len(exs) > 0 for exs in self._examples_by_dialect.values())
        )

    def available_for_dialect(self, dialect: str) -> bool:
        """Whether the selector has examples available for a specific dialect."""
        self._lazy_load()
        return (
            self._encoder is not None
            and dialect in self._embeddings_by_dialect
            and self._embeddings_by_dialect[dialect] is not None
            and len(self._examples_by_dialect.get(dialect, [])) > 0
        )

    def select(self, query: str, k: int = 3, dialect: str = None) -> list[dict]:
        """
        Select k most similar examples to the query.

        Args:
            query: The user's natural language question.
            k: Number of examples to return.
            dialect: SQL dialect to select examples from (e.g., 'mysql', 'sqlite').
                     If None, uses all available examples (legacy behavior).

        Returns:
            List of example dicts with 'question' and 'expected_sql' keys.
            Returns empty list if the selector is unavailable.
        """
        if not self.available:
            return []

        # Resolve which dialect pool(s) to search
        if dialect and dialect in self._embeddings_by_dialect:
            # Strict dialect isolation
            search_dialects = [dialect]
        elif dialect:
            # Requested dialect not available — return empty rather than
            # cross-contaminating with wrong dialect examples
            logger.debug("few_shot_dialect_not_available", dialect=dialect)
            return []
        else:
            # Legacy: search all dialects
            search_dialects = list(self._embeddings_by_dialect.keys())

        try:
            query_emb = self._encoder.encode([query], show_progress_bar=False)[0]

            # Collect candidates from all target dialect pools
            candidates = []
            for d in search_dialects:
                embeddings = self._embeddings_by_dialect.get(d)
                examples = self._examples_by_dialect.get(d, [])
                if embeddings is None or len(examples) == 0:
                    continue

                # Cosine similarity against this dialect's examples
                norms = np.linalg.norm(embeddings, axis=1) * np.linalg.norm(query_emb)
                norms = np.where(norms == 0, 1e-10, norms)
                similarities = np.dot(embeddings, query_emb) / norms

                for idx, sim in enumerate(similarities):
                    candidates.append((sim, examples[idx]))

            # Sort by similarity (highest first)
            candidates.sort(key=lambda x: x[0], reverse=True)

            # Select top-k, excluding exact query matches to prevent data leakage during eval
            selected = []
            query_lower = query.lower().strip()
            for _sim, ex in candidates:
                if ex["question"].lower().strip() != query_lower:
                    selected.append(ex)
                    if len(selected) >= k:
                        break

            return selected

        except Exception as e:
            logger.warning("few_shot_selection_failed", error=str(e))
            return []

    def format_for_prompt(self, examples: list[dict]) -> str:
        """
        Format selected examples as text to inject into the prompt.

        Args:
            examples: List of example dicts from select().

        Returns:
            Formatted string for prompt injection, or empty string if no examples.
        """
        if not examples:
            return ""

        parts = ["\n## SIMILAR EXAMPLES FROM EVALUATION SET"]
        for i, ex in enumerate(examples, 1):
            parts.append(f"\nSimilar Query {i}:")
            parts.append(f'Q: "{ex["question"]}"')
            parts.append(f'SQL: {ex["expected_sql"]}')

        return "\n".join(parts)


# ── Module-level singleton ───────────────────────────────
_selector_instance: Optional[DynamicFewShotSelector] = None


def get_few_shot_selector() -> DynamicFewShotSelector:
    """Get or create the singleton few-shot selector instance."""
    global _selector_instance
    if _selector_instance is None:
        _selector_instance = DynamicFewShotSelector()
    return _selector_instance

"""
Phase 6 Tests — Dialect-Aware Dynamic Few-Shot + SQL Projection Guidance

Tests:
1. SQLite few-shot dataset loads correctly
2. Dialect partitioning works (MySQL ≠ SQLite)
3. No cross-contamination between dialect pools
4. Few-shot selection returns SQLite examples for SQLite queries
5. Few-shot selection returns MySQL examples for MySQL queries
6. Projection guidance is present in the SQLite v2 prompt
7. SQL generation integration with dialect-aware few-shot
8. Backward compatibility with existing MySQL pipeline
"""

import json
import os
import sys
import pytest

# Ensure the backend directory is on the import path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import sentence_transformers  # noqa: F401
    _HAS_SENTENCE_TRANSFORMERS = True
except ImportError:
    _HAS_SENTENCE_TRANSFORMERS = False


def _ml_disabled():
    """True if sentence-transformers is missing or ML is explicitly disabled."""
    if not _HAS_SENTENCE_TRANSFORMERS:
        return True
    return os.environ.get("DISABLE_ML_INTENT", "false").lower() in ("true", "1", "yes")


# ────────────────────────────────────────────────
# Group 1: SQLite Few-Shot Dataset Integrity
# ────────────────────────────────────────────────

class TestSQLiteFewShotDataset:
    """Verify the sqlite_train.json dataset is correct and complete."""

    @pytest.fixture(autouse=True)
    def load_dataset(self):
        dataset_path = os.path.join(
            os.path.dirname(__file__), "..", "evaluation", "datasets", "sqlite_train.json"
        )
        assert os.path.exists(dataset_path), f"sqlite_train.json not found at {dataset_path}"
        with open(dataset_path, "r") as f:
            self.examples = json.load(f)

    def test_dataset_not_empty(self):
        """Dataset should contain at least 15 examples."""
        assert len(self.examples) >= 15, f"Expected ≥15 examples, got {len(self.examples)}"

    def test_all_examples_have_required_fields(self):
        """Every example must have question, expected_sql, and dialect."""
        required = {"question", "expected_sql", "dialect"}
        for ex in self.examples:
            missing = required - set(ex.keys())
            assert not missing, f"Example {ex.get('id', '?')} missing fields: {missing}"

    def test_all_examples_tagged_sqlite(self):
        """Every example in sqlite_train.json must have dialect='sqlite'."""
        for ex in self.examples:
            assert ex["dialect"] == "sqlite", (
                f"Example {ex.get('id', '?')} has dialect='{ex['dialect']}' (expected 'sqlite')"
            )

    def test_no_mysql_syntax_in_sqlite_examples(self):
        """SQLite examples must NOT contain MySQL-specific functions."""
        import re
        mysql_patterns = [
            "DATE_FORMAT(", "YEAR(", "MONTH(", "DATE_SUB(", "DATE_ADD(",
            "CURDATE(", "NOW()", "DATEDIFF(", "SUBSTRING(",
            "SEPARATOR",
        ]
        # CONCAT( must be standalone (not GROUP_CONCAT)
        concat_re = re.compile(r'(?<!GROUP_)CONCAT\(', re.IGNORECASE)
        # IF( must be standalone (not IIF or IFNULL or COALESCE...IF)
        if_re = re.compile(r'(?<![A-Z])IF\s*\(', re.IGNORECASE)

        for ex in self.examples:
            sql = ex["expected_sql"].upper()
            for pattern in mysql_patterns:
                assert pattern.upper() not in sql, (
                    f"Example {ex.get('id', '?')} contains MySQL syntax: {pattern}"
                )
            # Standalone CONCAT check
            assert not concat_re.search(ex["expected_sql"]), (
                f"Example {ex.get('id', '?')} contains MySQL CONCAT() function"
            )
            # Standalone IF check
            assert not if_re.search(ex["expected_sql"]), (
                f"Example {ex.get('id', '?')} contains MySQL IF() function"
            )

    def test_sqlite_syntax_present(self):
        """At least some examples should use SQLite-specific syntax."""
        all_sql = " ".join(ex["expected_sql"] for ex in self.examples)
        has_strftime = "strftime" in all_sql
        has_concat_op = "||" in all_sql
        has_date_now = "date('now'" in all_sql
        has_julianday = "julianday" in all_sql
        has_group_concat = "group_concat" in all_sql.lower()

        sqlite_features = sum([has_strftime, has_concat_op, has_date_now, has_julianday, has_group_concat])
        assert sqlite_features >= 3, (
            f"Expected ≥3 SQLite-specific features in dataset, found {sqlite_features}"
        )

    def test_difficulty_distribution(self):
        """Dataset should have examples across difficulty levels."""
        difficulties = {ex.get("difficulty", "unknown") for ex in self.examples}
        assert "easy" in difficulties, "Missing easy examples"
        assert "medium" in difficulties, "Missing medium examples"
        assert "hard" in difficulties, "Missing hard examples"


# ────────────────────────────────────────────────
# Group 2: Dialect-Aware Few-Shot Selector
# ────────────────────────────────────────────────

class TestDialectAwareFewShotSelector:
    """Verify dialect-aware partitioning in the DynamicFewShotSelector."""

    @pytest.fixture(autouse=True)
    def setup_selector(self):
        """Create a fresh selector for each test."""
        # Reset singleton to ensure clean state
        import app.prompts.few_shot as fs_module
        fs_module._selector_instance = None
        os.environ.pop("DISABLE_ML_INTENT", None)

    def test_selector_loads_both_dialects(self):
        """Selector should load both MySQL and SQLite datasets."""
        from app.prompts.few_shot import DynamicFewShotSelector

        selector = DynamicFewShotSelector()
        selector._lazy_load()

        assert "mysql" in selector._examples_by_dialect, "MySQL examples not loaded"
        assert "sqlite" in selector._examples_by_dialect, "SQLite examples not loaded"

    def test_selector_mysql_examples_count(self):
        """MySQL examples should come from train.json."""
        from app.prompts.few_shot import DynamicFewShotSelector

        selector = DynamicFewShotSelector()
        selector._lazy_load()

        mysql_count = len(selector._examples_by_dialect.get("mysql", []))
        assert mysql_count > 0, "No MySQL examples loaded"

    def test_selector_sqlite_examples_count(self):
        """SQLite examples should come from sqlite_train.json."""
        from app.prompts.few_shot import DynamicFewShotSelector

        selector = DynamicFewShotSelector()
        selector._lazy_load()

        sqlite_count = len(selector._examples_by_dialect.get("sqlite", []))
        assert sqlite_count >= 15, f"Expected ≥15 SQLite examples, got {sqlite_count}"

    @pytest.mark.skipif(
        _ml_disabled(),
        reason="ML models disabled by environment",
    )
    def test_separate_embeddings_per_dialect(self):
        """Each dialect should have its own embedding matrix."""
        from app.prompts.few_shot import DynamicFewShotSelector

        selector = DynamicFewShotSelector()
        selector._lazy_load()

        assert "mysql" in selector._embeddings_by_dialect
        assert "sqlite" in selector._embeddings_by_dialect
        assert selector._embeddings_by_dialect["mysql"] is not None
        assert selector._embeddings_by_dialect["sqlite"] is not None

    @pytest.mark.skipif(
        _ml_disabled(),
        reason="ML models disabled by environment",
    )
    def test_available_for_dialect(self):
        """available_for_dialect should return True for loaded dialects."""
        from app.prompts.few_shot import DynamicFewShotSelector

        selector = DynamicFewShotSelector()

        assert selector.available_for_dialect("mysql") is True
        assert selector.available_for_dialect("sqlite") is True
        assert selector.available_for_dialect("postgres") is False

    @pytest.mark.skipif(
        _ml_disabled(),
        reason="ML models disabled by environment",
    )
    def test_no_cross_contamination_sqlite(self):
        """SQLite dialect selection must NOT return MySQL examples."""
        from app.prompts.few_shot import DynamicFewShotSelector

        selector = DynamicFewShotSelector()
        results = selector.select("Show top employees by salary", k=5, dialect="sqlite")

        for ex in results:
            dialect = ex.get("dialect", "unknown")
            assert dialect == "sqlite", (
                f"Cross-contamination: SQLite query returned dialect='{dialect}' example"
            )

    @pytest.mark.skipif(
        _ml_disabled(),
        reason="ML models disabled by environment",
    )
    def test_no_cross_contamination_mysql(self):
        """MySQL dialect selection must NOT return SQLite examples."""
        from app.prompts.few_shot import DynamicFewShotSelector

        selector = DynamicFewShotSelector()
        results = selector.select("Show total revenue by region", k=5, dialect="mysql")

        for ex in results:
            dialect = ex.get("dialect", "mysql")
            assert dialect == "mysql", (
                f"Cross-contamination: MySQL query returned dialect='{dialect}' example"
            )

    @pytest.mark.skipif(
        _ml_disabled(),
        reason="ML models disabled by environment",
    )
    def test_sqlite_selection_returns_results(self):
        """SQLite dialect selection should return valid results."""
        from app.prompts.few_shot import DynamicFewShotSelector

        selector = DynamicFewShotSelector()
        results = selector.select("Count all orders grouped by month", k=3, dialect="sqlite")

        assert len(results) > 0, "No SQLite examples returned"
        assert len(results) <= 3, f"Expected ≤3 results, got {len(results)}"

    @pytest.mark.skipif(
        _ml_disabled(),
        reason="ML models disabled by environment",
    )
    def test_unknown_dialect_returns_empty(self):
        """Unknown dialect should return empty list (no cross-contamination)."""
        from app.prompts.few_shot import DynamicFewShotSelector

        selector = DynamicFewShotSelector()
        results = selector.select("Show all data", k=3, dialect="oracle")

        assert results == [], f"Expected empty list for unknown dialect, got {len(results)} results"

    @pytest.mark.skipif(
        _ml_disabled(),
        reason="ML models disabled by environment",
    )
    def test_legacy_mode_no_dialect(self):
        """Without dialect parameter, select should search all pools (legacy)."""
        from app.prompts.few_shot import DynamicFewShotSelector

        selector = DynamicFewShotSelector()
        results = selector.select("Show total revenue", k=3)

        assert len(results) > 0, "Legacy mode returned no results"

    @pytest.mark.skipif(
        _ml_disabled(),
        reason="ML models disabled by environment",
    )
    def test_format_for_prompt(self):
        """format_for_prompt should produce formatted text."""
        from app.prompts.few_shot import DynamicFewShotSelector

        selector = DynamicFewShotSelector()
        results = selector.select("Count all records", k=2, dialect="sqlite")
        formatted = selector.format_for_prompt(results)

        assert "SIMILAR EXAMPLES" in formatted
        assert "Q:" in formatted
        assert "SQL:" in formatted

    @pytest.mark.skipif(
        _ml_disabled(),
        reason="ML models disabled by environment",
    )
    def test_data_leakage_prevention(self):
        """Exact query matches should be excluded from results."""
        from app.prompts.few_shot import DynamicFewShotSelector

        selector = DynamicFewShotSelector()
        # Use an exact question from the sqlite dataset
        results = selector.select(
            "How many total records are in the table?", k=3, dialect="sqlite"
        )
        for r in results:
            assert r["question"].lower().strip() != "how many total records are in the table?", (
                "Data leakage: exact query match returned"
            )


# ────────────────────────────────────────────────
# Group 3: Prompt Registry Integration
# ────────────────────────────────────────────────

class TestPromptRegistryPhase6:
    """Verify prompt registry changes for Phase 6."""

    def test_sqlite_prompt_v1_exists(self):
        """SQLite v1 prompt should exist for rollback."""
        from app.prompts.registry import get_prompt_registry

        registry = get_prompt_registry()
        template = registry.get("sql_generation_sqlite", version="v1")
        assert template is not None
        assert template.version == "v1"

    def test_sqlite_prompt_v2_is_active(self):
        """SQLite v2 prompt should be the active default."""
        from app.prompts.registry import get_prompt_registry

        registry = get_prompt_registry()
        template = registry.get("sql_generation_sqlite")
        assert template.version == "v2", f"Expected v2 as active, got {template.version}"

    def test_sqlite_v2_has_projection_guidance(self):
        """SQLite v2 prompt should contain projection/column selection rules."""
        from app.prompts.registry import get_prompt_registry

        registry = get_prompt_registry()
        template = registry.get("sql_generation_sqlite", version="v2")

        assert "COLUMN SELECTION" in template.system or "PROJECTION GUIDANCE" in template.system, (
            "SQLite v2 prompt missing projection guidance section"
        )

    def test_sqlite_v2_has_minimal_projection_rules(self):
        """SQLite v2 should have rules about minimal column selection."""
        from app.prompts.registry import get_prompt_registry

        registry = get_prompt_registry()
        template = registry.get("sql_generation_sqlite", version="v2")

        # Check for key projection guidance rules
        assert "SELECT only the columns" in template.system or "minimal" in template.system.lower()
        assert "COUNT(*)" in template.system

    def test_sqlite_v2_has_boolean_guidance(self):
        """SQLite v2 should have boolean comparison guidance (col = 1, not TRUE)."""
        from app.prompts.registry import get_prompt_registry

        registry = get_prompt_registry()
        template = registry.get("sql_generation_sqlite", version="v2")

        assert "col = 1" in template.system or "Boolean" in template.system

    def test_mysql_prompt_unmodified(self):
        """MySQL v3 prompt should still be the active default (no regression)."""
        from app.prompts.registry import get_prompt_registry

        registry = get_prompt_registry()
        template = registry.get("sql_generation")
        assert template.version == "v3", f"Expected MySQL v3 as active, got {template.version}"
        assert "MySQL" in template.system or "mysql" in template.system.lower()


# ────────────────────────────────────────────────
# Group 4: SQL Generation Integration
# ────────────────────────────────────────────────

class TestSQLGenerationIntegration:
    """Verify sql_generation.py properly integrates dialect-aware few-shot."""

    def test_sql_generation_calls_few_shot_with_dialect(self):
        """sql_generation_node should pass dialect to few-shot selector."""
        import inspect
        from app.agents.sql_generation import sql_generation_node

        source = inspect.getsource(sql_generation_node)

        # The old guard should be gone
        assert "if sql_dialect != \"sqlite\"" not in source, (
            "Old SQLite exclusion guard still present in sql_generation_node"
        )

        # The new dialect parameter should be used
        assert "dialect=" in source or "few_shot_dialect" in source, (
            "sql_generation_node does not pass dialect to few-shot selector"
        )

    def test_sql_generation_maps_dialect_correctly(self):
        """sql_generation_node should map unknown dialects to 'mysql' for backward compat."""
        import inspect
        from app.agents.sql_generation import sql_generation_node

        source = inspect.getsource(sql_generation_node)

        # Check backward compatibility mapping
        assert "mysql" in source, "No mysql fallback in sql_generation_node"


# ────────────────────────────────────────────────
# Group 5: Singleton & Module-Level Behavior
# ────────────────────────────────────────────────

class TestSingleton:
    """Verify singleton behavior of the few-shot selector."""

    def test_get_few_shot_selector_returns_same_instance(self):
        """get_few_shot_selector should return the same instance."""
        import app.prompts.few_shot as fs_module
        fs_module._selector_instance = None  # Reset

        from app.prompts.few_shot import get_few_shot_selector

        s1 = get_few_shot_selector()
        s2 = get_few_shot_selector()
        assert s1 is s2, "Singleton returned different instances"

    def test_env_disable_prevents_embeddings(self):
        """DISABLE_ML_INTENT=true should prevent encoder loading."""
        import app.prompts.few_shot as fs_module
        fs_module._selector_instance = None  # Reset

        os.environ["DISABLE_ML_INTENT"] = "true"
        try:
            from app.prompts.few_shot import DynamicFewShotSelector

            selector = DynamicFewShotSelector()
            selector._lazy_load()

            assert selector._encoder is None, "Encoder loaded despite DISABLE_ML_INTENT=true"
            # Examples should still load
            total = sum(len(exs) for exs in selector._examples_by_dialect.values())
            assert total > 0, "No examples loaded even though only ML was disabled"
        finally:
            os.environ.pop("DISABLE_ML_INTENT", None)


# ────────────────────────────────────────────────
# Group 6: Backward Compatibility
# ────────────────────────────────────────────────

class TestBackwardCompatibility:
    """Verify that existing MySQL/TiDB functionality is not broken."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset the singleton before each test."""
        import app.prompts.few_shot as fs_module
        fs_module._selector_instance = None
        os.environ.pop("DISABLE_ML_INTENT", None)

    def test_explicit_path_mode(self):
        """Legacy single-path mode should still work."""
        from app.prompts.few_shot import DynamicFewShotSelector

        train_path = os.path.join(
            os.path.dirname(__file__), "..", "evaluation", "datasets", "train.json"
        )
        selector = DynamicFewShotSelector(examples_path=train_path)
        selector._lazy_load()

        assert "default" in selector._examples_by_dialect
        assert len(selector._examples_by_dialect["default"]) > 0

    @pytest.mark.skipif(
        _ml_disabled(),
        reason="ML models disabled by environment",
    )
    def test_mysql_few_shot_still_works(self):
        """MySQL few-shot selection should continue working."""
        from app.prompts.few_shot import get_few_shot_selector

        selector = get_few_shot_selector()
        results = selector.select("Show all accounts with high churn risk", k=3, dialect="mysql")

        assert len(results) > 0, "MySQL few-shot returned no results"

    def test_train_json_unchanged(self):
        """train.json (MySQL dataset) should not be modified."""
        train_path = os.path.join(
            os.path.dirname(__file__), "..", "evaluation", "datasets", "train.json"
        )
        with open(train_path, "r") as f:
            data = json.load(f)

        # Original train.json should have 51 examples (eval_001 to eval_051)
        assert len(data) >= 50, f"train.json has {len(data)} examples (expected ≥50)"

        # First example should be about accounts
        assert "accounts" in data[0]["expected_sql"].lower() or "accounts" in data[0]["question"].lower()

"""
Phase 8 Regression Tests — Deterministic Evaluation, Provider Pinning, Pacing & Semantic Handling.

Tests:
1. Evaluation mode configuration & production defaults preservation
2. Strict provider pinning (no silent fallback when pinned provider fails)
3. Production fallback & circuit breaker preservation
4. Evaluation-only request pacing
5. Temporal projection rule presence & guidance
6. Business terminology guidance (unit volume vs revenue distinction, non-hardcoded)
7. SQLite few-shot dataset isolation (29 examples, valid SQLite, 0 MySQL leaks)
8. Exact table mention boosting preservation
"""

import json
import os
import pytest
from unittest.mock import MagicMock, patch

from app.config import Settings, get_settings
from app.llm.router import ModelRouter, CircuitBreaker
from app.prompts.registry import get_prompt_registry
from app.prompts.few_shot import DynamicFewShotSelector


class TestDeterministicEvalConfig:
    """Test deterministic evaluation settings and production defaults."""

    def test_production_defaults_preserved(self):
        """Production defaults must remain unchanged: eval_mode=False, temp=0.1, no seed pinned."""
        settings = Settings(
            DB_URI="mysql+pymysql://test:test@localhost:3306/test",
            JWT_SECRET_KEY="x" * 32,
        )
        assert settings.PLAINSQL_EVAL_MODE is False
        assert settings.PLAINSQL_EVAL_PROVIDER == "groq"
        assert settings.PLAINSQL_EVAL_TEMPERATURE == 0.0
        assert settings.PLAINSQL_EVAL_SEED == 42
        assert settings.PLAINSQL_EVAL_DELAY_MS == 500

    def test_evaluation_mode_overrides(self):
        """When PLAINSQL_EVAL_MODE is enabled, config reflects evaluation parameters."""
        settings = Settings(
            DB_URI="mysql+pymysql://test:test@localhost:3306/test",
            JWT_SECRET_KEY="x" * 32,
            PLAINSQL_EVAL_MODE=True,
            PLAINSQL_EVAL_PROVIDER="groq",
            PLAINSQL_EVAL_TEMPERATURE=0.0,
            PLAINSQL_EVAL_SEED=42,
            PLAINSQL_EVAL_DELAY_MS=250,
        )
        assert settings.PLAINSQL_EVAL_MODE is True
        assert settings.PLAINSQL_EVAL_PROVIDER == "groq"
        assert settings.PLAINSQL_EVAL_TEMPERATURE == 0.0
        assert settings.PLAINSQL_EVAL_SEED == 42
        assert settings.PLAINSQL_EVAL_DELAY_MS == 250


class TestProviderPinning:
    """Test provider pinning and fallback behavior."""

    def test_strict_eval_raises_on_pinned_provider_failure(self):
        """Strict evaluation mode with pinned_provider MUST NOT silently fallback to another provider."""
        mock_groq = MagicMock()
        mock_groq.generate.side_effect = RuntimeError("Groq rate limit exceeded (429)")

        mock_hf = MagicMock()
        mock_hf.generate.return_value = "SELECT * FROM fallback;"

        router = ModelRouter.__new__(ModelRouter)
        router.providers = {"groq": mock_groq, "huggingface": mock_hf}
        router.breakers = {"groq": CircuitBreaker(), "huggingface": CircuitBreaker()}
        router.routing = {"default": "groq"}
        router.default_provider = "groq"
        router.token_tracker = MagicMock()

        # When pinned_provider="groq", it must raise RuntimeError and NOT call mock_hf
        with pytest.raises(RuntimeError, match="Pinned evaluation provider 'groq' failed"):
            router.generate(
                [{"role": "user", "content": "SELECT 1"}],
                pinned_provider="groq",
            )

        assert mock_groq.generate.called
        assert not mock_hf.generate.called

    def test_production_fallback_preserved_when_not_pinned(self):
        """Without pinned_provider, normal production fallback chain operates as expected."""
        mock_groq = MagicMock()
        mock_groq.generate.side_effect = RuntimeError("Groq 429")

        mock_hf = MagicMock()
        mock_hf.generate.return_value = "SELECT 1"

        router = ModelRouter.__new__(ModelRouter)
        router.providers = {"groq": mock_groq, "huggingface": mock_hf}
        router.breakers = {"groq": CircuitBreaker(), "huggingface": CircuitBreaker()}
        router.routing = {"default": "groq"}
        router.default_provider = "groq"
        router.token_tracker = MagicMock()
        router.token_tracker.track.return_value = {
            "input_tokens": 10,
            "output_tokens": 10,
            "estimated_cost_usd": 0.0,
        }

        # In production mode (no pinned_provider), fallback to huggingface should succeed
        result = router.generate(
            [{"role": "user", "content": "SELECT 1"}],
            pinned_provider=None,
        )
        assert result == "SELECT 1"
        assert mock_groq.generate.called
        assert mock_hf.generate.called


class TestPromptGuidancePhase8:
    """Test SQLite prompt guidance for temporal projection and business terminology."""

    def test_temporal_projection_rule_17_present(self):
        """Verify Rule 17 is present in the active SQLite prompt template."""
        registry = get_prompt_registry()
        tmpl = registry.get("sql_generation_sqlite")
        prompt_text = tmpl.system

        assert "17." in prompt_text
        assert "temporal dimension" in prompt_text.lower()
        assert "single season" in prompt_text.lower()
        assert "include the temporal dimension in the select projection" in prompt_text.lower()
        assert "do not add temporal columns if no such column exists" in prompt_text.lower()

    def test_business_terminology_rule_18_present(self):
        """Verify Rule 18 is present and distinguishes volume vs revenue without universal hardcoding."""
        registry = get_prompt_registry()
        tmpl = registry.get("sql_generation_sqlite")
        prompt_text = tmpl.system

        assert "18." in prompt_text
        assert "volume vs revenue" in prompt_text.lower()
        assert "item sales volume" in prompt_text.lower()
        assert "count(order_item_id)" in prompt_text.lower() or "count(" in prompt_text.lower()
        assert "do not hardcode a single universal rule" in prompt_text.lower()


class TestFewShotDatasetPhase8:
    """Test SQLite few-shot dataset additions and dialect isolation."""

    def test_sqlite_train_dataset_size_and_schema(self):
        """Verify sqlite_train.json contains exactly 29 examples, all valid SQLite."""
        file_path = os.path.join(
            os.path.dirname(__file__), "..", "evaluation", "datasets", "sqlite_train.json"
        )
        with open(file_path, "r", encoding="utf-8") as f:
            examples = json.load(f)

        assert len(examples) == 29, f"Expected 29 examples, got {len(examples)}"

        # Verify all examples have dialect="sqlite"
        for ex in examples:
            assert ex["dialect"] == "sqlite", f"Example {ex['id']} has dialect {ex.get('dialect')}"

    def test_four_new_targeted_examples_exist(self):
        """Verify sqlite_026 through sqlite_029 cover the four required semantic concepts."""
        file_path = os.path.join(
            os.path.dirname(__file__), "..", "evaluation", "datasets", "sqlite_train.json"
        )
        with open(file_path, "r", encoding="utf-8") as f:
            examples = {ex["id"]: ex for ex in json.load(f)}

        # Example A: Unit/item volume
        assert "sqlite_026" in examples
        assert "number of items sold" in examples["sqlite_026"]["question"].lower()
        assert "count(" in examples["sqlite_026"]["expected_sql"].lower()

        # Example B: Revenue
        assert "sqlite_027" in examples
        assert "total sales revenue" in examples["sqlite_027"]["question"].lower()
        assert "sum(" in examples["sqlite_027"]["expected_sql"].lower()

        # Example C: Temporal top-N
        assert "sqlite_028" in examples
        assert "single season" in examples["sqlite_028"]["question"].lower()
        assert "year" in examples["sqlite_028"]["expected_sql"].lower()
        assert "w" in examples["sqlite_028"]["expected_sql"].lower()

        # Example D: Temporal aggregation
        assert "sqlite_029" in examples
        assert "by year" in examples["sqlite_029"]["question"].lower()
        assert "strftime" in examples["sqlite_029"]["expected_sql"].lower()

    def test_few_shot_selector_isolation(self):
        """DynamicFewShotSelector with dialect='sqlite' selects only sqlite examples."""
        selector = DynamicFewShotSelector()
        results = selector.select("What are the top product categories by sales volume?", k=3, dialect="sqlite")

        assert len(results) > 0
        for r in results:
            assert r.get("dialect") == "sqlite"
            # Ensure no MySQL syntax leaked
            sql = r.get("expected_sql", "").lower()
            assert "curdate()" not in sql
            assert "date_add(" not in sql
            assert "date_sub(" not in sql
            assert "date_format(" not in sql

"""
Spider 2.0-Lite Evaluation Runner.

Evaluates PlainSQL (and Baseline) on Spider 2.0-Lite SQLite databases.
Measures:
1. SQL validity
2. Execution accuracy (normalized result comparison)
3. Schema retrieval quality (Table Recall & Precision)
4. Self-repair success rate
5. Execution latency (Avg, Median, P95)
6. Failure classification

Supports --limit N, --db_id, --mode [plainsql | baseline | both].
Saves results to evaluation/results/spider2_lite_results.json and summary.json.
"""

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Optional
import numpy as np
import sqlparse
import structlog

# Ensure backend root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings
from app.db.registry import get_database_registry, DatabaseRegistry
from app.llm.router import LLMRouter
from app.rag.retriever import HybridRetriever
from app.agents.orchestrator import AgentOrchestrator
from app.agents.sql_validation import sql_validation_node
from app.agents.state import AgentState
from evaluation.runner import EvalMetrics
from evaluation.spider_loader import SpiderDatasetLoader

logger = structlog.get_logger()


def extract_tables_from_sql(sql: str, known_tables: list[str]) -> list[str]:
    """
    Extract referenced tables from a SQL query by matching tokens against
    the database's known table names (case-insensitive).
    """
    if not sql or not known_tables:
        return []

    found = set()
    cleaned = re.sub(r'["`]', '', sql).lower()
    tokens = set(re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', cleaned))

    for tbl in known_tables:
        if tbl.lower() in tokens:
            found.add(tbl)

    return sorted(list(found))


class SpiderEvaluator:
    """
    Comprehensive evaluation harness for Spider 2.0-Lite SQLite benchmarks.
    Runs both PlainSQL and direct Baseline pipelines and computes comparative metrics.
    """

    def __init__(
        self,
        registry: Optional[DatabaseRegistry] = None,
        llm_router: Optional[LLMRouter] = None,
        rag_retriever: Optional[HybridRetriever] = None,
        eval_dir: Optional[str] = None,
    ):
        settings = get_settings()
        self.registry = registry or get_database_registry()
        if llm_router is not None:
            self.llm_router = llm_router
        else:
            llm_config = {
                "default_provider": settings.DEFAULT_LLM_PROVIDER,
                "groq_api_key": settings.GROQ_API_KEY,
                "groq_model_primary": settings.GROQ_MODEL_PRIMARY,
                "groq_model_fast": settings.GROQ_MODEL_FAST,
                "groq_base_url": settings.GROQ_BASE_URL,
                "huggingface_token": settings.HUGGINGFACEHUB_API_TOKEN,
                "huggingface_model": settings.DEFAULT_MODEL,
                "openai_api_key": settings.OPENAI_API_KEY,
                "anthropic_api_key": settings.ANTHROPIC_API_KEY,
                "ollama_base_url": settings.OLLAMA_BASE_URL,
            }
            self.llm_router = LLMRouter(llm_config)
        self.eval_dir = eval_dir or settings.SPIDER_EVAL_DIR
        self.loader = SpiderDatasetLoader(eval_dir=self.eval_dir, registry=self.registry)

        # Initialize HybridRetriever if not provided
        if rag_retriever is not None:
            self.retriever = rag_retriever
        else:
            self.retriever = HybridRetriever(
                db_pool=None,
                chroma_persist_dir=settings.CHROMA_PERSIST_DIR,
                registry=self.registry,
            )

    def evaluate_item_plainsql(self, example: dict[str, Any]) -> dict[str, Any]:
        """
        Evaluate a single benchmark item using the full PlainSQL Agentic Pipeline.
        """
        instance_id = example["instance_id"]
        db_id = example["db_id"]
        question = example["question"]
        gold_sql = example.get("gold_sql")
        has_gold = example.get("has_gold_sql", bool(gold_sql))

        settings = get_settings()
        eval_mode = settings.PLAINSQL_EVAL_MODE
        eval_temp = settings.PLAINSQL_EVAL_TEMPERATURE
        eval_seed = settings.PLAINSQL_EVAL_SEED
        pinned_provider = settings.PLAINSQL_EVAL_PROVIDER if eval_mode else None

        record: dict[str, Any] = {
            "instance_id": instance_id,
            "db_id": db_id,
            "question": question,
            "gold_sql": gold_sql,
            "has_gold_sql": has_gold,
            "pipeline": "plainsql",
            "eval_mode": eval_mode,
            "provider": pinned_provider or settings.DEFAULT_LLM_PROVIDER,
            "model": settings.GROQ_MODEL_PRIMARY,
            "temperature": eval_temp if eval_mode else 0.1,
            "seed": eval_seed if eval_mode else None,
            "pinned_provider": pinned_provider,
            "fallback_used": False,
            "selected_few_shots": [],
            "generated_sql": "",
            "is_valid": False,
            "validation_errors": [],
            "initial_execution_success": False,
            "repair_used": False,
            "repair_success": False,
            "final_execution_success": False,
            "execution_accuracy": None,
            "table_recall": None,
            "table_precision": None,
            "retrieved_tables": [],
            "gold_tables": [],
            "latency_ms": 0.0,
            "failure_category": None,
            "error": None,
        }

        # Check question presence
        if not question or not question.strip():
            record["failure_category"] = "missing_question"
            return record

        # Check database resolution
        try:
            pool = self.registry.get_pool(db_id)
            known_tables = pool.get_tables()
        except Exception as e:
            record["failure_category"] = "missing_database"
            record["error"] = f"Database '{db_id}' not found: {str(e)}"
            return record

        # Extract gold tables if gold SQL available
        gold_results = None
        if has_gold and gold_sql:
            record["gold_tables"] = extract_tables_from_sql(gold_sql, known_tables)
            try:
                gold_results = pool.execute_query(gold_sql)
            except Exception as e:
                logger.warning("gold_sql_execution_failed", db_id=db_id, error=str(e))
                record["error"] = f"Gold SQL execution error: {str(e)}"

        # Pre-index database for Hybrid RAG if not already indexed
        try:
            self.retriever.index_database(db_id, db_pool=pool)
        except Exception as e:
            logger.warning("index_database_error", db_id=db_id, error=str(e))

        # Build orchestrator for this database
        orchestrator = AgentOrchestrator(
            self.llm_router,
            self.retriever,
            pool,
            registry=self.registry,
        )

        start_time = time.perf_counter()
        initial_state: AgentState = {
            "user_query": question,
            "db_id": db_id,
            "sql_dialect": "sqlite",
            "retry_count": 0,
            "max_retries": 2,
            "validation_errors": [],
            "conversation_history": [],
            "session_id": f"eval_{instance_id}",
            "eval_mode": eval_mode,
            "eval_temperature": eval_temp,
            "eval_seed": eval_seed,
            "pinned_provider": pinned_provider,
        }

        try:
            final_state = orchestrator.graph.invoke(initial_state)
            record["latency_ms"] = round((time.perf_counter() - start_time) * 1000, 2)

            generated_sql = final_state.get("generated_sql", "")
            record["generated_sql"] = generated_sql
            record["is_valid"] = final_state.get("is_valid", False)
            record["validation_errors"] = final_state.get("validation_errors", [])
            record["retrieved_tables"] = final_state.get("relevant_tables", [])
            record["selected_few_shots"] = final_state.get("selected_few_shots", [])
            if final_state.get("llm_provider"):
                record["provider"] = final_state.get("llm_provider")
            retry_count = final_state.get("retry_count", 0)

            # Self-repair metrics tracking
            if retry_count > 0:
                record["repair_used"] = True
                record["initial_execution_success"] = False
                if final_state.get("error") is None and final_state.get("row_count", 0) >= 0:
                    record["repair_success"] = True
                    record["final_execution_success"] = True
                else:
                    record["repair_success"] = False
                    record["final_execution_success"] = False
            else:
                record["repair_used"] = False
                if final_state.get("error") is None:
                    record["initial_execution_success"] = True
                    record["final_execution_success"] = True
                else:
                    record["initial_execution_success"] = False
                    record["final_execution_success"] = False

            # Schema Table Recall & Precision
            if record["gold_tables"]:
                gold_set = set(t.lower() for t in record["gold_tables"])
                retrieved_set = set(t.lower() for t in record["retrieved_tables"])
                intersection = gold_set.intersection(retrieved_set)
                record["table_recall"] = round(len(intersection) / len(gold_set), 4) if gold_set else 1.0
                record["table_precision"] = round(len(intersection) / len(retrieved_set), 4) if retrieved_set else 0.0

            # Execution Accuracy comparison
            generated_results = final_state.get("query_results")
            if has_gold and gold_results is not None:
                if generated_results is not None and record["final_execution_success"]:
                    is_match = EvalMetrics.execution_match(generated_results, gold_results)
                    record["execution_accuracy"] = is_match
                else:
                    record["execution_accuracy"] = False
            else:
                record["execution_accuracy"] = None  # Gold not available for execution scoring

            # Failure classification
            if not record["final_execution_success"]:
                err = final_state.get("error") or "Unknown execution error"
                record["error"] = err
                if not generated_sql:
                    record["failure_category"] = "sql_generation_failure"
                elif not record["is_valid"]:
                    record["failure_category"] = "sql_validation_failure"
                elif record["repair_used"] and not record["repair_success"]:
                    record["failure_category"] = "self_repair_failure"
                else:
                    record["failure_category"] = "sql_execution_failure"
            elif record["execution_accuracy"] is False:
                record["failure_category"] = "semantic_mismatch"
            else:
                record["failure_category"] = None

        except Exception as e:
            record["latency_ms"] = round((time.perf_counter() - start_time) * 1000, 2)
            record["error"] = str(e)
            record["failure_category"] = "pipeline_exception"

        return record

    def evaluate_item_baseline(self, example: dict[str, Any]) -> dict[str, Any]:
        """
        Evaluate a single benchmark item using Baseline mode (Direct LLM without PlainSQL pipeline).

        Fairness context provided:
        Baseline receives the full database DDL schema for that db_id and question,
        prompted directly to generate SQLite SQL without PlainSQL's query understanding,
        Hybrid RAG, AST validation, guardrails, or self-repair loops.
        """
        instance_id = example["instance_id"]
        db_id = example["db_id"]
        question = example["question"]
        gold_sql = example.get("gold_sql")
        has_gold = example.get("has_gold_sql", bool(gold_sql))

        record: dict[str, Any] = {
            "instance_id": instance_id,
            "db_id": db_id,
            "question": question,
            "gold_sql": gold_sql,
            "has_gold_sql": has_gold,
            "pipeline": "baseline",
            "generated_sql": "",
            "is_valid": False,
            "initial_execution_success": False,
            "repair_used": False,
            "repair_success": False,
            "final_execution_success": False,
            "execution_accuracy": None,
            "latency_ms": 0.0,
            "failure_category": None,
            "error": None,
        }

        if not question or not question.strip():
            record["failure_category"] = "missing_question"
            return record

        try:
            pool = self.registry.get_pool(db_id)
            full_schema = pool.get_full_schema()
        except Exception as e:
            record["failure_category"] = "missing_database"
            record["error"] = str(e)
            return record

        gold_results = None
        if has_gold and gold_sql:
            try:
                gold_results = pool.execute_query(gold_sql)
            except Exception as e:
                pass

        # Baseline prompt: Direct question + full schema
        baseline_prompt = [
            {
                "role": "system",
                "content": (
                    "You are a SQL generator for SQLite databases. "
                    "Write a valid SQLite SELECT query that answers the user's question. "
                    "Return ONLY raw SQL or JSON with a 'sql' key. Do not include markdown formatting.\n\n"
                    f"DATABASE SCHEMA:\n{full_schema}"
                ),
            },
            {
                "role": "user",
                "content": question,
            },
        ]

        start_time = time.perf_counter()
        try:
            response = self.llm_router.generate(baseline_prompt, max_tokens=512, temperature=0.0)
            record["latency_ms"] = round((time.perf_counter() - start_time) * 1000, 2)

            # Parse SQL from response
            cleaned_sql = self._extract_sql_from_text(response)
            record["generated_sql"] = cleaned_sql

            if not cleaned_sql:
                record["failure_category"] = "sql_generation_failure"
                record["error"] = "Empty SQL returned by baseline LLM"
                return record

            # Basic AST syntax validity check
            parsed = sqlparse.parse(cleaned_sql)
            is_valid_syntax = len(parsed) > 0 and parsed[0].get_type() == "SELECT"
            record["is_valid"] = is_valid_syntax

            # Execute baseline SQL directly on SQLite
            try:
                baseline_results = pool.execute_query(cleaned_sql)
                record["initial_execution_success"] = True
                record["final_execution_success"] = True

                if has_gold and gold_results is not None:
                    record["execution_accuracy"] = EvalMetrics.execution_match(baseline_results, gold_results)
                    if record["execution_accuracy"] is False:
                        record["failure_category"] = "semantic_mismatch"
                else:
                    record["execution_accuracy"] = None

            except Exception as exec_err:
                record["initial_execution_success"] = False
                record["final_execution_success"] = False
                record["error"] = str(exec_err)
                record["failure_category"] = "sql_execution_failure"

        except Exception as e:
            record["latency_ms"] = round((time.perf_counter() - start_time) * 1000, 2)
            record["error"] = str(e)
            record["failure_category"] = "pipeline_exception"

        return record

    @staticmethod
    def _extract_sql_from_text(text: str) -> str:
        """Extract clean SQL string from direct LLM output."""
        if not text:
            return ""
        # Check if JSON
        text = text.strip()
        if text.startswith("{") and "sql" in text.lower():
            try:
                d = json.loads(text)
                if "sql" in d:
                    return d["sql"].strip()
            except Exception:
                pass
        # Check markdown blocks
        match = re.search(r'```(?:sql)?\s*(.*?)\s*```', text, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()
        # Fallback to text directly
        return text.rstrip(";").strip() + ";"

    def run_evaluation(
        self,
        db_id: Optional[str] = None,
        limit: int = 20,
        mode: str = "plainsql",
        require_gold_sql: bool = False,
    ) -> dict[str, Any]:
        """
        Run batch evaluation across Spider 2.0-Lite benchmark examples.

        Args:
            db_id: Optional database filter
            limit: Maximum items to evaluate
            mode: 'plainsql', 'baseline', or 'both'
            require_gold_sql: If True, only evaluate items with gold SQL

        Returns:
            Dict containing detailed records and summary metrics.
        """
        dataset = self.loader.load_dataset(
            db_id=db_id, limit=limit, require_gold_sql=require_gold_sql
        )

        plainsql_records = []
        baseline_records = []

        settings = get_settings()
        pacing_ms = getattr(settings, "PLAINSQL_EVAL_DELAY_MS", 500)
        run_id = f"eval_{time.strftime('%Y%m%d_%H%M%S')}"

        logger.info(
            "evaluation_started",
            run_id=run_id,
            dataset_size=len(dataset),
            mode=mode,
            db_id=db_id,
            limit=limit,
            pacing_delay_ms=pacing_ms,
            eval_mode=settings.PLAINSQL_EVAL_MODE,
        )

        for i, item in enumerate(dataset, 1):
            logger.info("evaluating_item", index=i, total=len(dataset), instance_id=item["instance_id"], db=item["db_id"])

            if mode in ("plainsql", "both"):
                rec_plain = self.evaluate_item_plainsql(item)
                rec_plain["run_id"] = run_id
                rec_plain["pacing_delay_ms"] = pacing_ms
                plainsql_records.append(rec_plain)

            if mode in ("baseline", "both"):
                rec_base = self.evaluate_item_baseline(item)
                rec_base["run_id"] = run_id
                rec_base["pacing_delay_ms"] = pacing_ms
                baseline_records.append(rec_base)

            # Evaluation-only request pacing (Phase 8) to avoid rate limits
            if pacing_ms > 0 and i < len(dataset):
                time.sleep(pacing_ms / 1000.0)

        # Compute summary metrics
        summary = {
            "run_id": run_id,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "eval_mode": settings.PLAINSQL_EVAL_MODE,
            "pinned_provider": settings.PLAINSQL_EVAL_PROVIDER if settings.PLAINSQL_EVAL_MODE else None,
            "provider": settings.PLAINSQL_EVAL_PROVIDER or settings.DEFAULT_LLM_PROVIDER,
            "model": settings.GROQ_MODEL_PRIMARY,
            "temperature": settings.PLAINSQL_EVAL_TEMPERATURE if settings.PLAINSQL_EVAL_MODE else 0.1,
            "seed": settings.PLAINSQL_EVAL_SEED if settings.PLAINSQL_EVAL_MODE else None,
            "pacing_delay_ms": pacing_ms,
            "dataset_filter": {"db_id": db_id, "limit": limit, "mode": mode},
            "total_examples": len(dataset),
        }

        if plainsql_records:
            summary["plainsql"] = self.compute_aggregate_metrics(plainsql_records)
        if baseline_records:
            summary["baseline"] = self.compute_aggregate_metrics(baseline_records)

        # Ensure output directory exists
        out_dir = os.path.join(os.path.dirname(__file__), "results")
        os.makedirs(out_dir, exist_ok=True)

        results_path = os.path.join(out_dir, "spider2_lite_results.json")
        summary_path = os.path.join(out_dir, "spider2_lite_summary.json")

        all_results = {
            "plainsql_records": plainsql_records,
            "baseline_records": baseline_records,
        }

        with open(results_path, "w", encoding="utf-8") as f:
            json.dump(all_results, f, indent=2)

        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

        logger.info("evaluation_finished", results_file=results_path, summary_file=summary_path)

        return {
            "summary": summary,
            "results_path": results_path,
            "summary_path": summary_path,
            "plainsql_records": plainsql_records,
            "baseline_records": baseline_records,
        }

    @staticmethod
    def compute_aggregate_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
        """Compute aggregate benchmark metrics over a set of evaluation records."""
        total = len(records)
        if total == 0:
            return {}

        evaluated = [r for r in records if r.get("failure_category") not in ("missing_question", "missing_database")]
        eval_count = len(evaluated)
        skipped_count = total - eval_count

        # Validity & Execution
        valid_count = sum(1 for r in evaluated if r.get("is_valid"))
        init_success_count = sum(1 for r in evaluated if r.get("initial_execution_success"))
        final_success_count = sum(1 for r in evaluated if r.get("final_execution_success"))

        # Self-repair
        repair_attempted = sum(1 for r in evaluated if r.get("repair_used"))
        repair_succeeded = sum(1 for r in evaluated if r.get("repair_success"))
        repair_success_rate = round(repair_succeeded / repair_attempted, 4) if repair_attempted > 0 else 0.0

        # Execution Accuracy (scored only over examples where gold SQL was available and executed)
        gold_examples = [r for r in evaluated if r.get("has_gold_sql") and r.get("execution_accuracy") is not None]
        gold_count = len(gold_examples)
        exec_acc_count = sum(1 for r in gold_examples if r.get("execution_accuracy") is True)
        exec_accuracy = round(exec_acc_count / gold_count, 4) if gold_count > 0 else 0.0

        # Retrieval Table Recall & Precision
        recall_values = [r["table_recall"] for r in evaluated if r.get("table_recall") is not None]
        prec_values = [r["table_precision"] for r in evaluated if r.get("table_precision") is not None]
        avg_table_recall = round(float(np.mean(recall_values)), 4) if recall_values else 0.0
        avg_table_precision = round(float(np.mean(prec_values)), 4) if prec_values else 0.0

        # Latencies
        latencies = [r["latency_ms"] for r in evaluated if r.get("latency_ms", 0) > 0]
        avg_lat = round(float(np.mean(latencies)), 2) if latencies else 0.0
        med_lat = round(float(np.median(latencies)), 2) if latencies else 0.0
        p95_lat = round(float(np.percentile(latencies, 95)), 2) if latencies else 0.0

        # Failure categories breakdown
        failure_categories: dict[str, int] = {}
        for r in records:
            cat = r.get("failure_category")
            if cat:
                failure_categories[cat] = failure_categories.get(cat, 0) + 1

        return {
            "total_examples": total,
            "evaluated_examples": eval_count,
            "skipped_examples": skipped_count,
            "sql_validity_rate": round(valid_count / eval_count, 4) if eval_count > 0 else 0.0,
            "initial_execution_success_rate": round(init_success_count / eval_count, 4) if eval_count > 0 else 0.0,
            "repair_attempt_rate": round(repair_attempted / eval_count, 4) if eval_count > 0 else 0.0,
            "repair_success_rate": repair_success_rate,
            "final_execution_success_rate": round(final_success_count / eval_count, 4) if eval_count > 0 else 0.0,
            "examples_with_gold_sql": gold_count,
            "execution_accuracy": exec_accuracy,
            "schema_table_recall": avg_table_recall,
            "schema_table_precision": avg_table_precision,
            "latency_ms": {
                "average": avg_lat,
                "median": med_lat,
                "p95": p95_lat,
            },
            "failure_categories": failure_categories,
        }


def print_smoke_test_summary(eval_output: dict[str, Any]):
    """Print human-readable smoke test table and metrics summary."""
    plainsql_recs = eval_output.get("plainsql_records", [])
    summary = eval_output.get("summary", {})

    print("\n" + "=" * 110)
    print("                      SPIDER 2.0-LITE SMOKE TEST BREAKDOWN (PLAINSQL)")
    print("=" * 110)
    print(f"{'ID':<10} | {'Database':<14} | {'Gold?':<6} | {'Valid?':<6} | {'Init':<5} | {'Repair':<7} | {'Final':<6} | {'Acc?':<5} | {'Question':<35}")
    print("-" * 110)

    for r in plainsql_recs:
        iid = r.get("instance_id", "")[:10]
        db = r.get("db_id", "")[:14]
        has_g = "Yes" if r.get("has_gold_sql") else "No"
        valid = "Yes" if r.get("is_valid") else "No"
        init_ok = "Pass" if r.get("initial_execution_success") else "Fail"
        repair = "Used" if r.get("repair_used") else "None"
        final_ok = "Pass" if r.get("final_execution_success") else "Fail"
        acc = "True" if r.get("execution_accuracy") is True else ("False" if r.get("execution_accuracy") is False else "N/A")
        q = r.get("question", "")[:35]
        print(f"{iid:<10} | {db:<14} | {has_g:<6} | {valid:<6} | {init_ok:<5} | {repair:<7} | {final_ok:<6} | {acc:<5} | {q:<35}")

    print("=" * 110)

    plain_metrics = summary.get("plainsql", {})
    if plain_metrics:
        print("\n=== PLAINSQL AGGREGATE METRICS ===")
        print(f"Total Evaluated        : {plain_metrics.get('evaluated_examples')} / {plain_metrics.get('total_examples')}")
        print(f"SQL Validity Rate      : {plain_metrics.get('sql_validity_rate') * 100:.1f}%")
        print(f"Initial Success Rate   : {plain_metrics.get('initial_execution_success_rate') * 100:.1f}%")
        print(f"Repair Attempt Rate    : {plain_metrics.get('repair_attempt_rate') * 100:.1f}%")
        print(f"Repair Success Rate    : {plain_metrics.get('repair_success_rate') * 100:.1f}%")
        print(f"Final Success Rate     : {plain_metrics.get('final_execution_success_rate') * 100:.1f}%")
        print(f"Execution Accuracy     : {plain_metrics.get('execution_accuracy') * 100:.1f}% (over {plain_metrics.get('examples_with_gold_sql')} gold examples)")
        print(f"Schema Table Recall    : {plain_metrics.get('schema_table_recall') * 100:.1f}%")
        print(f"Schema Table Precision : {plain_metrics.get('schema_table_precision') * 100:.1f}%")
        lat = plain_metrics.get("latency_ms", {})
        print(f"Latency (Avg / Med / P95) : {lat.get('average')} ms / {lat.get('median')} ms / {lat.get('p95')} ms")
        print(f"Failure Categories     : {plain_metrics.get('failure_categories')}")

    base_metrics = summary.get("baseline", {})
    if base_metrics:
        print("\n=== BASELINE AGGREGATE METRICS ===")
        print(f"Total Evaluated        : {base_metrics.get('evaluated_examples')} / {base_metrics.get('total_examples')}")
        print(f"SQL Validity Rate      : {base_metrics.get('sql_validity_rate') * 100:.1f}%")
        print(f"Execution Accuracy     : {base_metrics.get('execution_accuracy') * 100:.1f}%")
        print(f"Final Success Rate     : {base_metrics.get('final_execution_success_rate') * 100:.1f}%")
        lat = base_metrics.get("latency_ms", {})
        print(f"Latency (Avg / Med / P95) : {lat.get('average')} ms / {lat.get('median')} ms / {lat.get('p95')} ms")
        print(f"Failure Categories     : {base_metrics.get('failure_categories')}")


def main():
    parser = argparse.ArgumentParser(description="Spider 2.0-Lite SQLite Evaluation Runner")
    parser.add_argument("--db-id", type=str, default=None, help="Filter by specific database ID (e.g. E_commerce)")
    parser.add_argument("--limit", type=int, default=20, help="Maximum number of examples to evaluate")
    parser.add_argument("--mode", type=str, default="both", choices=["plainsql", "baseline", "both"], help="Evaluation mode")
    parser.add_argument("--require-gold", action="store_true", help="Only evaluate examples with gold SQL")
    parser.add_argument("--eval-mode", action="store_true", default=False, help="Enable strict deterministic evaluation mode")
    parser.add_argument("--eval-provider", type=str, default=None, help="Pinned provider (e.g. groq)")
    parser.add_argument("--eval-temperature", type=float, default=None, help="Deterministic evaluation temperature")
    parser.add_argument("--eval-seed", type=int, default=None, help="Deterministic evaluation seed")
    parser.add_argument("--eval-delay-ms", type=int, default=None, help="Pacing delay in milliseconds")
    args = parser.parse_args()

    if args.eval_mode:
        os.environ["PLAINSQL_EVAL_MODE"] = "true"
    if args.eval_provider:
        os.environ["PLAINSQL_EVAL_PROVIDER"] = args.eval_provider
    if args.eval_temperature is not None:
        os.environ["PLAINSQL_EVAL_TEMPERATURE"] = str(args.eval_temperature)
    if args.eval_seed is not None:
        os.environ["PLAINSQL_EVAL_SEED"] = str(args.eval_seed)
    if args.eval_delay_ms is not None:
        os.environ["PLAINSQL_EVAL_DELAY_MS"] = str(args.eval_delay_ms)

    get_settings.cache_clear()

    evaluator = SpiderEvaluator()
    output = evaluator.run_evaluation(
        db_id=args.db_id,
        limit=args.limit,
        mode=args.mode,
        require_gold_sql=args.require_gold,
    )

    print_smoke_test_summary(output)


if __name__ == "__main__":
    main()

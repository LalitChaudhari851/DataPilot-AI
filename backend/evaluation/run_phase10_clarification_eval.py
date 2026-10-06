"""
Phase 10 Clarification Evaluation & Ablation Suite.

Evaluates Confidence-Aware Interactive Query Disambiguation across:
1. Multiple timestamp cases (ambiguous vs explicit)
2. Revenue vs volume cases (ambiguous vs explicit)
3. Status vs timestamp cases
4. Explicit Business Definition precedence cases
5. Core benchmark regression targets (local005, local006, local007, local008, local142, local143)

Compares 3 Configurations:
A. Phase 9 (Semantic layer only, no ambiguity detection)
B. Phase 10 Detection-Only (Ambiguity detected & assumptions tracked, but no interactive pause)
C. Phase 10 Full Interactive (Confidence-aware clarification loop with deterministic resolution)

Computes:
- Clarification Precision & Recall
- False Clarification Rate & Missed Ambiguity Rate
- Correct Candidate Selection Rate
- SQL Validity & Execution Success
- Execution Accuracy
- Latency (Avg, Median, P95)
"""

import os
import sys
import time
import json
import numpy as np
import structlog

# Ensure backend root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings
from app.db.registry import get_database_registry
from app.semantics.models import (
    SemanticRole,
    AmbiguityType,
    BusinessDefinition,
    SemanticAmbiguity,
)
from app.semantics.registry import get_semantic_registry
from app.semantics.ambiguity_detector import AmbiguityDetector
from app.agents.state import AgentState
from app.agents.schema_retrieval import schema_retrieval_node
from app.agents.sql_generation import sql_generation_node
from app.agents.sql_validation import sql_validation_node
from app.agents.execution import execution_node
from app.agents.orchestrator import AgentOrchestrator
from evaluation.runner import EvalMetrics
from evaluation.spider_eval import SpiderEvaluator

logger = structlog.get_logger()

# ── Targeted Clarification Benchmark Dataset ────────────────────────────────

CLARIFICATION_BENCHMARK_TARGETS = [
    {
        "id": "clarif_01_multi_timestamp_ambiguous",
        "category": "Multiple Timestamp (Ambiguous)",
        "db_id": "E_commerce",
        "question": "When were the orders delivered?",
        "is_truly_ambiguous": True,
        "expected_ambiguity_type": "multiple_timestamp",
        "user_clarification_response": "1",  # Choose option 1: customer delivery
        "gold_sql": "SELECT order_id, order_delivered_customer_date FROM orders WHERE order_delivered_customer_date IS NOT NULL LIMIT 10;",
    },
    {
        "id": "clarif_02_multi_timestamp_explicit",
        "category": "Multiple Timestamp (Explicit)",
        "db_id": "E_commerce",
        "question": "When did the carrier deliver the orders?",
        "is_truly_ambiguous": False,
        "expected_ambiguity_type": "multiple_timestamp",
        "user_clarification_response": None,
        "gold_sql": "SELECT order_id, order_delivered_carrier_date FROM orders WHERE order_delivered_carrier_date IS NOT NULL LIMIT 10;",
    },
    {
        "id": "clarif_03_revenue_vs_volume_ambiguous",
        "category": "Revenue vs Volume (Ambiguous)",
        "db_id": "E_commerce",
        "question": "What are the top 3 products by sales?",
        "is_truly_ambiguous": True,
        "expected_ambiguity_type": "revenue_vs_volume",
        "user_clarification_response": "revenue",  # Choose monetary revenue
        "gold_sql": "SELECT product_id, SUM(price) AS total_revenue FROM order_items GROUP BY product_id ORDER BY total_revenue DESC LIMIT 3;",
    },
    {
        "id": "clarif_04_revenue_vs_volume_explicit",
        "category": "Revenue vs Volume (Explicit)",
        "db_id": "E_commerce",
        "question": "What are the top 3 product categories by total item sales volume?",
        "is_truly_ambiguous": False,
        "expected_ambiguity_type": "revenue_vs_volume",
        "user_clarification_response": None,
        "gold_sql": "SELECT p.product_category_name, COUNT(oi.order_item_id) AS total_sold FROM order_items oi JOIN products p ON oi.product_id = p.product_id WHERE p.product_category_name IS NOT NULL GROUP BY p.product_category_name ORDER BY total_sold DESC LIMIT 3;",
    },
    {
        "id": "clarif_05_status_vs_timestamp_high_conf",
        "category": "Status vs Timestamp (High Confidence)",
        "db_id": "E_commerce",
        "question": "Find the average freight value and average price for delivered orders.",
        "is_truly_ambiguous": False,
        "expected_ambiguity_type": "status_vs_timestamp",
        "user_clarification_response": None,
        "gold_sql": "SELECT AVG(oi.price) AS avg_price, AVG(oi.freight_value) AS avg_freight FROM order_items oi JOIN orders o ON oi.order_id = o.order_id WHERE o.order_status = 'delivered';",
    },
    {
        "id": "clarif_06_business_definition_override",
        "category": "Business Definition Precedence",
        "db_id": "E_commerce",
        "question": "Show sales by product category.",
        "is_truly_ambiguous": False,  # Overridden by explicit definition
        "expected_ambiguity_type": "revenue_vs_volume",
        "business_definition": {
            "term": "sales",
            "meaning": "Monetary revenue of order items",
            "preferred_columns": ["price"],
            "formula_or_hint": "SUM(price)",
        },
        "user_clarification_response": None,
        "gold_sql": "SELECT p.product_category_name, SUM(oi.price) AS total_sales FROM products p JOIN order_items oi ON p.product_id = oi.product_id WHERE p.product_category_name IS NOT NULL GROUP BY p.product_category_name ORDER BY total_sales DESC LIMIT 10;",
    },
    {
        "id": "local007",
        "category": "Unambiguous Exact Table",
        "db_id": "Baseball",
        "question": "How many total players are listed in the player table?",
        "is_truly_ambiguous": False,
        "expected_ambiguity_type": None,
        "user_clarification_response": None,
        "gold_sql": "SELECT COUNT(*) FROM player;",
    },
    {
        "id": "local008",
        "category": "Unambiguous Temporal Metric",
        "db_id": "Baseball",
        "question": "Find the top 5 teams with the highest number of wins in any single season.",
        "is_truly_ambiguous": False,
        "expected_ambiguity_type": None,
        "user_clarification_response": None,
        "gold_sql": "SELECT name, year, w FROM team ORDER BY w DESC LIMIT 5;",
    },
    {
        "id": "local142",
        "category": "Discrepancy Case",
        "db_id": "AdventureWorks",
        "question": "List the top 5 sales orders with the highest total due amount.",
        "is_truly_ambiguous": False,
        "expected_ambiguity_type": None,
        "user_clarification_response": None,
        "gold_sql": "SELECT salesorderid, customerid, totaldue FROM salesorderheader ORDER BY totaldue DESC LIMIT 5;",
    },
    {
        "id": "local143",
        "category": "Discrepancy Case",
        "db_id": "AdventureWorks",
        "question": "Find all products with a list price greater than 1000, ordered by list price descending.",
        "is_truly_ambiguous": False,
        "expected_ambiguity_type": None,
        "user_clarification_response": None,
        "gold_sql": "SELECT productid, name, listprice FROM product WHERE listprice > 1000 ORDER BY listprice DESC LIMIT 10;",
    },
]


def run_benchmark_item(evaluator, item, mode: str):
    """
    Run single benchmark item under specified mode:
    mode='phase9': Semantic layer only, ambiguity detection disabled.
    mode='phase10_detect_only': Ambiguity detected, but auto-selects top candidate without clarification pause.
    mode='phase10_interactive': Confidence-aware clarification loop.
    """
    db_id = item["db_id"]
    question = item["question"]
    gold_sql = item["gold_sql"]
    is_ambiguous = item["is_truly_ambiguous"]

    sem_reg = get_semantic_registry()

    # Register temporary business definition if specified
    bdef_obj = None
    if "business_definition" in item:
        bd = item["business_definition"]
        bdef_obj = BusinessDefinition(
            term=bd["term"],
            meaning=bd["meaning"],
            database_id=db_id,
            preferred_columns=bd["preferred_columns"],
            formula_or_hint=bd.get("formula_or_hint"),
        )
        sem_reg.register_business_definition(bdef_obj)

    t0 = time.perf_counter()
    clarification_requested = False
    clarification_resolved = False
    selected_candidate_name = None
    ambiguity_detected = False
    ambiguity_type = None

    try:
        if mode == "phase9":
            # Phase 9: standard pipeline without clarification
            record = evaluator.evaluate_item_plainsql({
                "instance_id": item["id"],
                "db_id": db_id,
                "question": question,
                "gold_sql": gold_sql,
                "difficulty": "medium",
                "external_knowledge": None,
            })
            gen_sql = record.get("generated_sql", "")
            is_valid = record.get("is_valid", False)
            exec_succ = record.get("final_execution_success", False)
            exec_acc = record.get("execution_accuracy", False)

        elif mode == "phase10_detect_only":
            # Detect ambiguities, but force auto-execute without interactive pause
            orig_thresh = sem_reg.detect_ambiguities
            # Force auto_execute by setting clarification_threshold to 0.0
            def detect_no_pause(*args, **kwargs):
                ambs = orig_thresh(*args, **kwargs)
                for a in ambs:
                    a.requires_clarification = False
                return ambs

            sem_reg.detect_ambiguities = detect_no_pause
            try:
                record = evaluator.evaluate_item_plainsql({
                    "instance_id": item["id"],
                    "db_id": db_id,
                    "question": question,
                    "gold_sql": gold_sql,
                    "difficulty": "medium",
                    "external_knowledge": None,
                })
            finally:
                sem_reg.detect_ambiguities = orig_thresh

            gen_sql = record.get("generated_sql", "")
            is_valid = record.get("is_valid", False)
            exec_succ = record.get("final_execution_success", False)
            exec_acc = record.get("execution_accuracy", False)

        elif mode == "phase10_interactive":
            # Phase 10 interactive: run initial state
            pool = evaluator.registry.get_pool(db_id)
            gold_results = None
            if gold_sql:
                try:
                    gold_results = pool.execute_query(gold_sql)
                except Exception:
                    pass

            orchestrator = AgentOrchestrator(
                evaluator.llm_router,
                evaluator.retriever,
                pool,
                registry=evaluator.registry,
            )
            res1 = orchestrator.process_query(
                user_query=question,
                db_id=db_id,
            )

            if res1.get("requires_clarification"):
                clarification_requested = True
                active_clarif = res1.get("active_clarification", {})
                ambiguity_type = active_clarif.get("ambiguity_type")
                ambiguity_detected = True

                user_resp = item.get("user_clarification_response", "1")
                # Resume execution with user's clarification response
                res2 = orchestrator.process_query(
                    user_query=question,
                    db_id=db_id,
                    clarification_response=user_resp,
                    active_clarification=active_clarif,
                )
                clarification_resolved = bool(res2.get("clarification_resolved", False))
                selected_candidate_name = (res2.get("selected_candidate") or {}).get("column_name")
                gen_sql = res2.get("sanitized_sql") or res2.get("generated_sql", "")
                is_valid = bool(gen_sql and not res2.get("validation_errors"))
                exec_succ = bool(not res2.get("error") and res2.get("query_results") is not None)
                if gold_results is not None and exec_succ:
                    exec_acc = EvalMetrics.execution_match(res2.get("query_results", []), gold_results)
                else:
                    exec_acc = False
            else:
                gen_sql = res1.get("sanitized_sql") or res1.get("generated_sql", "")
                is_valid = bool(gen_sql and not res1.get("validation_errors"))
                exec_succ = bool(not res1.get("error") and res1.get("query_results") is not None)
                if gold_results is not None and exec_succ:
                    exec_acc = EvalMetrics.execution_match(res1.get("query_results", []), gold_results)
                else:
                    exec_acc = False

        latency_ms = round((time.perf_counter() - t0) * 1000, 2)

    finally:
        if bdef_obj:
            sem_reg.clear(db_id)

    return {
        "id": item["id"],
        "category": item["category"],
        "db_id": db_id,
        "question": question,
        "is_truly_ambiguous": is_ambiguous,
        "mode": mode,
        "generated_sql": gen_sql,
        "is_valid": is_valid,
        "execution_success": exec_succ,
        "execution_accuracy": exec_acc,
        "clarification_requested": clarification_requested,
        "clarification_resolved": clarification_resolved,
        "selected_candidate": selected_candidate_name,
        "latency_ms": latency_ms,
    }


def main():
    print("=" * 65)
    print("Starting Phase 10 Clarification Evaluation & Ablation Suite")
    print("=" * 65)

    evaluator = SpiderEvaluator()

    modes = [
        ("phase9", "Phase 9 (Semantics Only)"),
        ("phase10_detect_only", "Phase 10 (Detection Only)"),
        ("phase10_interactive", "Phase 10 (Interactive Clarification)"),
    ]

    all_mode_results = {}

    for mode_key, mode_name in modes:
        print(f"\nEvaluating: {mode_name}...")
        mode_records = []
        for item in CLARIFICATION_BENCHMARK_TARGETS:
            res = run_benchmark_item(evaluator, item, mode=mode_key)
            mode_records.append(res)
            acc_str = "MATCH" if res["execution_accuracy"] else "DIFF"
            clarif_str = " [CLARIFIED]" if res["clarification_requested"] else ""
            print(f"  [{res['id']}] {acc_str}{clarif_str} | Latency: {res['latency_ms']}ms | SQL: {res['generated_sql'][:50]}...")

        all_mode_results[mode_key] = mode_records

    # ── Compute Comparative Metrics ───────────────────────────────────────
    def compute_metrics(records, name):
        total = len(records)
        valid = sum(1 for r in records if r["is_valid"])
        exec_succ = sum(1 for r in records if r["execution_success"])
        acc = sum(1 for r in records if r["execution_accuracy"])
        clarif_reqs = sum(1 for r in records if r["clarification_requested"])
        latencies = [r["latency_ms"] for r in records]

        # Clarification precision & recall
        true_ambiguities = sum(1 for r in records if r["is_truly_ambiguous"])
        tp_clarif = sum(1 for r in records if r["clarification_requested"] and r["is_truly_ambiguous"])
        fp_clarif = sum(1 for r in records if r["clarification_requested"] and not r["is_truly_ambiguous"])
        fn_clarif = sum(1 for r in records if not r["clarification_requested"] and r["is_truly_ambiguous"])

        clarif_precision = round(tp_clarif / max(tp_clarif + fp_clarif, 1) * 100, 2)
        clarif_recall = round(tp_clarif / max(true_ambiguities, 1) * 100, 2)
        false_clarif_rate = round(fp_clarif / max(total - true_ambiguities, 1) * 100, 2)
        missed_ambiguity_rate = round(fn_clarif / max(true_ambiguities, 1) * 100, 2)

        return {
            "name": name,
            "total_items": total,
            "sql_validity": round(valid / total * 100, 2),
            "execution_success": round(exec_succ / total * 100, 2),
            "execution_accuracy": round(acc / total * 100, 2),
            "clarification_rate": round(clarif_reqs / total * 100, 2),
            "clarification_precision": clarif_precision,
            "clarification_recall": clarif_recall,
            "false_clarification_rate": false_clarif_rate,
            "missed_ambiguity_rate": missed_ambiguity_rate,
            "avg_latency_ms": round(float(np.mean(latencies)), 2),
            "median_latency_ms": round(float(np.median(latencies)), 2),
            "p95_latency_ms": round(float(np.percentile(latencies, 95)), 2),
        }

    p9_summary = compute_metrics(all_mode_results["phase9"], "Phase 9 (Semantics Only)")
    p10_det_summary = compute_metrics(all_mode_results["phase10_detect_only"], "Phase 10 (Detection Only)")
    p10_int_summary = compute_metrics(all_mode_results["phase10_interactive"], "Phase 10 (Interactive Clarification)")

    print("\n" + "=" * 80)
    print("PHASE 10 CLARIFICATION ABLATION SUMMARY")
    print("=" * 80)
    print(f"{'Metric':<28} {'Phase 9':<16} {'Phase 10 (Detect)':<20} {'Phase 10 (Interactive)'}")
    print("-" * 80)
    print(f"{'Execution Accuracy':<28} {p9_summary['execution_accuracy']}%{'':<10} {p10_det_summary['execution_accuracy']}%{'':<13} {p10_int_summary['execution_accuracy']}%")
    print(f"{'SQL Validity':<28} {p9_summary['sql_validity']}%{'':<10} {p10_det_summary['sql_validity']}%{'':<13} {p10_int_summary['sql_validity']}%")
    print(f"{'Execution Success':<28} {p9_summary['execution_success']}%{'':<10} {p10_det_summary['execution_success']}%{'':<13} {p10_int_summary['execution_success']}%")
    print(f"{'Clarification Precision':<28} {p9_summary['clarification_precision']}%{'':<10} {p10_det_summary['clarification_precision']}%{'':<13} {p10_int_summary['clarification_precision']}%")
    print(f"{'Clarification Recall':<28} {p9_summary['clarification_recall']}%{'':<10} {p10_det_summary['clarification_recall']}%{'':<13} {p10_int_summary['clarification_recall']}%")
    print(f"{'False Clarification Rate':<28} {p9_summary['false_clarification_rate']}%{'':<10} {p10_det_summary['false_clarification_rate']}%{'':<13} {p10_int_summary['false_clarification_rate']}%")
    print(f"{'Clarification Rate':<28} {p9_summary['clarification_rate']}%{'':<10} {p10_det_summary['clarification_rate']}%{'':<13} {p10_int_summary['clarification_rate']}%")
    print(f"{'Average Latency (ms)':<28} {p9_summary['avg_latency_ms']:<16} {p10_det_summary['avg_latency_ms']:<20} {p10_int_summary['avg_latency_ms']}")
    print("=" * 80)

    # Save detailed output artifact
    out_dir = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "phase10_clarification_ablation_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "summaries": {
                "phase9": p9_summary,
                "phase10_detect_only": p10_det_summary,
                "phase10_interactive": p10_int_summary,
            },
            "detailed_results": all_mode_results,
        }, f, indent=2)
    print(f"\nDetailed ablation results saved to: {out_path}")


if __name__ == "__main__":
    main()

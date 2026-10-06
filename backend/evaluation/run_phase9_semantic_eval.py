"""
Phase 9 Semantic Evaluation & Ablation Suite.

Evaluates semantic column disambiguation on targeted semantic queries
and measures:
- Baseline (Schema RAG only, without semantic layer)
vs.
- Phase 9 (Schema RAG + Business Semantic Layer)

Measures:
- SQL Validity
- Execution Success
- Execution Accuracy
- Schema Table Recall
- Latency (Average, Median, P95)
- Semantic Correctness on ambiguous column pairs
"""

import os
import sys
import time
import json
import sqlite3
import numpy as np
import structlog

# Ensure backend root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings
from app.db.registry import get_database_registry
from app.llm.router import LLMRouter
from app.rag.retriever import HybridRetriever
from app.agents.orchestrator import AgentOrchestrator
from app.agents.state import AgentState
from evaluation.runner import EvalMetrics

logger = structlog.get_logger()

# ── Semantic Evaluation Dataset ──────────────────────────────────────────
SEMANTIC_EVAL_TARGETS = [
    {
        "id": "sem_A_status_ambiguity",
        "category": "Status Ambiguity",
        "db_id": "E_commerce",
        "question": "How many delivered orders are there?",
        "gold_sql": "SELECT COUNT(*) FROM orders WHERE order_status = 'delivered';",
    },
    {
        "id": "sem_B_delivery_timestamp",
        "category": "Delivery Timestamp",
        "db_id": "E_commerce",
        "question": "How many orders have a delivery date?",
        "gold_sql": "SELECT COUNT(*) FROM orders WHERE order_delivered_customer_date IS NOT NULL;",
    },
    {
        "id": "sem_C_revenue",
        "category": "Revenue",
        "db_id": "E_commerce",
        "question": "What is total revenue?",
        "gold_sql": "SELECT SUM(price) FROM order_items;",
    },
    {
        "id": "sem_D_unit_volume",
        "category": "Unit Volume",
        "db_id": "E_commerce",
        "question": "What is the total number of items sold?",
        "gold_sql": "SELECT COUNT(order_item_id) FROM order_items;",
    },
    {
        "id": "sem_E_temporal_meaning",
        "category": "Temporal Meaning",
        "db_id": "E_commerce",
        "question": "Show sales by year.",
        "gold_sql": "SELECT STRFTIME('%Y', o.order_purchase_timestamp) AS year, SUM(oi.price) AS total_sales FROM orders o JOIN order_items oi ON o.order_id = oi.order_id GROUP BY year ORDER BY year;",
    },
    {
        "id": "sem_F_category_meaning",
        "category": "Category Meaning",
        "db_id": "E_commerce",
        "question": "Which product category has the most sales?",
        "gold_sql": "SELECT p.product_category_name, SUM(oi.price) AS total_sales FROM products p JOIN order_items oi ON p.product_id = oi.product_id WHERE p.product_category_name IS NOT NULL GROUP BY p.product_category_name ORDER BY total_sales DESC LIMIT 1;",
    },
    {
        "id": "local005",
        "category": "Phase 8 Ambiguity Case",
        "db_id": "E_commerce",
        "question": "Find the average freight value and average price for delivered orders.",
        "gold_sql": "SELECT AVG(oi.price) AS avg_price, AVG(oi.freight_value) AS avg_freight FROM order_items oi JOIN orders o ON oi.order_id = o.order_id WHERE o.order_status = 'delivered';",
    },
    {
        "id": "local006",
        "category": "Phase 8 Verified Case",
        "db_id": "E_commerce",
        "question": "What are the top 3 product categories by total item sales volume?",
        "gold_sql": "SELECT p.product_category_name, COUNT(oi.order_item_id) AS total_sold FROM order_items oi JOIN products p ON oi.product_id = p.product_id WHERE p.product_category_name IS NOT NULL GROUP BY p.product_category_name ORDER BY total_sold DESC LIMIT 3;",
    },
    {
        "id": "local008",
        "category": "Phase 8 Verified Case",
        "db_id": "Baseball",
        "question": "Find the top 5 teams with the highest number of wins in any single season.",
        "gold_sql": "SELECT name, year, w FROM team ORDER BY w DESC LIMIT 5;",
    },
    {
        "id": "local142",
        "category": "Discrepancy Case",
        "db_id": "AdventureWorks",
        "question": "List the top 5 sales orders with the highest total due amount.",
        "gold_sql": "SELECT salesorderid, customerid, totaldue FROM salesorderheader ORDER BY totaldue DESC LIMIT 5;",
    },
    {
        "id": "local143",
        "category": "Discrepancy Case",
        "db_id": "AdventureWorks",
        "question": "Find all products with a list price greater than 1000, ordered by list price descending.",
        "gold_sql": "SELECT productid, name, listprice FROM product WHERE listprice > 1000 ORDER BY listprice DESC LIMIT 10;",
    },
]


def run_single_eval(evaluator, item, enable_semantics: bool = True):
    """Run single evaluation item with or without semantic layer."""
    db_id = item["db_id"]
    question = item["question"]
    gold_sql = item["gold_sql"]

    example = {
        "instance_id": item["id"],
        "db_id": db_id,
        "question": question,
        "gold_sql": gold_sql,
        "difficulty": "medium",
        "external_knowledge": None,
    }

    from app.semantics.registry import get_semantic_registry
    sem_reg = get_semantic_registry()
    orig_method = sem_reg.get_semantic_context

    if not enable_semantics:
        sem_reg.get_semantic_context = lambda *args, **kwargs: ""

    try:
        record = evaluator.evaluate_item_plainsql(example)
    finally:
        sem_reg.get_semantic_context = orig_method

    return {
        "id": item["id"],
        "category": item["category"],
        "db_id": db_id,
        "question": question,
        "gold_sql": gold_sql,
        "generated_sql": record.get("generated_sql", ""),
        "is_valid": record.get("is_valid", False),
        "execution_success": record.get("final_execution_success", False),
        "execution_accuracy": record.get("execution_accuracy", False),
        "latency_ms": record.get("latency_ms", 0.0),
        "error": record.get("error"),
    }


def main():
    print("=" * 60)
    print("Starting Phase 9 Semantic Evaluation & Ablation Suite")
    print("=" * 60)

    from evaluation.spider_eval import SpiderEvaluator
    evaluator = SpiderEvaluator()

    # ── 1. BASELINE RUN (Schema RAG Only) ────────────────────────
    print("\n[1/2] Running BASELINE (Schema RAG Only, Semantics Disabled)...")
    baseline_results = []
    for item in SEMANTIC_EVAL_TARGETS:
        res = run_single_eval(evaluator, item, enable_semantics=False)
        baseline_results.append(res)
        status = "MATCH" if res["execution_accuracy"] else "DIFF"
        print(f"  [{res['id']}] {status} | Latency: {res['latency_ms']}ms | SQL: {res['generated_sql'][:60]}...")

    # ── 2. PHASE 9 RUN (Schema RAG + Business Semantic Layer) ────
    print("\n[2/2] Running PHASE 9 (Schema RAG + Business Semantic Layer)...")
    phase9_results = []
    for item in SEMANTIC_EVAL_TARGETS:
        res = run_single_eval(evaluator, item, enable_semantics=True)
        phase9_results.append(res)
        status = "MATCH" if res["execution_accuracy"] else "DIFF"
        print(f"  [{res['id']}] {status} | Latency: {res['latency_ms']}ms | SQL: {res['generated_sql'][:60]}...")

    # ── 3. SUMMARY METRICS ───────────────────────────────────────
    def summarize(results, name):
        total = len(results)
        valid = sum(1 for r in results if r["is_valid"])
        exec_succ = sum(1 for r in results if r["execution_success"])
        acc = sum(1 for r in results if r["execution_accuracy"])
        latencies = [r["latency_ms"] for r in results]

        return {
            "name": name,
            "total": total,
            "sql_validity": round(valid / total * 100, 2),
            "execution_success": round(exec_succ / total * 100, 2),
            "execution_accuracy": round(acc / total * 100, 2),
            "avg_latency_ms": round(float(np.mean(latencies)), 2),
            "median_latency_ms": round(float(np.median(latencies)), 2),
            "p95_latency_ms": round(float(np.percentile(latencies, 95)), 2),
        }

    base_summary = summarize(baseline_results, "Baseline (Schema RAG Only)")
    p9_summary = summarize(phase9_results, "Phase 9 (Schema RAG + Semantic Layer)")

    print("\n" + "=" * 60)
    print("ABLATION RESULTS SUMMARY")
    print("=" * 60)
    print(f"Metric                    Baseline          Phase 9 (Semantics)")
    print(f"----------------------------------------------------------------")
    print(f"SQL Validity              {base_summary['sql_validity']}%            {p9_summary['sql_validity']}%")
    print(f"Execution Success         {base_summary['execution_success']}%            {p9_summary['execution_success']}%")
    print(f"Execution Accuracy        {base_summary['execution_accuracy']}%            {p9_summary['execution_accuracy']}%")
    print(f"Average Latency (ms)      {base_summary['avg_latency_ms']}        {p9_summary['avg_latency_ms']}")
    print(f"Median Latency (ms)       {base_summary['median_latency_ms']}        {p9_summary['median_latency_ms']}")
    print(f"P95 Latency (ms)          {base_summary['p95_latency_ms']}        {p9_summary['p95_latency_ms']}")
    print("=" * 60)

    # Save detailed artifact
    out_dir = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "phase9_semantic_ablation_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "baseline_summary": base_summary,
            "phase9_summary": p9_summary,
            "baseline_items": baseline_results,
            "phase9_items": phase9_results,
        }, f, indent=2)
    print(f"Detailed ablation results saved to: {out_path}")


if __name__ == "__main__":
    main()

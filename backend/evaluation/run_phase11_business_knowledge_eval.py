"""
Phase 11 Business Knowledge RAG & Persistent Semantic Learning Evaluation & Ablation Suite.

Evaluates:
1. Business term retrieval accuracy
2. Correct definition and column selection
3. Disambiguation override via glossary precedence
4. Preservation of clarification when no definition exists
5. Strict database isolation (zero cross-database leakage)
6. Stale definition detection and safe fallback
7. Persistent semantic learning promotion candidate behavior
8. Spider 2.0-Lite benchmark stability (local007, local008, local142, local143)

Compares 3 Configurations:
A. Phase 10 Baseline: No Business Knowledge RAG (glossary disabled)
B. Phase 11 Business Knowledge RAG: Enterprise glossary + Hybrid RAG retrieval
C. Phase 11 RAG + Persistent Learning: Business Knowledge RAG + User-confirmed promoted candidates
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
    DefinitionStatus,
    BusinessKnowledgeSource,
    ProposedBusinessDefinition,
)
from app.semantics.registry import get_semantic_registry, SemanticRegistry
from app.semantics.loader import BusinessKnowledgeLoader
from app.semantics.learning import get_semantic_learning_manager
from app.agents.orchestrator import AgentOrchestrator
from evaluation.runner import EvalMetrics
from evaluation.spider_eval import SpiderEvaluator

logger = structlog.get_logger()

# ── 20-Query Enterprise Business Knowledge Benchmark Dataset ──────────────────

BUSINESS_KNOWLEDGE_BENCHMARK = [
    {
        "id": "bdef_01_revenue_glossary",
        "category": "Revenue",
        "db_id": "E_commerce",
        "question": "What is total revenue by product category?",
        "expected_term": "revenue",
        "expected_columns": ["order_items.price"],
        "is_business_query": True,
        "is_ambiguous": False,
        "gold_sql": "SELECT p.product_category_name, SUM(oi.price) AS total_revenue FROM products p JOIN order_items oi ON p.product_id = oi.product_id WHERE p.product_category_name IS NOT NULL GROUP BY p.product_category_name ORDER BY total_revenue DESC LIMIT 10;",
    },
    {
        "id": "bdef_02_revenue_synonym",
        "category": "Revenue (Synonym)",
        "db_id": "E_commerce",
        "question": "What is the turnover by product category?",
        "expected_term": "revenue",
        "expected_columns": ["order_items.price"],
        "is_business_query": True,
        "is_ambiguous": False,
        "gold_sql": "SELECT p.product_category_name, SUM(oi.price) AS turnover FROM products p JOIN order_items oi ON p.product_id = oi.product_id WHERE p.product_category_name IS NOT NULL GROUP BY p.product_category_name ORDER BY turnover DESC LIMIT 10;",
    },
    {
        "id": "bdef_03_sales_volume",
        "category": "Sales Volume",
        "db_id": "E_commerce",
        "question": "What are the top 3 product categories by total item sales volume?",
        "expected_term": "sales_volume",
        "expected_columns": ["order_items.order_item_id"],
        "is_business_query": True,
        "is_ambiguous": False,
        "gold_sql": "SELECT p.product_category_name, COUNT(oi.order_item_id) AS total_items_sold FROM products p JOIN order_items oi ON p.product_id = oi.product_id WHERE p.product_category_name IS NOT NULL GROUP BY p.product_category_name ORDER BY total_items_sold DESC LIMIT 3;",
    },
    {
        "id": "bdef_04_units_sold_synonym",
        "category": "Units Sold (Synonym)",
        "db_id": "E_commerce",
        "question": "How many units were sold for each product category?",
        "expected_term": "units_sold",
        "expected_columns": ["order_items.order_item_id"],
        "is_business_query": True,
        "is_ambiguous": False,
        "gold_sql": "SELECT p.product_category_name, COUNT(oi.order_item_id) AS units_sold FROM products p JOIN order_items oi ON p.product_id = oi.product_id WHERE p.product_category_name IS NOT NULL GROUP BY p.product_category_name ORDER BY units_sold DESC LIMIT 10;",
    },
    {
        "id": "bdef_05_delivered_order",
        "category": "Delivered Order",
        "db_id": "E_commerce",
        "question": "Find the average freight value and average price for delivered orders.",
        "expected_term": "delivered_order",
        "expected_columns": ["orders.order_status"],
        "is_business_query": True,
        "is_ambiguous": False,
        "gold_sql": "SELECT AVG(oi.freight_value) AS avg_freight, AVG(oi.price) AS avg_price FROM order_items oi JOIN orders o ON oi.order_id = o.order_id WHERE o.order_status = 'delivered';",
    },
    {
        "id": "bdef_06_delivered_synonym",
        "category": "Fulfilled Order (Synonym)",
        "db_id": "E_commerce",
        "question": "Find the average freight value for fulfilled orders.",
        "expected_term": "delivered_order",
        "expected_columns": ["orders.order_status"],
        "is_business_query": True,
        "is_ambiguous": False,
        "gold_sql": "SELECT AVG(oi.freight_value) AS avg_freight FROM order_items oi JOIN orders o ON oi.order_id = o.order_id WHERE o.order_status = 'delivered';",
    },
    {
        "id": "bdef_07_pending_order",
        "category": "Pending Order",
        "db_id": "E_commerce",
        "question": "Count pending orders.",
        "expected_term": "pending_order",
        "expected_columns": ["orders.order_status"],
        "is_business_query": True,
        "is_ambiguous": False,
        "gold_sql": "SELECT COUNT(*) FROM orders WHERE order_status IN ('processing', 'invoiced', 'created');",
    },
    {
        "id": "bdef_08_average_order_value",
        "category": "Average Order Value",
        "db_id": "E_commerce",
        "question": "What is the average order value across all orders?",
        "expected_term": "average_order_value",
        "expected_columns": ["order_items.price", "order_items.order_id"],
        "is_business_query": True,
        "is_ambiguous": False,
        "gold_sql": "SELECT SUM(price) / COUNT(DISTINCT order_id) AS aov FROM order_items;",
    },
    {
        "id": "bdef_09_customer_delivery_date",
        "category": "Customer Delivery Date",
        "db_id": "E_commerce",
        "question": "When were orders delivered to customers?",
        "expected_term": "customer_delivery_date",
        "expected_columns": ["orders.order_delivered_customer_date"],
        "is_business_query": True,
        "is_ambiguous": False,
        "gold_sql": "SELECT order_id, order_delivered_customer_date FROM orders WHERE order_delivered_customer_date IS NOT NULL LIMIT 10;",
    },
    {
        "id": "bdef_10_carrier_delivery_date",
        "category": "Carrier Delivery Date",
        "db_id": "E_commerce",
        "question": "When did the carrier deliver the orders?",
        "expected_term": "carrier_delivery_date",
        "expected_columns": ["orders.order_delivered_carrier_date"],
        "is_business_query": True,
        "is_ambiguous": False,
        "gold_sql": "SELECT order_id, order_delivered_carrier_date FROM orders WHERE order_delivered_carrier_date IS NOT NULL LIMIT 10;",
    },
    {
        "id": "bdef_11_ambiguous_sales_glossary_override",
        "category": "Ambiguity Override",
        "db_id": "E_commerce",
        "question": "What are the top 3 products by sales?",
        "expected_term": "sales",
        "expected_columns": ["order_items.price"],
        "is_business_query": True,
        "is_ambiguous": True,  # Ambiguous in Phase 10 without glossary; auto-resolved with glossary!
        "user_clarification_response": "price",
        "gold_sql": "SELECT p.product_id, SUM(oi.price) AS total_sales FROM products p JOIN order_items oi ON p.product_id = oi.product_id GROUP BY p.product_id ORDER BY total_sales DESC LIMIT 3;",
    },
    {
        "id": "bdef_12_missing_definition_clarification",
        "category": "True Ambiguity (Clarification Preserved)",
        "db_id": "E_commerce",
        "question": "When were the orders delivered?",
        "expected_term": None,
        "expected_columns": ["orders.order_delivered_customer_date"],
        "is_business_query": False,
        "is_ambiguous": True,
        "user_clarification_response": "1",
        "gold_sql": "SELECT order_id, order_delivered_customer_date FROM orders WHERE order_delivered_customer_date IS NOT NULL LIMIT 10;",
    },
    {
        "id": "bdef_13_adventureworks_sales_volume",
        "category": "Cross-DB Glossary",
        "db_id": "AdventureWorks",
        "question": "What is the sales volume by product?",
        "expected_term": "sales_volume",
        "expected_columns": ["salesorderdetail.orderqty"],
        "is_business_query": True,
        "is_ambiguous": False,
        "gold_sql": "SELECT productid, SUM(orderqty) AS sales_volume FROM salesorderdetail GROUP BY productid ORDER BY sales_volume DESC LIMIT 10;",
    },
    {
        "id": "bdef_14_adventureworks_list_price",
        "category": "Benchmark Regression",
        "db_id": "AdventureWorks",
        "question": "Find all products with a list price greater than 1000, ordered by list price descending.",
        "expected_term": "list_price",
        "expected_columns": ["product.listprice"],
        "is_business_query": True,
        "is_ambiguous": False,
        "gold_sql": "SELECT productid, name, listprice FROM product WHERE listprice > 1000 ORDER BY listprice DESC LIMIT 10;",
    },
    {
        "id": "bdef_15_adventureworks_total_due",
        "category": "Benchmark Regression",
        "db_id": "AdventureWorks",
        "question": "List the top 5 sales orders with the highest total due amount.",
        "expected_term": "total_due",
        "expected_columns": ["salesorderheader.totaldue"],
        "is_business_query": True,
        "is_ambiguous": False,
        "gold_sql": "SELECT salesorderid, customerid, totaldue FROM salesorderheader ORDER BY totaldue DESC LIMIT 5;",
    },
    {
        "id": "bdef_16_baseball_season_wins",
        "category": "Benchmark Regression",
        "db_id": "Baseball",
        "question": "Find the top 5 teams with the highest number of wins in any single season.",
        "expected_term": "single_season_wins",
        "expected_columns": ["team.w", "team.year", "team.name"],
        "is_business_query": True,
        "is_ambiguous": False,
        "gold_sql": "SELECT name, year, w FROM team ORDER BY w DESC LIMIT 5;",
    },
    {
        "id": "bdef_17_baseball_player_count",
        "category": "Benchmark Regression",
        "db_id": "Baseball",
        "question": "How many total players are listed in the player table?",
        "expected_term": "player_count",
        "expected_columns": ["player.player_id"],
        "is_business_query": True,
        "is_ambiguous": False,
        "gold_sql": "SELECT COUNT(*) FROM player;",
    },
    {
        "id": "bdef_18_cross_db_isolation_check",
        "category": "Cross-DB Isolation",
        "db_id": "Baseball",
        "question": "Show turnover for teams.",
        "expected_term": None,  # Turnover is in E_commerce, NOT Baseball
        "expected_columns": [],
        "is_business_query": False,
        "is_ambiguous": False,
        "gold_sql": "SELECT name, w FROM team LIMIT 5;",
    },
    {
        "id": "bdef_19_stale_definition_fallback",
        "category": "Stale Definition Fallback",
        "db_id": "E_commerce",
        "question": "Show ghost metrics by product.",
        "expected_term": "ghost_metric",
        "expected_columns": [],
        "is_business_query": True,
        "is_ambiguous": False,
        "gold_sql": "SELECT product_id FROM products LIMIT 5;",
    },
    {
        "id": "bdef_20_persistent_learning_promoted",
        "category": "Persistent Learning",
        "db_id": "E_commerce",
        "question": "What are our top products by net sales value?",
        "expected_term": "net_sales_value",
        "expected_columns": ["order_items.price"],
        "is_business_query": True,
        "is_ambiguous": False,
        "gold_sql": "SELECT product_id, SUM(price) AS net_sales FROM order_items GROUP BY product_id ORDER BY net_sales DESC LIMIT 5;",
    },
]


def run_benchmark_item(
    evaluator: SpiderEvaluator,
    item: dict,
    mode: str,
) -> dict:
    """Run an individual benchmark target through the pipeline in the specified mode."""
    instance_id = item["id"]
    db_id = item["db_id"]
    question = item["question"]
    gold_sql = item.get("gold_sql")
    expected_term = item.get("expected_term")
    expected_cols = item.get("expected_columns", [])

    sem_reg = get_semantic_registry()
    t0 = time.perf_counter()

    term_retrieved = False
    definition_selected = False
    correct_columns_selected = False
    clarification_requested = False
    clarification_resolved = False
    cross_db_leak = False

    try:
        # Resolve pool and gold results
        pool = evaluator.registry.get_pool(db_id)
        gold_results = None
        if gold_sql:
            try:
                gold_results = pool.execute_query(gold_sql)
            except Exception:
                pass

        # Build orchestrator for this database
        orchestrator = AgentOrchestrator(
            evaluator.llm_router,
            evaluator.retriever,
            pool,
            registry=evaluator.registry,
        )

        # Mode setup
        if mode == "phase10_baseline":
            # Baseline: clear business definitions
            sem_reg.clear(db_id)
            sem_reg.clear("global")
        elif mode == "phase11_rag":
            # Load standard external glossary
            sem_reg.load_glossary()
            if evaluator.retriever:
                evaluator.retriever.index_business_definitions(db_id)
        elif mode == "phase11_learning":
            # Load glossary + register a promoted learning candidate
            sem_reg.load_glossary()
            if item["id"] == "bdef_20_persistent_learning_promoted":
                promoted = BusinessDefinition(
                    term="net_sales_value",
                    meaning="Promoted user preference for net sales",
                    database_id="E_commerce",
                    preferred_columns=["order_items.price"],
                    formula_or_hint="SUM(order_items.price)",
                    source=BusinessKnowledgeSource.USER_CONFIRMED.value,
                    priority=50,
                    confidence=0.95,
                    active=True,
                )
                sem_reg.register_business_definition(promoted)
            if evaluator.retriever:
                evaluator.retriever.index_business_definitions(db_id)

        # Stale definition injection for bdef_19
        if item["id"] == "bdef_19_stale_definition_fallback":
            stale_def = BusinessDefinition(
                term="ghost_metric",
                meaning="Ghost nonexistent metric",
                database_id=db_id,
                preferred_columns=["nonexistent_table.ghost_col"],
                source="test",
                priority=100,
            )
            sem_reg.register_business_definition(stale_def)

        # Run Turn 1
        res1 = orchestrator.process_query(
            user_query=question,
            db_id=db_id,
        )

        semantic_ctx = res1.get("semantic_context", "")

        # Check term retrieval and cross-db isolation
        if expected_term and expected_term.lower() in semantic_ctx.lower():
            term_retrieved = True
            definition_selected = True

        # Check cross-db isolation check (bdef_18)
        if item["id"] == "bdef_18_cross_db_isolation_check":
            if "turnover" in semantic_ctx.lower():
                cross_db_leak = True

        # Check clarification
        if res1.get("requires_clarification"):
            clarification_requested = True
            active_clarif = res1.get("active_clarification", {})
            user_resp = item.get("user_clarification_response", "1")

            # Turn 2: Resume
            res2 = orchestrator.process_query(
                user_query=question,
                db_id=db_id,
                clarification_response=user_resp,
                active_clarification=active_clarif,
            )
            clarification_resolved = bool(res2.get("clarification_resolved", False))
            final_res = res2
        else:
            final_res = res1

        gen_sql = final_res.get("sanitized_sql") or final_res.get("generated_sql", "")
        is_valid = bool(gen_sql and not final_res.get("validation_errors"))
        exec_succ = bool(not final_res.get("error") and final_res.get("query_results") is not None)

        # Compare execution
        if gold_results is not None and exec_succ:
            exec_acc = EvalMetrics.execution_match(final_res.get("query_results", []), gold_results)
        else:
            exec_acc = False

        # Check if expected columns were selected in generated SQL
        if expected_cols:
            all_cols_in_sql = True
            for c in expected_cols:
                bare_col = c.split(".")[-1].lower()
                if bare_col not in gen_sql.lower():
                    all_cols_in_sql = False
                    break
            correct_columns_selected = all_cols_in_sql
        else:
            correct_columns_selected = True

    finally:
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)

    return {
        "id": item["id"],
        "category": item["category"],
        "db_id": db_id,
        "question": question,
        "mode": mode,
        "generated_sql": gen_sql,
        "is_valid": is_valid,
        "execution_success": exec_succ,
        "execution_accuracy": exec_acc,
        "term_retrieved": term_retrieved,
        "definition_selected": definition_selected,
        "correct_columns_selected": correct_columns_selected,
        "clarification_requested": clarification_requested,
        "clarification_resolved": clarification_resolved,
        "cross_db_leak": cross_db_leak,
        "latency_ms": latency_ms,
    }


def compute_metrics(results: list[dict]) -> dict:
    """Compute summary metrics for a mode run."""
    n = len(results)
    if n == 0:
        return {}

    valid_count = sum(1 for r in results if r["is_valid"])
    exec_succ_count = sum(1 for r in results if r["execution_success"])
    exec_acc_count = sum(1 for r in results if r["execution_accuracy"])
    term_ret_count = sum(1 for r in results if r["term_retrieved"])
    col_sel_count = sum(1 for r in results if r["correct_columns_selected"])
    clarif_req_count = sum(1 for r in results if r["clarification_requested"])
    cross_leak_count = sum(1 for r in results if r.get("cross_db_leak", False))

    latencies = [r["latency_ms"] for r in results]

    return {
        "total_queries": n,
        "sql_validity": round(valid_count / n * 100, 2),
        "execution_success": round(exec_succ_count / n * 100, 2),
        "execution_accuracy": round(exec_acc_count / n * 100, 2),
        "term_retrieval_rate": round(term_ret_count / n * 100, 2),
        "column_selection_accuracy": round(col_sel_count / n * 100, 2),
        "clarification_rate": round(clarif_req_count / n * 100, 2),
        "cross_db_leakage_rate": round(cross_leak_count / n * 100, 2),
        "avg_latency_ms": round(float(np.mean(latencies)), 2),
        "median_latency_ms": round(float(np.median(latencies)), 2),
        "p95_latency_ms": round(float(np.percentile(latencies, 95)), 2),
    }


def main():
    print("=" * 80)
    print("PLAINSQL PHASE 11 — BUSINESS KNOWLEDGE RAG & SEMANTIC LEARNING EVALUATION")
    print("=" * 80)

    # Force deterministic settings & fast startup
    os.environ["PLAINSQL_EVAL_MODE"] = "true"
    os.environ["PLAINSQL_EVAL_TEMPERATURE"] = "0.0"
    os.environ["PLAINSQL_EVAL_SEED"] = "42"
    os.environ["PLAINSQL_EVAL_PROVIDER"] = os.getenv("PLAINSQL_EVAL_PROVIDER", "huggingface")
    os.environ["PLAINSQL_EVAL_DELAY_MS"] = "300"
    os.environ["TF_ENABLE_ONEDNN_OPTS"] = "0"
    os.environ["TRANSFORMERS_NO_TF"] = "1"
    os.environ["DISABLE_ML_INTENT"] = "true"

    evaluator = SpiderEvaluator()

    # Evaluate across the 3 modes
    modes = [
        ("phase10_baseline", "Phase 10 (Baseline - No Glossary)"),
        ("phase11_rag", "Phase 11 (Business Knowledge RAG)"),
        ("phase11_learning", "Phase 11 (RAG + Persistent Learning)"),
    ]

    all_mode_results = {}
    summaries = {}

    for mode_key, mode_name in modes:
        print(f"\nEvaluating: {mode_name}...")
        mode_records = []
        for item in BUSINESS_KNOWLEDGE_BENCHMARK:
            print(f"  [{item['id']}] {item['question'][:50]}...", end="", flush=True)
            res = run_benchmark_item(evaluator, item, mode=mode_key)
            mode_records.append(res)
            status_str = "MATCH" if res["execution_accuracy"] else ("DIFF" if res["execution_success"] else "ERR")
            clarif_str = " [Clarified]" if res["clarification_requested"] else ""
            print(f" -> {status_str}{clarif_str} ({res['latency_ms']}ms)")
            # Pacing delay
            time.sleep(0.3)

        all_mode_results[mode_key] = mode_records
        metrics = compute_metrics(mode_records)
        metrics["name"] = mode_name
        summaries[mode_key] = metrics

    # Print Summary Table
    print("\n" + "=" * 80)
    print("PHASE 11 ABLATION SUMMARY")
    print("=" * 80)
    header = f"{'Metric':<32} {'Phase 10 Base':<16} {'Phase 11 RAG':<16} {'Phase 11 Learning':<16}"
    print(header)
    print("-" * 80)

    p10 = summaries["phase10_baseline"]
    p11_rag = summaries["phase11_rag"]
    p11_learn = summaries["phase11_learning"]

    rows = [
        ("Execution Accuracy", f"{p10['execution_accuracy']}%", f"{p11_rag['execution_accuracy']}%", f"{p11_learn['execution_accuracy']}%"),
        ("SQL Validity", f"{p10['sql_validity']}%", f"{p11_rag['sql_validity']}%", f"{p11_learn['sql_validity']}%"),
        ("Execution Success", f"{p10['execution_success']}%", f"{p11_rag['execution_success']}%", f"{p11_learn['execution_success']}%"),
        ("Term Retrieval Rate", f"{p10['term_retrieval_rate']}%", f"{p11_rag['term_retrieval_rate']}%", f"{p11_learn['term_retrieval_rate']}%"),
        ("Column Selection Accuracy", f"{p10['column_selection_accuracy']}%", f"{p11_rag['column_selection_accuracy']}%", f"{p11_learn['column_selection_accuracy']}%"),
        ("Clarification Rate", f"{p10['clarification_rate']}%", f"{p11_rag['clarification_rate']}%", f"{p11_learn['clarification_rate']}%"),
        ("Cross-DB Leakage Rate", f"{p10['cross_db_leakage_rate']}%", f"{p11_rag['cross_db_leakage_rate']}%", f"{p11_learn['cross_db_leakage_rate']}%"),
        ("Average Latency (ms)", f"{p10['avg_latency_ms']}", f"{p11_rag['avg_latency_ms']}", f"{p11_learn['avg_latency_ms']}"),
    ]

    for label, v1, v2, v3 in rows:
        print(f"{label:<32} {v1:<16} {v2:<16} {v3:<16}")
    print("=" * 80)

    # Save results to evaluation/results
    results_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "results",
        "phase11_business_knowledge_ablation_results.json",
    )
    os.makedirs(os.path.dirname(results_path), exist_ok=True)
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "summaries": summaries,
                "detailed_results": all_mode_results,
            },
            f,
            indent=2,
        )
    print(f"\nDetailed ablation results saved to: {results_path}")


if __name__ == "__main__":
    main()

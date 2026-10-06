"""Run Phase 6 Spider evaluation and save results."""
import sys
import os
import json
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0'

RESULTS_FILE = os.path.join(os.path.dirname(__file__), "phase6_eval_output.json")

try:
    from evaluation.spider_eval import SpiderEvaluator

    evaluator = SpiderEvaluator()
    output = evaluator.run_evaluation(
        db_id=None,
        limit=20,
        mode='plainsql',
        require_gold_sql=True,
    )

    # Extract results
    summary = output.get('summary', {})
    plainsql_records = output.get('plainsql_records', [])

    # Build per-instance report
    instance_details = []
    for r in plainsql_records:
        instance_details.append({
            "instance_id": r.get("instance_id"),
            "db_id": r.get("db_id"),
            "question": r.get("question", "")[:80],
            "is_valid": r.get("is_valid"),
            "initial_execution_success": r.get("initial_execution_success"),
            "repair_used": r.get("repair_used"),
            "final_execution_success": r.get("final_execution_success"),
            "execution_accuracy": r.get("execution_accuracy"),
            "table_recall": r.get("table_recall"),
            "table_precision": r.get("table_precision"),
            "latency_ms": r.get("latency_ms"),
            "failure_category": r.get("failure_category"),
            "generated_sql": r.get("generated_sql", "")[:200],
        })

    result = {
        "summary": summary,
        "instance_details": instance_details,
    }

    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str)

    print(f"SUCCESS: Results written to {RESULTS_FILE}")
    print(json.dumps(summary, indent=2, default=str))

except Exception as e:
    error_info = {"error": str(e), "traceback": traceback.format_exc()}
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(error_info, f, indent=2)
    print(f"ERROR: {e}")
    traceback.print_exc()
    sys.exit(1)

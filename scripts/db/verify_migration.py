#!/usr/bin/env python3
"""
scripts/db/verify_migration.py — Production Database Verification Suite.

Performs automated data validation, constraint checks, referential integrity
verification, and query execution tests against TiDB Cloud.

Usage:
    python scripts/db/verify_migration.py
"""

import sys
import os
import time

backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.config import get_settings
from app.db.connection import DatabasePool

EXPECTED_TABLES = [
    "accounts", "contacts", "conversations", "departments", "employees",
    "feature_catalog", "incidents", "invoices", "messages", "opportunities",
    "payments", "plainsql_users", "plans", "product_usage_daily", "products",
    "query_audit_log", "query_feedback", "subscriptions", "support_tickets",
    "ticket_events", "workspace_users", "workspaces",
]

# Baseline expected row counts
BASELINE_COUNTS = {
    "accounts": 120,
    "contacts": 360,
    "departments": 10,
    "employees": 80,
    "feature_catalog": 10,
    "incidents": 90,
    "invoices": 4320,
    "opportunities": 850,
    "payments": 4100,
    "plans": 4,
    "product_usage_daily": 12000,
    "products": 4,
    "query_audit_log": 2500,
    "subscriptions": 360,
    "support_tickets": 1600,
    "ticket_events": 3200,
    "workspace_users": 1800,
    "workspaces": 180,
}


def run_verification():
    settings = get_settings()
    pool = DatabasePool(settings.DB_URI)
    print("=" * 60)
    print("PLAINSQL TIDB CLOUD DATABASE VERIFICATION")
    print(f"Target Database: {pool.db_name} @ {pool.host}")
    print("=" * 60)

    results = {"tables_verified": 0, "failed_checks": 0, "passed_checks": 0}

    # ── 1. Table Existence & Row Counts ──
    print("\n[1] Verifying Tables and Row Counts:")
    live_tables = set(pool.get_tables())
    
    missing_tables = [t for t in EXPECTED_TABLES if t not in live_tables]
    if missing_tables:
        print(f"  [FAIL] Missing tables: {missing_tables}")
        results["failed_checks"] += 1
    else:
        print(f"  [PASS] All {len(EXPECTED_TABLES)} expected tables exist.")
        results["passed_checks"] += 1

    print("\n  Table Row Counts:")
    total_rows = 0
    for tbl in sorted(live_tables):
        cnt = pool.get_row_count(tbl)
        total_rows += cnt
        expected = BASELINE_COUNTS.get(tbl)
        status = "OK"
        if expected is not None:
            if cnt != expected:
                status = f"DIFF (expected {expected})"
                results["failed_checks"] += 1
            else:
                results["passed_checks"] += 1
        print(f"    - {tbl:<22}: {cnt:>6} rows [{status}]")
        results["tables_verified"] += 1

    print(f"\n  Total Production Rows: {total_rows:,}")

    # ── 2. Financial & Business Aggregates ──
    print("\n[2] Verifying Financial & Business Aggregates:")
    
    # Subscriptions Contracted ARR
    arr_res = pool.execute_query("""
        SELECT 
            COUNT(*) as total_subs,
            SUM(CASE WHEN status = 'active' THEN contracted_arr ELSE 0 END) as active_arr,
            SUM(contracted_arr) as total_arr
        FROM subscriptions
    """)
    if arr_res:
        row = arr_res[0]
        print(f"    - Subscriptions Active ARR: ${float(row['active_arr'] or 0):,.2f}")
        print(f"    - Subscriptions Total ARR:  ${float(row['total_arr'] or 0):,.2f}")
        assert float(row['active_arr']) > 0
        results["passed_checks"] += 1

    # Invoices Total
    inv_res = pool.execute_query("""
        SELECT 
            COUNT(*) as total_invoices,
            SUM(total) as gross_billed,
            MIN(invoice_date) as earliest_inv,
            MAX(invoice_date) as latest_inv
        FROM invoices
    """)
    if inv_res:
        row = inv_res[0]
        print(f"    - Gross Billed Invoices:    ${float(row['gross_billed'] or 0):,.2f}")
        print(f"    - Invoice Date Range:       {row['earliest_inv']} to {row['latest_inv']}")
        assert float(row['gross_billed']) > 0
        results["passed_checks"] += 1

    # Payments Total Succeeded
    pay_res = pool.execute_query("""
        SELECT 
            COUNT(*) as total_payments,
            SUM(CASE WHEN payment_status = 'succeeded' THEN amount ELSE 0 END) as net_collected
        FROM payments
    """)
    if pay_res:
        row = pay_res[0]
        print(f"    - Net Collected Payments:   ${float(row['net_collected'] or 0):,.2f}")
        assert float(row['net_collected']) > 0
        results["passed_checks"] += 1

    # Daily Product Usage
    usage_res = pool.execute_query("""
        SELECT 
            COUNT(*) as total_records,
            MIN(usage_date) as start_date,
            MAX(usage_date) as end_date,
            SUM(active_users) as total_active_user_days
        FROM product_usage_daily
    """)
    if usage_res:
        row = usage_res[0]
        print(f"    - Product Usage Range:      {row['start_date']} to {row['end_date']}")
        print(f"    - Total Active User Days:   {int(row['total_active_user_days'] or 0):,}")
        results["passed_checks"] += 1

    # ── 3. Referential Integrity (Foreign Keys) ──
    print("\n[3] Verifying Foreign Key Referential Integrity:")
    fk_checks = [
        ("contacts.account_id", "SELECT COUNT(*) as orphans FROM contacts c LEFT JOIN accounts a ON c.account_id = a.account_id WHERE a.account_id IS NULL"),
        ("employees.department_id", "SELECT COUNT(*) as orphans FROM employees e LEFT JOIN departments d ON e.department_id = d.department_id WHERE d.department_id IS NULL"),
        ("subscriptions.account_id", "SELECT COUNT(*) as orphans FROM subscriptions s LEFT JOIN accounts a ON s.account_id = a.account_id WHERE a.account_id IS NULL"),
        ("subscriptions.plan_id", "SELECT COUNT(*) as orphans FROM subscriptions s LEFT JOIN plans p ON s.plan_id = p.plan_id WHERE p.plan_id IS NULL"),
        ("invoices.subscription_id", "SELECT COUNT(*) as orphans FROM invoices i LEFT JOIN subscriptions s ON i.subscription_id = s.subscription_id WHERE s.subscription_id IS NULL"),
        ("payments.invoice_id", "SELECT COUNT(*) as orphans FROM payments p LEFT JOIN invoices i ON p.invoice_id = i.invoice_id WHERE i.invoice_id IS NULL"),
        ("ticket_events.ticket_id", "SELECT COUNT(*) as orphans FROM ticket_events te LEFT JOIN support_tickets st ON te.ticket_id = st.ticket_id WHERE st.ticket_id IS NULL"),
        ("feature_catalog.product_id", "SELECT COUNT(*) as orphans FROM feature_catalog fc LEFT JOIN products p ON fc.product_id = p.product_id WHERE p.product_id IS NULL"),
    ]

    for label, sql in fk_checks:
        res = pool.execute_query(sql)
        orphans = res[0]["orphans"] if res else 0
        if orphans == 0:
            print(f"    - FK {label:<28}: [PASS] 0 orphaned rows")
            results["passed_checks"] += 1
        else:
            print(f"    - FK {label:<28}: [FAIL] {orphans} orphaned rows!")
            results["failed_checks"] += 1

    # ── 4. TiDB Query Engine Compatibility ──
    print("\n[4] Verifying TiDB SQL Query Engine Compatibility:")
    test_queries = [
        ("Simple COUNT", "SELECT COUNT(*) as c FROM accounts"),
        ("JOIN & Filter", "SELECT a.account_name, p.plan_name FROM subscriptions s JOIN accounts a ON s.account_id = a.account_id JOIN plans p ON s.plan_id = p.plan_id LIMIT 5"),
        ("GROUP BY & Aggregation", "SELECT industry, COUNT(*) as cnt, AVG(health_score) as avg_score FROM accounts GROUP BY industry ORDER BY cnt DESC LIMIT 3"),
        ("Date Arithmetic", "SELECT account_id, created_at FROM accounts WHERE created_at >= DATE_SUB('2024-01-01', INTERVAL 1 YEAR) LIMIT 5"),
        ("CASE Statement (NRR)", "SELECT s.account_id, SUM(CASE WHEN s.status = 'active' THEN s.contracted_arr ELSE 0 END) as active_arr FROM subscriptions s GROUP BY s.account_id LIMIT 5"),
        ("Subquery", "SELECT product_name FROM products WHERE product_id IN (SELECT DISTINCT product_id FROM feature_catalog)"),
    ]

    for name, sql in test_queries:
        t0 = time.perf_counter()
        res = pool.execute_query(sql)
        elapsed = (time.perf_counter() - t0) * 1000
        print(f"    - {name:<26}: [PASS] {len(res)} rows in {elapsed:.1f}ms")
        results["passed_checks"] += 1

    # ── 5. Read-Only Guardrails Verification ──
    print("\n[5] Verifying Read-Only Guardrails:")
    destructive_attempts = [
        "DROP TABLE plainsql_test_drop",
        "TRUNCATE TABLE messages",
        "INSERT INTO departments (name, cost_center, region, annual_budget) VALUES ('X', 'Y', 'Z', 100)",
        "SELECT * FROM accounts; DROP TABLE accounts;",
        "UPDATE accounts SET health_score = 0",
    ]
    from app.agents.sql_validation import sql_validation_node
    for d_sql in destructive_attempts:
        state = {"generated_sql": d_sql, "trace_id": "test", "retry_count": 0}
        val_res = sql_validation_node(state)
        if not val_res.get("is_valid", False):
            errors = val_res.get("validation_errors", [])
            print(f"    - Guardrail blocked: '{d_sql[:35]}...' -> {errors[0] if errors else 'Blocked'}")
            results["passed_checks"] += 1
        else:
            print(f"    - [FAIL] Guardrail did NOT block destructive SQL: {d_sql}")
            results["failed_checks"] += 1

    print("\n" + "=" * 60)
    print(f"VERIFICATION SUMMARY: {results['passed_checks']} PASSED, {results['failed_checks']} FAILED")
    print("=" * 60)
    return results["failed_checks"] == 0


if __name__ == "__main__":
    success = run_verification()
    sys.exit(0 if success else 1)

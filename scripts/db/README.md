# DataPilot Database Migration & Verification Runbook

This directory contains reproducible scripts to introspect, export, and verify the production TiDB Cloud database.

## Architecture & Data Sources
- **Production Database**: TiDB Cloud Serverless (`chatbot` database, MySQL 8.0 wire-compatible)
- **Host**: `gateway01.ap-southeast-1.prod.alicloud.tidbcloud.com`
- **Port**: `4000`
- **Canonical Seed & Schema**: `db.sql` (18 domain tables) + application persistence tables (4 tables)
- **Total Tables**: 22 tables
- **Total Production Rows**: 31,061 rows

## Available Scripts

### 1. `export_schema.py`
Introspects the live database and exports table schemas, columns, constraints, and foreign keys into a JSON specification:
```bash
python scripts/db/export_schema.py --output scripts/db/schema_export.json
```

### 2. `verify_migration.py`
Executes an automated verification suite against the live TiDB Cloud instance:
- Verifies table presence across all 22 production tables.
- Validates row counts against baseline.
- Validates financial and usage aggregates (`invoices.total`, `payments.amount`, `subscriptions.contracted_arr`, `product_usage_daily`).
- Checks referential integrity across 8 core parent-child foreign key relationships.
- Runs representative DataPilot analytical queries (JOIN, GROUP BY, date arithmetic, subqueries, CASE expressions).
- Tests read-only SQL guardrails to ensure destructive statements (`DROP`, `TRUNCATE`, `INSERT`) are blocked.

```bash
python scripts/db/verify_migration.py
```

## Rollback Procedure
The production migration is strictly non-destructive. DataPilot does not drop or truncate tables. In the event of an operational issue:
1. Revert the backend application deployment on Railway/Docker to the prior release.
2. In the unlikely event of data corruption, TiDB Cloud Serverless maintains continuous Point-in-Time Recovery (PITR) logs and snapshots for instant recovery.

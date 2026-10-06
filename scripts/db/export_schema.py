#!/usr/bin/env python3
"""
scripts/db/export_schema.py — PlainSQL Database Schema Exporter.

Introspects the live database (TiDB Cloud / MySQL) and generates
a structured schema definition JSON and reference DDL summary.

Usage:
    python scripts/db/export_schema.py [--output schema_dump.json]
"""

import sys
import os
import json
import argparse
from datetime import datetime

# Add backend directory to sys.path
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.config import get_settings
from app.db.connection import DatabasePool


def export_schema(output_path: str = None) -> dict:
    """Introspect all tables, columns, keys, and foreign keys from TiDB."""
    settings = get_settings()
    pool = DatabasePool(settings.DB_URI)

    tables = sorted(pool.get_tables())
    schema_data = {
        "database": pool.db_name,
        "host": pool.host,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "total_tables": len(tables),
        "tables": {}
    }

    for table in tables:
        columns = pool.get_table_schema(table)
        fks = pool.get_foreign_keys(table)
        row_count = pool.get_row_count(table)

        schema_data["tables"][table] = {
            "row_count": row_count,
            "columns": columns,
            "foreign_keys": fks,
        }

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(schema_data, f, indent=2)
        print(f"Schema exported successfully to {output_path}")

    return schema_data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export TiDB Database Schema")
    parser.add_argument("--output", default="scripts/db/schema_export.json", help="Output file path")
    args = parser.parse_args()

    data = export_schema(args.output)
    print(f"Exported {data['total_tables']} tables from database '{data['database']}'.")

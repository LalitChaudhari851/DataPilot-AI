r"""
scripts/db/migrate_ecommerce_to_tidb.py -- Deploy Spider E_commerce to TiDB Cloud.

Migrates all 11 tables and 1,559,764 rows from:
    E:\Downloads\local_sqlite\E_commerce.sqlite
to the isolated TiDB Cloud database:
    ecommerce
"""

import sys
import os
import sqlite3
import time
from urllib.parse import urlparse, unquote
import ssl
import pymysql

backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.config import get_settings

SOURCE_SQLITE_PATH = r"E:\Downloads\local_sqlite\E_commerce.sqlite"


DDL_STATEMENTS = {
    "product_category_name_translation": """
        CREATE TABLE IF NOT EXISTS product_category_name_translation (
            product_category_name VARCHAR(64) PRIMARY KEY,
            product_category_name_english VARCHAR(64) NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "customers": """
        CREATE TABLE IF NOT EXISTS customers (
            customer_id VARCHAR(32) PRIMARY KEY,
            customer_unique_id VARCHAR(32) NOT NULL,
            customer_zip_code_prefix INT NOT NULL,
            customer_city VARCHAR(64) NOT NULL,
            customer_state VARCHAR(10) NOT NULL,
            INDEX idx_customer_city (customer_city),
            INDEX idx_customer_state (customer_state),
            INDEX idx_customer_unique (customer_unique_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "sellers": """
        CREATE TABLE IF NOT EXISTS sellers (
            seller_id VARCHAR(32) PRIMARY KEY,
            seller_zip_code_prefix INT NOT NULL,
            seller_city VARCHAR(64) NOT NULL,
            seller_state VARCHAR(10) NOT NULL,
            INDEX idx_seller_city (seller_city),
            INDEX idx_seller_state (seller_state)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "products": """
        CREATE TABLE IF NOT EXISTS products (
            product_id VARCHAR(32) PRIMARY KEY,
            product_category_name VARCHAR(64) NULL,
            product_name_lenght INT NULL,
            product_description_lenght INT NULL,
            product_photos_qty INT NULL,
            product_weight_g INT NULL,
            product_length_cm INT NULL,
            product_height_cm INT NULL,
            product_width_cm INT NULL,
            INDEX idx_prod_cat (product_category_name)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "orders": """
        CREATE TABLE IF NOT EXISTS orders (
            order_id VARCHAR(32) PRIMARY KEY,
            customer_id VARCHAR(32) NOT NULL,
            order_status VARCHAR(32) NOT NULL,
            order_purchase_timestamp DATETIME NOT NULL,
            order_approved_at DATETIME NULL,
            order_delivered_carrier_date DATETIME NULL,
            order_delivered_customer_date DATETIME NULL,
            order_estimated_delivery_date DATETIME NOT NULL,
            INDEX idx_order_customer (customer_id),
            INDEX idx_order_status (order_status),
            INDEX idx_order_purchase (order_purchase_timestamp)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "order_items": """
        CREATE TABLE IF NOT EXISTS order_items (
            order_id VARCHAR(32) NOT NULL,
            order_item_id INT NOT NULL,
            product_id VARCHAR(32) NOT NULL,
            seller_id VARCHAR(32) NOT NULL,
            shipping_limit_date DATETIME NOT NULL,
            price DECIMAL(10,2) NOT NULL,
            freight_value DECIMAL(10,2) NOT NULL,
            PRIMARY KEY (order_id, order_item_id),
            INDEX idx_oi_product (product_id),
            INDEX idx_oi_seller (seller_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "order_payments": """
        CREATE TABLE IF NOT EXISTS order_payments (
            order_id VARCHAR(32) NOT NULL,
            payment_sequential INT NOT NULL,
            payment_type VARCHAR(32) NOT NULL,
            payment_installments INT NOT NULL,
            payment_value DECIMAL(10,2) NOT NULL,
            PRIMARY KEY (order_id, payment_sequential),
            INDEX idx_op_payment_type (payment_type)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "order_reviews": """
        CREATE TABLE IF NOT EXISTS order_reviews (
            review_id VARCHAR(32) NOT NULL,
            order_id VARCHAR(32) NOT NULL,
            review_score INT NOT NULL,
            review_comment_title VARCHAR(255) NULL,
            review_comment_message TEXT NULL,
            review_creation_date DATETIME NOT NULL,
            review_answer_timestamp DATETIME NOT NULL,
            PRIMARY KEY (review_id, order_id),
            INDEX idx_or_score (review_score),
            INDEX idx_or_order (order_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "leads_qualified": """
        CREATE TABLE IF NOT EXISTS leads_qualified (
            mql_id VARCHAR(32) PRIMARY KEY,
            first_contact_date DATE NOT NULL,
            landing_page_id VARCHAR(32) NOT NULL,
            origin VARCHAR(32) NULL,
            INDEX idx_lq_origin (origin)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "leads_closed": """
        CREATE TABLE IF NOT EXISTS leads_closed (
            mql_id VARCHAR(32) PRIMARY KEY,
            seller_id VARCHAR(32) NOT NULL,
            sdr_id VARCHAR(32) NOT NULL,
            sr_id VARCHAR(32) NOT NULL,
            won_date DATETIME NOT NULL,
            business_segment VARCHAR(64) NULL,
            lead_type VARCHAR(32) NULL,
            lead_behaviour_profile VARCHAR(32) NULL,
            has_company TINYINT(1) NULL,
            has_gtin TINYINT(1) NULL,
            average_stock VARCHAR(32) NULL,
            business_type VARCHAR(32) NULL,
            declared_product_catalog_size DOUBLE NULL,
            declared_monthly_revenue DOUBLE NOT NULL,
            INDEX idx_lc_seller (seller_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "geolocation": """
        CREATE TABLE IF NOT EXISTS geolocation (
            geolocation_id BIGINT AUTO_INCREMENT PRIMARY KEY,
            geolocation_zip_code_prefix INT NOT NULL,
            geolocation_lat DOUBLE NOT NULL,
            geolocation_lng DOUBLE NOT NULL,
            geolocation_city VARCHAR(64) NOT NULL,
            geolocation_state VARCHAR(10) NOT NULL,
            INDEX idx_geo_zip (geolocation_zip_code_prefix),
            INDEX idx_geo_city (geolocation_city)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
}

# Table migration order (independent tables first)
MIGRATION_ORDER = [
    "product_category_name_translation",
    "sellers",
    "customers",
    "products",
    "orders",
    "order_items",
    "order_payments",
    "order_reviews",
    "leads_qualified",
    "leads_closed",
    "geolocation",
]


def sanitize_val(val):
    if val is None or val == "" or val == "None":
        return None
    return val


def get_tidb_connection():
    settings = get_settings()
    parsed = urlparse(settings.DB_URI)
    host = parsed.hostname
    port = parsed.port or 4000
    user = parsed.username
    password = unquote(parsed.password) if parsed.password else ""
    db_name = "ecommerce"

    ssl_ctx = ssl.create_default_context() if "tidbcloud.com" in (host or "") else None

    conn = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=db_name,
        ssl=ssl_ctx,
        charset="utf8mb4",
        autocommit=False,
    )
    return conn


def migrate_ecommerce(batch_size: int = 5000):
    start_total = time.perf_counter()
    print("=" * 65)
    print("PLAINSQL -- SPIDER E_COMMERCE -> TIDB CLOUD MIGRATION")
    print("=" * 65)
    print(f"Source: {SOURCE_SQLITE_PATH}")

    if not os.path.exists(SOURCE_SQLITE_PATH):
        raise FileNotFoundError(f"Source file not found: {SOURCE_SQLITE_PATH}")

    sqlite_conn = sqlite3.connect(SOURCE_SQLITE_PATH)
    sqlite_cursor = sqlite_conn.cursor()

    tidb_conn = get_tidb_connection()
    tidb_cursor = tidb_conn.cursor()

    print("Connected to TiDB database: ecommerce")

    # 1. Create tables
    print("\n[1] Creating Schemas in TiDB (ecommerce)...")
    for tbl in MIGRATION_ORDER:
        ddl = DDL_STATEMENTS[tbl]
        tidb_cursor.execute(ddl)
        print(f"  - Table '{tbl}' ready.")
    tidb_conn.commit()

    # 2. Migrate data table by table
    print("\n[2] Migrating Data...")
    migration_summary = {}

    for tbl in MIGRATION_ORDER:
        t0 = time.perf_counter()
        src_count = sqlite_cursor.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]

        # Check existing destination count
        tidb_cursor.execute(f"SELECT COUNT(*) FROM `{tbl}`")
        dest_count = tidb_cursor.fetchone()[0]

        if dest_count == src_count and src_count > 0:
            print(f"  - {tbl:<35}: ALREADY MIGRATED ({dest_count:,} rows). Skipping.")
            migration_summary[tbl] = {"source": src_count, "dest": dest_count, "status": "MATCH"}
            continue

        if dest_count > 0 and dest_count != src_count:
            print(f"  - {tbl:<35}: Existing {dest_count:,} rows differs from {src_count:,}. Truncating target table for clean sync...")
            tidb_cursor.execute(f"TRUNCATE TABLE `{tbl}`")
            tidb_conn.commit()

        # Get column names from SQLite
        col_info = sqlite_cursor.execute(f'PRAGMA table_info("{tbl}")').fetchall()
        col_names = [c[1] for c in col_info]
        placeholders = ", ".join(["%s"] * len(col_names))
        col_list_str = ", ".join([f"`{c}`" for c in col_names])
        insert_sql = f"INSERT INTO `{tbl}` ({col_list_str}) VALUES ({placeholders})"

        sqlite_cursor.execute(f'SELECT * FROM "{tbl}"')

        inserted = 0
        while True:
            rows = sqlite_cursor.fetchmany(batch_size)
            if not rows:
                break
            # Sanitize values
            clean_rows = [[sanitize_val(v) for v in row] for row in rows]
            tidb_cursor.executemany(insert_sql, clean_rows)
            tidb_conn.commit()
            inserted += len(rows)
            if src_count > 50000:
                print(f"    ... {tbl}: {inserted:,} / {src_count:,} rows inserted ({inserted/src_count*100:.1f}%)")

        elapsed = round(time.perf_counter() - t0, 2)
        print(f"  - {tbl:<35}: [DONE] {inserted:,} rows in {elapsed}s")
        migration_summary[tbl] = {"source": src_count, "dest": inserted, "status": "MATCH" if src_count == inserted else "MISMATCH"}

    # 3. Validation
    print("\n[3] Validating Row Counts...")
    all_matched = True
    total_src = 0
    total_dest = 0

    for tbl in MIGRATION_ORDER:
        src = migration_summary[tbl]["source"]
        tidb_cursor.execute(f"SELECT COUNT(*) FROM `{tbl}`")
        dest = tidb_cursor.fetchone()[0]
        status = "MATCH" if src == dest else "FAIL"
        if src != dest:
            all_matched = False
        total_src += src
        total_dest += dest
        print(f"  {tbl:<35}: SQLite={src:>8,} | TiDB={dest:>8,} [{status}]")

    print(f"\nTOTAL ROWS: SQLite={total_src:,} | TiDB={total_dest:,} (Match: {all_matched})")

    # 4. Business aggregates validation
    print("\n[4] Validating Business Aggregates...")
    # Orders count & earliest/latest purchase
    tidb_cursor.execute("SELECT COUNT(*), MIN(order_purchase_timestamp), MAX(order_purchase_timestamp) FROM orders")
    ord_row = tidb_cursor.fetchone()
    print(f"  - Orders: {ord_row[0]:,} rows (Range: {ord_row[1]} to {ord_row[2]})")

    # Order Items gross price & freight
    tidb_cursor.execute("SELECT SUM(price), SUM(freight_value) FROM order_items")
    items_row = tidb_cursor.fetchone()
    print(f"  - Order Items Gross Sales: ${float(items_row[0] or 0):,.2f}")
    print(f"  - Order Items Total Freight: ${float(items_row[1] or 0):,.2f}")

    # Order Payments total collected
    tidb_cursor.execute("SELECT SUM(payment_value) FROM order_payments")
    pay_row = tidb_cursor.fetchone()
    print(f"  - Order Payments Collected: ${float(pay_row[0] or 0):,.2f}")

    sqlite_conn.close()
    tidb_conn.close()

    total_time = round(time.perf_counter() - start_total, 2)
    print("\n" + "=" * 65)
    print(f"MIGRATION COMPLETE in {total_time}s - Status: {'SUCCESS' if all_matched else 'FAILED'}")
    print("=" * 65)
    return all_matched


if __name__ == "__main__":
    success = migrate_ecommerce(batch_size=5000)
    sys.exit(0 if success else 1)

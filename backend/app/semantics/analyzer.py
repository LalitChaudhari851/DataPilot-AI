"""
Semantic Schema Analyzer for DataPilot.
Infers semantic roles, extracts safe distinct values, and derives business meanings.
"""

import re
import sqlite3
import logging
from typing import List, Dict, Any, Optional

try:
    from app.semantics.models import SemanticRole, ColumnSemanticMetadata
except ImportError:
    from backend.app.semantics.models import SemanticRole, ColumnSemanticMetadata

logger = logging.getLogger(__name__)

# Common synonyms mapping for canonical business concepts
CONCEPT_SYNONYMS: Dict[str, List[str]] = {
    "status": ["state", "stage", "lifecycle", "condition", "phase"],
    "price": ["cost", "charge", "rate", "amount", "unit price"],
    "freight": ["freight value", "shipping fee", "shipping cost", "delivery fee"],
    "revenue": ["sales", "turnover", "total sales", "gross sales"],
    "quantity": ["units", "items", "volume", "units sold", "item count"],
    "customer": ["client", "buyer", "account", "user"],
    "order": ["purchase", "transaction"],
    "delivery": ["delivered", "fulfillment", "shipment"],
    "timestamp": ["date time", "time", "event time", "recorded at"],
    "date": ["day", "calendar date"],
}


class SemanticSchemaAnalyzer:
    """Analyzes schema structure and bounded samples to infer column semantics."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path

    @staticmethod
    def infer_role(table_name: str, column_name: str, data_type: str = "") -> SemanticRole:
        """Heuristically infer semantic role from column name, table name, and data type."""
        c = column_name.lower().strip()
        t = (data_type or "").upper()

        # 1. Primary & Foreign Keys / Identifiers
        if c in ("id", "uuid", "guid", "pk"):
            return SemanticRole.IDENTIFIER
        if c.endswith(("_id", "id")) and not c.endswith(("valid", "paid", "mid")):
            tbl = table_name.lower()
            tbl_stem = tbl[:-1] if tbl.endswith("s") else tbl
            if c.startswith((f"{tbl}_", f"{tbl_stem}_")) or c in (f"{tbl}id", f"{tbl_stem}id"):
                return SemanticRole.IDENTIFIER
            return SemanticRole.FOREIGN_KEY
        if c.endswith(("_code", "code", "key", "number", "num")) and not c.endswith(("phone_number", "track_number")):
            if "zip" in c or "postal" in c or "area" in c:
                return SemanticRole.DIMENSION
            return SemanticRole.IDENTIFIER

        # 2. Boolean flags
        if c.startswith(("is_", "has_", "can_", "should_")) or c.endswith(("_flag", "flag")):
            return SemanticRole.BOOLEAN
        if t in ("BOOLEAN", "BOOL", "TINYINT(1)"):
            return SemanticRole.BOOLEAN

        # 3. Timestamps & Dates
        if c.endswith(("_at", "_time", "timestamp", "_datetime")) or "timestamp" in c or "datetime" in c:
            return SemanticRole.TIMESTAMP
        if c.endswith(("_date", "date", "_dt")) or t in ("DATE", "DATETIME", "TIMESTAMP"):
            if "time" in c or c.endswith("_at") or t in ("DATETIME", "TIMESTAMP"):
                return SemanticRole.TIMESTAMP
            return SemanticRole.DATE

        # 4. Status
        if c.endswith(("status", "state", "stage", "lifecycle", "phase")) or "status" in c:
            return SemanticRole.STATUS

        # 5. Percentage / Rate
        if c.endswith(("_pct", "pct", "_percent", "percentage", "_rate", "rate", "_ratio", "ratio", "margin")):
            return SemanticRole.PERCENTAGE

        # 6. Monetary Amounts
        amount_keywords = ("price", "amount", "freight", "cost", "total", "tax", "fee", "revenue", 
                           "salary", "bonus", "payment", "sales", "due", "balance", "charge")
        if any(kw in c for kw in amount_keywords) and not c.endswith(("_id", "_count", "_qty")):
            return SemanticRole.AMOUNT

        # 7. Quantity / Counts / Sports Metrics
        qty_keywords = ("quantity", "qty", "count", "units", "items", "inventory", "volume")
        if any(kw in c for kw in qty_keywords):
            return SemanticRole.QUANTITY
        # Common sports / baseball metrics: w (wins), l (losses), g (games), r (runs), h (hits), ab (at bats)
        if table_name.lower() in ("team", "player", "pitching", "batting") and c in ("w", "l", "g", "r", "h", "ab", "hr", "so", "bb", "era"):
            return SemanticRole.METRIC

        # 8. Names
        if c in ("name", "first_name", "last_name", "full_name", "company_name", "title", "nickname"):
            return SemanticRole.NAME

        # 9. Categories / Types
        if c.endswith(("_type", "type", "_category", "category", "_genre", "genre", "_segment", "segment", "_tier", "tier", "_class", "class")):
            return SemanticRole.CATEGORY

        # 10. Dimensions (Geography, temporal intervals, attributes)
        if c in ("city", "country", "state", "region", "zip_code", "postal_code", "year", "month", "day", "quarter"):
            return SemanticRole.DIMENSION

        # 11. Descriptive text
        if c in ("description", "notes", "comment", "summary", "text", "body", "details"):
            return SemanticRole.DESCRIPTIVE_TEXT

        # 12. Fallbacks based on data type
        if any(num_t in t for num_t in ("INT", "FLOAT", "DECIMAL", "NUMERIC", "DOUBLE", "REAL")):
            return SemanticRole.METRIC

        return SemanticRole.DIMENSION

    @staticmethod
    def infer_business_meaning(table_name: str, column_name: str, role: SemanticRole) -> str:
        """Derive a natural business meaning for a column."""
        clean_col = column_name.replace("_", " ").title()
        clean_tbl = table_name.replace("_", " ").title()

        if role == SemanticRole.STATUS:
            return f"Current lifecycle state or condition of {clean_tbl} ({clean_col})"
        elif role == SemanticRole.TIMESTAMP:
            if "delivered" in column_name.lower():
                return f"Date and time when the {clean_tbl} was physically delivered to customer"
            if "purchase" in column_name.lower() or "created" in column_name.lower() or "order" in column_name.lower():
                return f"Timestamp when {clean_tbl} was created or placed"
            return f"Timestamp recording when the {clean_col} event occurred in {clean_tbl}"
        elif role == SemanticRole.DATE:
            return f"Calendar date of {clean_col} for {clean_tbl}"
        elif role == SemanticRole.AMOUNT:
            if "freight" in column_name.lower():
                return f"Shipping or freight charge value for {clean_tbl}"
            if "price" in column_name.lower():
                return f"Monetary item price or unit transaction amount in {clean_tbl}"
            return f"Monetary amount representing {clean_col} in {clean_tbl}"
        elif role == SemanticRole.QUANTITY:
            return f"Count or unit volume of {clean_col} in {clean_tbl}"
        elif role == SemanticRole.METRIC:
            return f"Numeric metric {clean_col} for {clean_tbl}"
        elif role == SemanticRole.CATEGORY:
            return f"Classification or category of {clean_col} for {clean_tbl}"
        elif role == SemanticRole.IDENTIFIER:
            return f"Unique primary identifier for {clean_tbl}"
        elif role == SemanticRole.FOREIGN_KEY:
            return f"Reference key linking {clean_tbl} to associated entity"
        elif role == SemanticRole.BOOLEAN:
            return f"Boolean flag indicating whether {clean_col} applies to {clean_tbl}"
        elif role == SemanticRole.NAME:
            return f"Display name or title of {clean_tbl}"
        return f"{clean_col} attribute of {clean_tbl}"

    @staticmethod
    def derive_synonyms(column_name: str, role: SemanticRole) -> List[str]:
        """Extract lightweight contextual synonyms for column."""
        c = column_name.lower()
        synonyms = set()
        words = c.split("_")
        
        # Add basic space-separated name
        synonyms.add(" ".join(words))

        for word in words:
            if word in CONCEPT_SYNONYMS:
                synonyms.update(CONCEPT_SYNONYMS[word])

        if role == SemanticRole.STATUS:
            synonyms.add("state")
            synonyms.add("status")
        elif role == SemanticRole.TIMESTAMP:
            synonyms.add("time")
            synonyms.add("timestamp")
            if "delivered" in c:
                synonyms.add("delivered date")
                synonyms.add("delivery timestamp")
                synonyms.add("delivery time")
        elif role == SemanticRole.AMOUNT:
            synonyms.add("value")
            synonyms.add("cost")
            synonyms.add("money")

        return sorted(list(synonyms))

    def sample_distinct_values(self, table_name: str, column_name: str, role: SemanticRole, max_values: int = 10) -> List[str]:
        """Safely fetch distinct sample values for status, category, or boolean columns.
        Bounded by LIMIT and read-only query.
        """
        # Only inspect values for categorical/status/boolean columns to avoid performance and privacy issues
        if role not in (SemanticRole.STATUS, SemanticRole.CATEGORY, SemanticRole.BOOLEAN):
            return []

        if not self.db_path:
            return []

        # Sanitize identifiers
        if not re.match(r"^[a-zA-Z0-9_]+$", table_name) or not re.match(r"^[a-zA-Z0-9_]+$", column_name):
            return []

        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=3.0)
            cursor = conn.cursor()
            query = f'SELECT DISTINCT "{column_name}" FROM "{table_name}" WHERE "{column_name}" IS NOT NULL LIMIT {max_values}'
            cursor.execute(query)
            rows = cursor.fetchall()
            conn.close()

            values = [str(r[0]).strip() for r in rows if r[0] is not None and str(r[0]).strip() != ""]
            return values[:max_values]
        except Exception as e:
            logger.debug(f"Could not sample values for {table_name}.{column_name}: {e}")
            return []

    def analyze_column(
        self,
        database_id: str,
        table_name: str,
        column_name: str,
        data_type: str = "",
        sample_values: Optional[List[str]] = None,
    ) -> ColumnSemanticMetadata:
        """Build full ColumnSemanticMetadata for a column."""
        role = self.infer_role(table_name, column_name, data_type)
        meaning = self.infer_business_meaning(table_name, column_name, role)
        synonyms = self.derive_synonyms(column_name, role)

        # Inspect values if not provided and role is eligible
        if sample_values is None:
            sample_values = self.sample_distinct_values(table_name, column_name, role)

        return ColumnSemanticMetadata(
            database_id=database_id,
            table_name=table_name,
            column_name=column_name,
            data_type=data_type,
            semantic_role=role,
            description=f"{data_type} column in {table_name}",
            business_meaning=meaning,
            synonyms=synonyms,
            sample_values=sample_values or [],
            confidence=0.95,
        )

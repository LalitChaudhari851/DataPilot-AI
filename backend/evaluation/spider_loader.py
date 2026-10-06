"""
Spider 2.0-Lite Dataset Adapter.

Loads task/question metadata from local Spider 2.0-Lite evaluation files.
Normalizes examples into a standard format and maps db_id to SQLitePool
via DatabaseRegistry.

Does not hardcode absolute Windows paths. Configured via SPIDER_EVAL_DIR.
"""

import json
import os
from typing import Optional, Any
import structlog

from app.config import get_settings
from app.db.registry import get_database_registry, DatabaseRegistry

logger = structlog.get_logger()


class SpiderDatasetLoader:
    """
    Adapter for loading and normalizing Spider 2.0-Lite evaluation examples.

    Handles:
    - JSONL files with tasks (e.g. spider2-lite.jsonl, spider_sqlite_benchmarks.jsonl)
    - local-map.jsonl mapping instance_id -> db_id
    - Missing gold SQL handling (explicitly flags has_gold_sql=False)
    - db_id mapping to local SQLite databases via DatabaseRegistry
    """

    def __init__(self, eval_dir: Optional[str] = None, registry: Optional[DatabaseRegistry] = None):
        settings = get_settings()
        self.eval_dir = eval_dir or settings.SPIDER_EVAL_DIR or settings.SPIDER_DB_DIR
        self.registry = registry or get_database_registry()

    def load_dataset(
        self,
        db_id: Optional[str] = None,
        limit: Optional[int] = None,
        require_gold_sql: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Load Spider 2.0-Lite benchmark examples.

        Args:
            db_id: Optional filter for a specific database (e.g. 'E_commerce')
            limit: Maximum number of examples to return
            require_gold_sql: If True, only return examples with non-null gold_sql

        Returns:
            List of normalized example dicts:
            [
                {
                    "instance_id": "local002",
                    "db_id": "E_commerce",
                    "question": "...",
                    "gold_sql": "...",
                    "external_knowledge": ...,
                    "has_gold_sql": bool,
                },
                ...
            ]
        """
        raw_examples = self._discover_and_read_examples()

        normalized = []
        for raw in raw_examples:
            item = self.normalize_example(raw)

            # Filter by db_id
            if db_id and item["db_id"].lower() != db_id.lower():
                continue

            # Filter by gold SQL availability
            if require_gold_sql and not item.get("has_gold_sql"):
                continue

            normalized.append(item)

            if limit is not None and len(normalized) >= limit:
                break

        logger.info(
            "spider_dataset_loaded",
            total_loaded=len(normalized),
            db_id_filter=db_id,
            require_gold_sql=require_gold_sql,
            limit=limit,
        )

        return normalized

    def normalize_example(self, raw: dict[str, Any]) -> dict[str, Any]:
        """
        Normalize a raw Spider example into a clean standard structure.
        Only includes fields that actually exist in the local dataset.
        Does not invent missing gold SQL.
        """
        instance_id = str(raw.get("instance_id") or raw.get("id") or "unknown")
        database_id = str(raw.get("db_id") or raw.get("db") or "default")
        question = raw.get("question") or raw.get("prompt") or ""
        gold_sql = raw.get("gold_sql") or raw.get("query") or raw.get("expected_sql")
        external_knowledge = raw.get("external_knowledge")

        # Clean string if empty
        if isinstance(gold_sql, str) and not gold_sql.strip():
            gold_sql = None

        has_gold_sql = gold_sql is not None and len(str(gold_sql).strip()) > 0

        normalized: dict[str, Any] = {
            "instance_id": instance_id,
            "db_id": database_id,
            "question": str(question).strip() if question else "",
            "gold_sql": gold_sql.strip() if isinstance(gold_sql, str) else gold_sql,
            "has_gold_sql": has_gold_sql,
        }

        if external_knowledge is not None:
            normalized["external_knowledge"] = external_knowledge

        if "difficulty" in raw:
            normalized["difficulty"] = raw["difficulty"]

        return normalized

    def _discover_and_read_examples(self) -> list[dict[str, Any]]:
        """
        Discover and read examples from configured directories.
        Checks:
        1. Bundled benchmarks (backend/evaluation/datasets/spider_sqlite_benchmarks.jsonl)
        2. SPIDER_EVAL_DIR for jsonl / json files
        3. local-map.jsonl for mapping definitions
        """
        examples: list[dict[str, Any]] = []
        seen_instances: set[str] = set()

        # 1. Check bundled benchmark dataset
        bundled_path = os.path.join(
            os.path.dirname(__file__), "datasets", "spider_sqlite_benchmarks.jsonl"
        )
        if os.path.isfile(bundled_path):
            bundled_items = self._read_jsonl_or_json(bundled_path)
            for item in bundled_items:
                iid = item.get("instance_id") or item.get("id")
                if iid and iid not in seen_instances:
                    seen_instances.add(iid)
                    examples.append(item)

        # 2. Check SPIDER_EVAL_DIR if configured and different from bundled
        if self.eval_dir and os.path.exists(self.eval_dir):
            if os.path.isdir(self.eval_dir):
                # Search for jsonl / json files
                for fname in sorted(os.listdir(self.eval_dir)):
                    fpath = os.path.join(self.eval_dir, fname)
                    if fname.endswith((".jsonl", ".json")) and not fname.startswith("."):
                        if fname == "local-map.jsonl":
                            continue  # Handled separately
                        try:
                            items = self._read_jsonl_or_json(fpath)
                            for item in items:
                                iid = item.get("instance_id") or item.get("id")
                                if iid and iid not in seen_instances:
                                    seen_instances.add(iid)
                                    examples.append(item)
                        except Exception as e:
                            logger.warning("eval_file_read_error", file=fname, error=str(e))

                # Handle local-map.jsonl if present
                local_map_path = os.path.join(self.eval_dir, "local-map.jsonl")
                if os.path.isfile(local_map_path):
                    self._merge_local_map(local_map_path, examples, seen_instances)

            elif os.path.isfile(self.eval_dir):
                items = self._read_jsonl_or_json(self.eval_dir)
                for item in items:
                    iid = item.get("instance_id") or item.get("id")
                    if iid and iid not in seen_instances:
                        seen_instances.add(iid)
                        examples.append(item)

        return examples

    def _merge_local_map(
        self,
        local_map_path: str,
        examples: list[dict[str, Any]],
        seen_instances: set[str],
    ):
        """
        Read local-map.jsonl which maps instance_id -> db_id.
        Adds instances that are in local-map.jsonl but have no questions yet (with has_gold_sql=False).
        """
        try:
            with open(local_map_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return

                # local-map.jsonl can be a single JSON dict or JSON lines
                if content.startswith("{") and not "\n{" in content:
                    try:
                        map_dict = json.loads(content)
                        for inst_id, db_name in map_dict.items():
                            if inst_id not in seen_instances:
                                seen_instances.add(inst_id)
                                examples.append({
                                    "instance_id": inst_id,
                                    "db_id": db_name,
                                    "question": "",
                                    "gold_sql": None,
                                    "has_gold_sql": False,
                                })
                        return
                    except Exception:
                        pass

                for line in content.splitlines():
                    line = line.strip()
                    if line:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            for inst_id, db_name in obj.items():
                                if inst_id not in seen_instances:
                                    seen_instances.add(inst_id)
                                    examples.append({
                                        "instance_id": inst_id,
                                        "db_id": db_name,
                                        "question": "",
                                        "gold_sql": None,
                                        "has_gold_sql": False,
                                    })
        except Exception as e:
            logger.warning("local_map_read_error", error=str(e))

    @staticmethod
    def _read_jsonl_or_json(file_path: str) -> list[dict[str, Any]]:
        """Read a file that may be JSONL or a JSON array/object."""
        items = []
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return items

            # Try parsing as standard JSON array or object
            if content.startswith("["):
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, list):
                        return parsed
                except Exception:
                    pass
            elif content.startswith("{"):
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict) and "examples" in parsed:
                        return parsed["examples"]
                    elif isinstance(parsed, dict) and "data" in parsed:
                        return parsed["data"]
                except Exception:
                    pass

            # Parse as JSON Lines
            for line in content.splitlines():
                line = line.strip()
                if line and line.startswith("{"):
                    try:
                        items.append(json.loads(line))
                    except Exception:
                        pass
        return items

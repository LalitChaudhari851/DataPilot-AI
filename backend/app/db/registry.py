"""
Database Registry — Maps db_id → database pool for multi-database support.

Supports both MySQL (existing DatabasePool) and SQLite (new SQLitePool).
Auto-discovers Spider 2.0-Lite SQLite files from a configured directory.
"""

import os
import structlog

logger = structlog.get_logger()


class DatabaseRegistry:
    """
    Multi-database registry that maps db_id → database pool instance.

    Usage:
        registry = DatabaseRegistry()

        # Register the existing MySQL database
        registry.register("default", mysql_pool, dialect="mysql")

        # Auto-discover Spider SQLite databases from a directory
        registry.discover_sqlite_databases("/path/to/spider_databases")

        # Get a pool by db_id
        pool = registry.get_pool("E_commerce")
        pool.get_tables()
    """

    def __init__(self):
        self._pools: dict[str, dict] = {}
        # {db_id: {"pool": pool_instance, "dialect": "mysql"|"sqlite", "path": "..."}}

    def register(self, db_id: str, pool, dialect: str = "mysql", path: str = ""):
        """
        Register a database pool with the given ID.

        Args:
            db_id: Unique identifier for this database
            pool: DatabasePool or SQLitePool instance
            dialect: "mysql" or "sqlite"
            path: File path (for SQLite) or connection URI info
        """
        self._pools[db_id] = {
            "pool": pool,
            "dialect": dialect,
            "path": path,
        }
        logger.info(
            "database_registered",
            db_id=db_id,
            dialect=dialect,
            tables=len(pool.get_tables()) if hasattr(pool, "get_tables") else 0,
        )

    def discover_sqlite_databases(self, directory: str, lazy: bool = True):
        """
        Auto-discover and register all .sqlite files in a directory.

        Args:
            directory: Path to directory containing .sqlite files
            lazy: If True, create pool instances lazily on first access.
                  If False, create all pools immediately on discovery.
        """
        if not directory or not os.path.isdir(directory):
            logger.warning(
                "sqlite_discovery_skipped",
                directory=directory,
                reason="directory not found or not configured",
            )
            return

        discovered = 0
        for filename in sorted(os.listdir(directory)):
            if not filename.endswith(".sqlite"):
                continue

            db_path = os.path.join(directory, filename)
            db_id = os.path.splitext(filename)[0]

            # Skip if already registered (don't overwrite manual registrations)
            if db_id in self._pools:
                continue

            if lazy:
                # Store path only — pool created on first get_pool() call
                self._pools[db_id] = {
                    "pool": None,  # Lazy — created on first access
                    "dialect": "sqlite",
                    "path": db_path,
                }
                discovered += 1
            else:
                try:
                    from app.db.sqlite_pool import SQLitePool
                    pool = SQLitePool(db_path)
                    self.register(db_id, pool, dialect="sqlite", path=db_path)
                    discovered += 1
                except Exception as e:
                    logger.warning(
                        "sqlite_discovery_failed",
                        db_id=db_id,
                        path=db_path,
                        error=str(e),
                    )

        logger.info(
            "sqlite_discovery_complete",
            directory=directory,
            discovered=discovered,
            total_registered=len(self._pools),
            lazy=lazy,
        )

    def get_pool(self, db_id: str):
        """
        Get the database pool for a given db_id.

        Handles lazy initialization for SQLite databases discovered
        with lazy=True.

        Raises:
            KeyError: If db_id is not registered
        """
        entry = self._pools.get(db_id)
        if entry is None:
            available = self.list_databases()
            raise KeyError(
                f"Database '{db_id}' not found. Available: {[d['db_id'] for d in available]}"
            )

        # Lazy initialization for SQLite pools
        if entry["pool"] is None and entry["dialect"] == "sqlite":
            from app.db.sqlite_pool import SQLitePool
            entry["pool"] = SQLitePool(entry["path"])
            logger.info("sqlite_pool_lazy_initialized", db_id=db_id)

        # Lazy initialization for MySQL pools
        if entry["pool"] is None and entry["dialect"] == "mysql":
            from app.db.connection import DatabasePool
            entry["pool"] = DatabasePool(entry["path"])
            logger.info("mysql_pool_lazy_initialized", db_id=db_id)

        return entry["pool"]

    def get_dialect(self, db_id: str) -> str:
        """Get the SQL dialect for a given db_id."""
        entry = self._pools.get(db_id)
        if entry is None:
            raise KeyError(f"Database '{db_id}' not found")
        return entry["dialect"]

    def list_databases(self) -> list[dict]:
        """
        List all registered databases with metadata.

        Returns:
            [{"db_id": ..., "dialect": ..., "path": ..., "initialized": bool}, ...]
        """
        result = []
        for db_id, entry in sorted(self._pools.items()):
            result.append({
                "db_id": db_id,
                "dialect": entry["dialect"],
                "path": entry.get("path", ""),
                "initialized": entry["pool"] is not None,
            })
        return result

    def has_database(self, db_id: str) -> bool:
        """Check if a database is registered."""
        return db_id in self._pools

    @property
    def default_db_id(self) -> str:
        """Return the default database ID (first registered, typically MySQL)."""
        if "default" in self._pools:
            return "default"
        if self._pools:
            return next(iter(self._pools))
        raise RuntimeError("No databases registered")


# ── Module-level Singleton ───────────────────────────────────────

_registry_instance: DatabaseRegistry | None = None


def get_database_registry() -> DatabaseRegistry:
    """Get or create the global DatabaseRegistry singleton."""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = DatabaseRegistry()
        try:
            from app.config import get_settings
            settings = get_settings()

            use_sqlite_ecom = os.getenv("PLAINSQL_USE_SQLITE_ECOMMERCE", "").lower() in {"1", "true", "yes"}
            if settings.resolved_ecommerce_db_uri and not use_sqlite_ecom:
                _registry_instance.register(
                    "E_commerce",
                    pool=None,
                    dialect="mysql",
                    path=settings.resolved_ecommerce_db_uri,
                )

            if getattr(settings, "SPIDER_DB_DIR", None):
                _registry_instance.discover_sqlite_databases(settings.SPIDER_DB_DIR, lazy=True)
        except Exception as e:
            logger.warning("database_registry_auto_discovery_failed", error=str(e))
    return _registry_instance


def resolve_database(
    db_id: str | None = None,
    default_pool=None,
    registry: DatabaseRegistry | None = None,
) -> tuple[str, str, object]:
    """
    Resolve (db_id, sql_dialect, pool) given an optional db_id.
    
    DatabaseRegistry is the source of truth for the database dialect.
    Returns:
        (resolved_db_id, resolved_dialect, pool_instance)
    """
    reg = registry or get_database_registry()
    target_id = db_id or "default"

    if reg.has_database(target_id):
        dialect = reg.get_dialect(target_id)
        pool = reg.get_pool(target_id)
        return target_id, dialect, pool

    # If target_id was 'default' or not found, fall back to default_pool
    if default_pool is not None:
        return "default", "mysql", default_pool

    # If registry has a default db registered
    try:
        def_id = reg.default_db_id
        return def_id, reg.get_dialect(def_id), reg.get_pool(def_id)
    except Exception:
        raise KeyError(f"Database '{target_id}' could not be resolved and no default pool provided.")


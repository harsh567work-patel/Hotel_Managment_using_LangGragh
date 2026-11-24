"""
table_split.db.migrations.runner — Migration runner for Table Split Mode.
Provides safe, reversible migration execution and rollback with schema_migrations tracking.
"""

from __future__ import annotations

import importlib
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from config import TABLE_SPLIT_DB_PATH

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "versions"


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Creates a SQLite connection with foreign keys and WAL enabled."""
    target_path = Path(db_path or TABLE_SPLIT_DB_PATH)
    if str(target_path) != ":memory:":
        target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    if str(target_path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def init_migration_table(conn: sqlite3.Connection) -> None:
    """Ensures schema_migrations table exists."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at INTEGER NOT NULL
        );
    """)
    conn.commit()


def get_applied_versions(conn: sqlite3.Connection) -> list[str]:
    """Returns list of migration version strings already applied, ordered ascending."""
    init_migration_table(conn)
    cursor = conn.execute("SELECT version FROM schema_migrations ORDER BY version ASC;")
    return [row["version"] for row in cursor.fetchall()]


def discover_migrations() -> list[tuple[str, str, Any]]:
    """
    Discovers available migration modules in versions/ directory.
    Returns sorted list of (version, name, module).
    """
    migrations: list[tuple[str, str, Any]] = []
    if not MIGRATIONS_DIR.exists():
        return migrations

    for file in sorted(MIGRATIONS_DIR.glob("*.py")):
        if file.name.startswith("__"):
            continue
        module_name = f"table_split.db.migrations.versions.{file.stem}"
        try:
            module = importlib.import_module(module_name)
            version = getattr(module, "VERSION", file.stem.split("_")[0])
            name = getattr(module, "NAME", file.stem)
            migrations.append((version, name, module))
        except Exception as e:
            logger.error("Failed importing migration %s: %s", file.name, e)
            raise

    return sorted(migrations, key=lambda m: m[0])


def migrate_up(db_path: str | Path | None = None) -> list[str]:
    """Applies all pending migrations in ascending order."""
    conn = get_connection(db_path)
    applied_versions = get_applied_versions(conn)
    all_migrations = discover_migrations()
    newly_applied: list[str] = []

    try:
        for version, name, module in all_migrations:
            if version not in applied_versions:
                logger.info("Applying migration %s (%s)...", version, name)
                # Run upgrade in transaction
                with conn:
                    module.upgrade(conn)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?);",
                        (version, name, int(time.time())),
                    )
                newly_applied.append(version)
        return newly_applied
    finally:
        conn.close()


def migrate_down(db_path: str | Path | None = None, steps: int = 1) -> list[str]:
    """Rolls back the latest `steps` applied migrations in descending order."""
    conn = get_connection(db_path)
    applied_versions = get_applied_versions(conn)
    all_migrations = {v: (n, m) for v, n, m in discover_migrations()}
    rolled_back: list[str] = []

    try:
        targets = list(reversed(applied_versions))[:steps]
        for version in targets:
            if version in all_migrations:
                name, module = all_migrations[version]
                logger.info("Rolling back migration %s (%s)...", version, name)
                with conn:
                    module.downgrade(conn)
                    conn.execute("DELETE FROM schema_migrations WHERE version = ?;", (version,))
                rolled_back.append(version)
            else:
                raise RuntimeError(f"Cannot rollback migration {version}: module not found")
        return rolled_back
    finally:
        conn.close()

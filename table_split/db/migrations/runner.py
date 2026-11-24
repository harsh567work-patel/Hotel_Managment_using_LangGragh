"""
table_split.db.migrations.runner — Reversible migration runner with SHA-256 checksum drift detection.

Features:
- Enforces SQLite foreign_keys, WAL mode, and busy timeout via centralized engine.
- Tracks schema version, name, SHA-256 checksum, and applied timestamp.
- Detects migration drift: fails immediately with MigrationDriftError if any applied
  migration file has been modified after execution.
- Supports clean reversible upgrade and downgrade.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from table_split.db.engine import get_sqlite_connection

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "versions"


class MigrationDriftError(RuntimeError):
    """Raised when an applied migration's stored checksum differs from source code on disk."""


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Delegates connection creation to centralized engine."""
    return get_sqlite_connection(db_path)


def compute_module_checksum(module: Any, file_path: Path | None = None) -> str:
    """Computes a SHA-256 checksum of the migration module source code."""
    hasher = hashlib.sha256()
    if file_path and file_path.exists():
        hasher.update(file_path.read_bytes())
    else:
        src = inspect.getsource(module)
        hasher.update(src.encode("utf-8"))
    return hasher.hexdigest()


def init_migration_table(conn: sqlite3.Connection) -> None:
    """Ensures schema_migrations table exists with version, name, checksum, and applied_at."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at INTEGER NOT NULL
        );
    """)
    conn.commit()


def get_applied_migrations_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Returns mapping of version -> checksum for all applied migrations."""
    init_migration_table(conn)
    cursor = conn.execute("SELECT version, checksum FROM schema_migrations ORDER BY version ASC;")
    return {row["version"]: row["checksum"] for row in cursor.fetchall()}


def get_applied_versions(conn: sqlite3.Connection) -> list[str]:
    """Returns list of migration version strings already applied, ordered ascending."""
    return list(get_applied_migrations_map(conn).keys())


def discover_migrations() -> list[tuple[str, str, Any, str]]:
    """
    Discovers available migration modules in versions/ directory.
    Returns sorted list of (version, name, module, checksum).
    """
    migrations: list[tuple[str, str, Any, str]] = []
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
            checksum = compute_module_checksum(module, file)
            migrations.append((version, name, module, checksum))
        except Exception as e:
            logger.error("Failed importing migration %s: %s", file.name, e)
            raise

    return sorted(migrations, key=lambda m: m[0])


def verify_schema_integrity(conn_or_path: sqlite3.Connection | str | Path | None = None) -> None:
    """
    Validates that all applied migrations match current on-disk checksums.
    Raises MigrationDriftError on any mismatch.
    """
    should_close = False
    if isinstance(conn_or_path, sqlite3.Connection):
        conn = conn_or_path
    else:
        conn = get_connection(conn_or_path)
        should_close = True

    try:
        applied_map = get_applied_migrations_map(conn)
        available_migrations = {v: (n, m, c) for v, n, m, c in discover_migrations()}

        for version, stored_checksum in applied_map.items():
            if version not in available_migrations:
                raise MigrationDriftError(
                    f"Applied migration '{version}' is missing from versions/ migration directory!"
                )
            name, _, current_checksum = available_migrations[version]
            if stored_checksum != current_checksum:
                raise MigrationDriftError(
                    f"Migration drift detected for {version} ({name})! "
                    f"Database stored checksum {stored_checksum[:12]}... "
                    f"does not match current source checksum {current_checksum[:12]}..."
                )
    finally:
        if should_close:
            conn.close()


def migrate_up(db_path: str | Path | None = None) -> list[str]:
    """
    Verifies existing migrations against drift, then applies all pending migrations in ascending order.
    """
    conn = get_connection(db_path)
    try:
        # 1. Verify integrity of existing applied migrations
        verify_schema_integrity(conn)

        applied_map = get_applied_migrations_map(conn)
        all_migrations = discover_migrations()
        newly_applied: list[str] = []

        # 2. Apply unapplied migrations
        for version, name, module, checksum in all_migrations:
            if version not in applied_map:
                logger.info("Applying migration %s (%s) [checksum: %s]...", version, name, checksum[:8])
                with conn:
                    module.upgrade(conn)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (?, ?, ?, ?);",
                        (version, name, checksum, int(time.time())),
                    )
                newly_applied.append(version)
        return newly_applied
    finally:
        conn.close()


def migrate_down(db_path: str | Path | None = None, steps: int = 1) -> list[str]:
    """Rolls back the latest `steps` applied migrations in descending order."""
    conn = get_connection(db_path)
    try:
        applied_versions = get_applied_versions(conn)
        all_migrations = {v: (n, m, c) for v, n, m, c in discover_migrations()}
        rolled_back: list[str] = []

        targets = list(reversed(applied_versions))[:steps]
        for version in targets:
            if version in all_migrations:
                name, module, _ = all_migrations[version]
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

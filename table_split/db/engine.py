"""
table_split.db.engine — Centralized SQLite connection engine with strict PRAGMAs.

Enforces on EVERY connection (application & testing):
- PRAGMA foreign_keys = ON (referential integrity)
- PRAGMA journal_mode = WAL (write-ahead logging for high concurrency)
- PRAGMA busy_timeout = 15000 (15 second timeout to prevent SQLite busy locks)
- PRAGMA synchronous = NORMAL (safe performance on WAL)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from config import TABLE_SPLIT_DB_PATH

DEFAULT_BUSY_TIMEOUT_MS = 15000


def get_sqlite_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """
    Creates an optimized SQLite connection with foreign keys, WAL mode,
    and a 15-second busy timeout enabled immediately.
    """
    target = Path(db_path or TABLE_SPLIT_DB_PATH)
    is_memory = str(target) == ":memory:"

    if not is_memory:
        target.parent.mkdir(parents=True, exist_ok=True)

    # timeout parameter in sqlite3.connect sets internal lock retry, busy_timeout pragma reinforces at SQLite layer
    conn = sqlite3.connect(str(target), timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row

    # Strict pragma configuration
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS};")

    if not is_memory:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")

    return conn


def create_sqlite_engine(db_path: str | Path | None = None) -> Engine:
    """
    Creates a SQLAlchemy 2.0 Engine wired with a connection hook ensuring
    foreign keys, WAL mode, and busy timeout are applied on every checkout.
    """
    target = Path(db_path or TABLE_SPLIT_DB_PATH)
    is_memory = str(target) == ":memory:"

    if not is_memory:
        target.parent.mkdir(parents=True, exist_ok=True)
        db_url = f"sqlite:///{target.as_posix()}"
    else:
        db_url = "sqlite:///:memory:"

    engine = create_engine(
        db_url,
        connect_args={"timeout": DEFAULT_BUSY_TIMEOUT_MS / 1000.0},
        echo=False,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON;")
        cursor.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS};")
        if not is_memory:
            cursor.execute("PRAGMA journal_mode = WAL;")
            cursor.execute("PRAGMA synchronous = NORMAL;")
        cursor.close()

    return engine


def get_session_factory(db_path: str | Path | None = None) -> sessionmaker[Session]:
    """Provides a thread-safe SQLAlchemy sessionmaker."""
    engine = create_sqlite_engine(db_path)
    return sessionmaker(bind=engine, expire_on_commit=False)

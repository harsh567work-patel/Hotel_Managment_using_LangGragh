"""
Migration 003_stage4_session_events_seq: Adds ordered event sequence & version columns
to session_events and retention purge tracking to table_sessions.
"""

from __future__ import annotations

import sqlite3

VERSION = "003"
NAME = "stage4_session_events_seq"


def upgrade(conn: sqlite3.Connection) -> None:
    """Adds seq/session_version on session_events and sensitive_data_purged_at on table_sessions."""
    conn.executescript("""
        ALTER TABLE session_events
            ADD COLUMN seq INTEGER NOT NULL DEFAULT 0;

        ALTER TABLE session_events
            ADD COLUMN session_version INTEGER NOT NULL DEFAULT 1;

        CREATE INDEX IF NOT EXISTS idx_session_events_session_seq
            ON session_events(session_id, seq);

        ALTER TABLE table_sessions
            ADD COLUMN sensitive_data_purged_at INTEGER;
    """)


def downgrade(conn: sqlite3.Connection) -> None:
    """Reverts Stage 4 columns and sequence index cleanly."""
    conn.executescript("""
        DROP INDEX IF EXISTS idx_session_events_session_seq;
        ALTER TABLE session_events DROP COLUMN session_version;
        ALTER TABLE session_events DROP COLUMN seq;
        ALTER TABLE table_sessions DROP COLUMN sensitive_data_purged_at;
    """)

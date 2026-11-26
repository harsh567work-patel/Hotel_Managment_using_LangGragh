"""
Migration 004_stage5_parser_logs: Adds PII-redacted parser audit log table
with 7-day retention expiry index.
"""

from __future__ import annotations

import sqlite3

VERSION = "004"
NAME = "stage5_parser_logs"


def upgrade(conn: sqlite3.Connection) -> None:
    """Creates parser_logs table for PII-redacted preference parser audit logging."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS parser_logs (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            participant_id TEXT,
            locale TEXT NOT NULL DEFAULT 'en-IN',
            redacted_input TEXT NOT NULL,
            redacted_output_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,
            error_code TEXT,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            CONSTRAINT ck_parser_log_status_valid CHECK (
                status IN ('success', 'timeout', 'schema_error', 'disabled', 'error')
            ),
            CONSTRAINT ck_parser_log_latency_non_negative CHECK (latency_ms >= 0)
        );
        CREATE INDEX IF NOT EXISTS idx_parser_logs_session_id ON parser_logs(session_id);
        CREATE INDEX IF NOT EXISTS idx_parser_logs_expires_at ON parser_logs(expires_at);
    """)


def downgrade(conn: sqlite3.Connection) -> None:
    """Drops parser_logs table and indices cleanly."""
    conn.executescript("""
        DROP INDEX IF EXISTS idx_parser_logs_expires_at;
        DROP INDEX IF EXISTS idx_parser_logs_session_id;
        DROP TABLE IF EXISTS parser_logs;
    """)

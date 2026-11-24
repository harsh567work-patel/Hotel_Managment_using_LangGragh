"""
table_split.db.persistence — Production-grade SQLite persistence layer for Table Split Mode.
Implements:
- PRAGMA journal_mode=WAL for high-throughput concurrent reads
- Optimistic locking via session_version (HTTP 409 Conflict protection)
- Fully normalized relational tables + JSON snapshots
- Audit event trail logging
"""

import json
import sqlite3
import time
from pathlib import Path

from config import TABLE_SPLIT_DB_PATH
from table_split.models import AuditEvent, SessionStatus, TableSession


class SessionConcurrencyError(Exception):
    """Raised when an optimistic lock conflict occurs (stale session version)."""


class TableSessionRepository:
    """
    Thread-safe SQLite repository with WAL mode and optimistic concurrency control.
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = Path(db_path or TABLE_SPLIT_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self) -> None:
        """Initializes database tables if not existing."""
        with self._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS table_sessions (
                    session_id TEXT PRIMARY KEY,
                    room_code TEXT UNIQUE NOT NULL,
                    join_token_128 TEXT UNIQUE NOT NULL,
                    host_diner_id TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    session_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_room_code ON table_sessions(room_code);
                CREATE INDEX IF NOT EXISTS idx_sessions_join_token ON table_sessions(join_token_128);

                CREATE TABLE IF NOT EXISTS diners (
                    diner_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    is_host INTEGER NOT NULL DEFAULT 0,
                    guest_token_hash TEXT NOT NULL,
                    payment_status TEXT NOT NULL,
                    paid_amount_paise INTEGER NOT NULL DEFAULT 0,
                    joined_at INTEGER NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES table_sessions(session_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_diners_session_id ON diners(session_id);
                CREATE INDEX IF NOT EXISTS idx_diners_token_hash ON diners(guest_token_hash);

                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY,
                    timestamp INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    diner_id TEXT,
                    action TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    details TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_audit_session_id ON audit_events(session_id);

                CREATE TABLE IF NOT EXISTS payment_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    diner_id TEXT NOT NULL,
                    amount_paise INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    response_json TEXT NOT NULL
                );
            """)
            conn.commit()

    def create_session(self, session: TableSession) -> TableSession:
        """Inserts a brand-new session into SQLite."""
        session_json = session.model_dump_json()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO table_sessions (
                    session_id, room_code, join_token_128, host_diner_id,
                    version, status, session_json, created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.room_code,
                    session.join_token_128,
                    session.host_diner_id,
                    session.version,
                    session.status.value,
                    session_json,
                    session.created_at,
                    session.updated_at,
                    session.expires_at,
                ),
            )
            # Insert individual diners
            for diner in session.diners.values():
                conn.execute(
                    """
                    INSERT INTO diners (
                        diner_id, session_id, name, is_host, guest_token_hash,
                        payment_status, paid_amount_paise, joined_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        diner.diner_id,
                        session.session_id,
                        diner.name,
                        1 if diner.is_host else 0,
                        diner.guest_token_hash,
                        diner.payment_status.value,
                        diner.paid_amount_paise,
                        diner.joined_at,
                    ),
                )
            conn.commit()
        return session

    def update_session(self, session: TableSession, expected_version: int) -> TableSession:
        """
        Updates a session using optimistic concurrency control.
        Increments session version by 1.
        Raises SessionConcurrencyError if expected_version does not match DB version.
        """
        new_version = expected_version + 1
        now = int(time.time())
        updated_session = session.model_copy(update={"version": new_version, "updated_at": now})
        session_json = updated_session.model_dump_json()

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE table_sessions
                SET version = ?, status = ?, session_json = ?, updated_at = ?
                WHERE session_id = ? AND version = ?
                """,
                (
                    new_version,
                    updated_session.status.value,
                    session_json,
                    now,
                    session.session_id,
                    expected_version,
                ),
            )
            if cursor.rowcount == 0:
                raise SessionConcurrencyError(
                    f"Conflict: Session {session.session_id} has been modified concurrently. "
                    f"Expected version {expected_version}."
                )

            # Sync diners table
            conn.execute("DELETE FROM diners WHERE session_id = ?", (session.session_id,))
            for diner in updated_session.diners.values():
                conn.execute(
                    """
                    INSERT INTO diners (
                        diner_id, session_id, name, is_host, guest_token_hash,
                        payment_status, paid_amount_paise, joined_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        diner.diner_id,
                        session.session_id,
                        diner.name,
                        1 if diner.is_host else 0,
                        diner.guest_token_hash,
                        diner.payment_status.value,
                        diner.paid_amount_paise,
                        diner.joined_at,
                    ),
                )
            conn.commit()

        return updated_session

    def get_session(self, session_id: str) -> TableSession | None:
        """Loads a session by session_id, checking TTL expiration."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT session_json, expires_at, status FROM table_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return None

            now = int(time.time())
            data = json.loads(row["session_json"])
            session = TableSession.model_validate(data)

            # Check expiration
            if now >= row["expires_at"] and session.status != SessionStatus.EXPIRED:
                session.status = SessionStatus.EXPIRED
                # Update status in db
                conn.execute(
                    "UPDATE table_sessions SET status = ? WHERE session_id = ?",
                    (SessionStatus.EXPIRED.value, session_id),
                )
                conn.commit()

            return session

    def get_session_by_room_code(self, room_code: str) -> TableSession | None:
        """Loads a session by its secondary room code (e.g. TAB-8K2M)."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT session_id FROM table_sessions WHERE room_code = ?",
                (room_code.upper().strip(),),
            ).fetchone()
            if not row:
                return None
            return self.get_session(row["session_id"])

    def get_session_by_join_token(self, token_128: str) -> TableSession | None:
        """Loads a session by its primary 128-bit join token."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT session_id FROM table_sessions WHERE join_token_128 = ?",
                (token_128.strip(),),
            ).fetchone()
            if not row:
                return None
            return self.get_session(row["session_id"])

    def record_audit_event(self, event: AuditEvent) -> None:
        """Appends an immutable audit event to SQLite."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (
                    event_id, timestamp, session_id, diner_id, action, payload_hash, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.timestamp,
                    event.session_id,
                    event.diner_id,
                    event.action,
                    event.payload_hash,
                    event.details,
                ),
            )
            conn.commit()

    def list_audit_events(self, session_id: str) -> list[AuditEvent]:
        """Retrieves ordered audit history for a session."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE session_id = ? ORDER BY timestamp ASC",
                (session_id,),
            ).fetchall()
            return [
                AuditEvent(
                    event_id=r["event_id"],
                    timestamp=r["timestamp"],
                    session_id=r["session_id"],
                    diner_id=r["diner_id"],
                    action=r["action"],
                    payload_hash=r["payload_hash"],
                    details=r["details"],
                )
                for r in rows
            ]

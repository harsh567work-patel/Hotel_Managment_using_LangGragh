"""
table_split.services.session_reaper — Background maintenance job expiring stale sessions
and purging sensitive preference data & guest credentials past the retention window.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from config import TABLE_SPLIT_DB_PATH, TABLE_SPLIT_RETENTION_SECONDS
from table_split.db.engine import get_sqlite_connection
from table_split.db.migrations.runner import migrate_up
from table_split.sse import SSEHub

logger = logging.getLogger(__name__)


def run_expiry_and_retention_job(
    *,
    db_path: str | Path | None = None,
    now: int | None = None,
    retention_seconds: int = TABLE_SPLIT_RETENTION_SECONDS,
    sse_hub: SSEHub | None = None,
) -> dict[str, Any]:
    """
    Executes two idempotent cleanup passes inside explicit transactions:
    1. Expire active/draft/collecting/proposing/locked/paying sessions whose `expires_at <= now`.
       Transitions status -> 'expired', bumps version, and emits `session.expired`.
    2. Delete sensitive `preferences` rows and clear `guest_token_hash` for expired/cancelled
       sessions past `expires_at + retention_seconds` (default 24h after expiry).
    """
    target_db = Path(db_path or TABLE_SPLIT_DB_PATH)
    migrate_up(target_db)
    ts = int(time.time()) if now is None else int(now)

    expired_session_ids: list[str] = []
    purged_session_ids: list[str] = []
    deleted_preferences_count = 0
    events_to_publish: list[tuple[str, dict[str, Any]]] = []

    conn = get_sqlite_connection(target_db)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE;")
        try:
            # Pass 1: Expire overdue sessions
            overdue_rows = conn.execute(
                """
                SELECT id, version
                FROM table_sessions
                WHERE status NOT IN ('placed', 'cancelled', 'expired')
                  AND expires_at <= ?;
                """,
                (ts,),
            ).fetchall()

            for row in overdue_rows:
                sid = row["id"]
                new_ver = int(row["version"]) + 1
                conn.execute(
                    "UPDATE table_sessions SET status = 'expired', version = ? WHERE id = ?;",
                    (new_ver, sid),
                )
                seq_row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM session_events WHERE session_id = ?;",
                    (sid,),
                ).fetchone()
                next_seq = int(seq_row["next_seq"])
                event_id = str(uuid4())
                payload = {"reason": "ttl_expired", "expired_at": ts}
                conn.execute(
                    """
                    INSERT INTO session_events (
                        id, session_id, participant_id, type, payload, seq, session_version, created_at
                    ) VALUES (?, ?, NULL, 'session.expired', ?, ?, ?, ?);
                    """,
                    (event_id, sid, json.dumps(payload), next_seq, new_ver, ts),
                )
                expired_session_ids.append(sid)
                events_to_publish.append((
                    sid,
                    {
                        "event_id": event_id,
                        "seq": next_seq,
                        "session_id": sid,
                        "session_version": new_ver,
                        "participant_id": None,
                        "type": "session.expired",
                        "payload": payload,
                        "created_at": ts,
                    },
                ))

            # Pass 2: Delete sensitive preferences past retention window
            purge_cutoff = ts - int(retention_seconds)
            purge_rows = conn.execute(
                """
                SELECT id, version
                FROM table_sessions
                WHERE status IN ('expired', 'cancelled')
                  AND sensitive_data_purged_at IS NULL
                  AND expires_at <= ?;
                """,
                (purge_cutoff,),
            ).fetchall()

            for row in purge_rows:
                sid = row["id"]
                del_cur = conn.execute(
                    """
                    DELETE FROM preferences
                    WHERE participant_id IN (SELECT id FROM participants WHERE session_id = ?);
                    """,
                    (sid,),
                )
                deleted_cnt = int(del_cur.rowcount or 0)
                deleted_preferences_count += deleted_cnt

                conn.execute(
                    "UPDATE participants SET guest_token_hash = NULL WHERE session_id = ?;",
                    (sid,),
                )
                conn.execute(
                    "UPDATE table_sessions SET sensitive_data_purged_at = ? WHERE id = ?;",
                    (ts, sid),
                )

                seq_row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM session_events WHERE session_id = ?;",
                    (sid,),
                ).fetchone()
                next_seq = int(seq_row["next_seq"])
                conn.execute(
                    """
                    INSERT INTO session_events (
                        id, session_id, participant_id, type, payload, seq, session_version, created_at
                    ) VALUES (?, ?, NULL, 'session.sensitive_data_purged', ?, ?, ?, ?);
                    """,
                    (
                        str(uuid4()),
                        sid,
                        json.dumps({"deleted_preferences": deleted_cnt, "purged_at": ts}),
                        next_seq,
                        int(row["version"]),
                        ts,
                    ),
                )
                purged_session_ids.append(sid)

            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
    finally:
        conn.close()

    if sse_hub is not None:
        for sid, evt in events_to_publish:
            sse_hub.publish(sid, evt)

    return {
        "expired_count": len(expired_session_ids),
        "expired_session_ids": expired_session_ids,
        "purged_session_count": len(purged_session_ids),
        "purged_session_ids": purged_session_ids,
        "deleted_preferences_count": deleted_preferences_count,
    }

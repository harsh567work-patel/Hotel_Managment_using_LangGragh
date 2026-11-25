"""
table_split.services.session_service — Core session lifecycle, token auth,
optimistic locking (409 + latest state), privacy redaction, and SSE event publishing.
"""

from __future__ import annotations

import hmac
import json
import sqlite3
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from config import (
    TABLE_SPLIT_DB_PATH,
    TABLE_SPLIT_DEFAULT_EXPIRY_SECONDS,
    TABLE_SPLIT_MAX_EXPIRY_SECONDS,
    TABLE_SPLIT_MAX_PARTICIPANTS,
)
from table_split.db.engine import get_sqlite_connection
from table_split.db.migrations.runner import migrate_up
from table_split.security import (
    generate_guest_token,
    generate_join_token_128,
    hash_token,
    verify_token,
)
from table_split.sse import SSEHub

OPEN_FOR_JOIN_STATUSES = ("draft", "collecting", "proposing")
MUTABLE_PREF_STATUSES = ("draft", "collecting", "proposing")
TERMINAL_STATUSES = ("placed", "cancelled", "expired")


class SessionError(Exception):
    """Base error for Table Split session service."""


class SessionNotFoundError(SessionError):
    """Raised when session or participant ID does not exist (HTTP 404)."""


class UnauthorizedError(SessionError):
    """Raised when guest/user credentials are missing or unrecognized (HTTP 401)."""


class ForbiddenError(SessionError):
    """Raised when caller lacks host permission or token is invalid/rotated (HTTP 403)."""


class SessionExpiredError(SessionError):
    """Raised when session or join token has expired (HTTP 410)."""


class SessionValidationError(SessionError):
    """Raised when request parameters violate domain rules (HTTP 400/422)."""


class SessionConflictError(SessionError):
    """
    Raised on optimistic version mismatch, locked state, or participant cap exceeded (HTTP 409).
    Always attaches latest sanitized session state so clients can merge or retry.
    """

    def __init__(self, message: str, latest_state: dict[str, Any], code: str = "SESSION_CONFLICT") -> None:
        self.latest_state = latest_state
        self.code = code
        super().__init__(message)


class SessionService:
    """Thread-safe SQLite-backed session service with optimistic concurrency and SSE fan-out."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        sse_hub: SSEHub | None = None,
    ) -> None:
        self.db_path = Path(db_path or TABLE_SPLIT_DB_PATH)
        self.sse_hub = sse_hub or SSEHub()
        migrate_up(self.db_path)

    def _begin_write(self, conn: sqlite3.Connection) -> None:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE;")

    def _load_session_row(self, conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM table_sessions WHERE id = ?;", (session_id,)).fetchone()
        if row is None:
            raise SessionNotFoundError(f"Table session '{session_id}' not found.")
        return row

    def _build_sanitized_state(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        viewer_participant_id: str | None = None,
    ) -> dict[str, Any]:
        sess = self._load_session_row(conn, session_id)
        p_rows = conn.execute(
            """
            SELECT id, session_id, user_id, display_name, role, status, is_ready, joined_at
            FROM participants
            WHERE session_id = ?
            ORDER BY joined_at ASC, rowid ASC;
            """,
            (session_id,),
        ).fetchall()

        pref_rows = conn.execute(
            """
            SELECT p.*
            FROM preferences p
            JOIN participants part ON part.id = p.participant_id
            WHERE part.session_id = ?
            ORDER BY p.id ASC;
            """,
            (session_id,),
        ).fetchall()

        prefs_by_part: dict[str, list[dict[str, Any]]] = {}
        for pr in pref_rows:
            is_priv = bool(pr["is_private"])
            owner_pid = pr["participant_id"]
            # CRITICAL PRIVACY RULE: private preferences NEVER returned to other participants or anonymous GET
            if is_priv and (viewer_participant_id is None or owner_pid != viewer_participant_id):
                continue
            prefs_by_part.setdefault(owner_pid, []).append({
                "id": pr["id"],
                "kind": pr["kind"],
                "strength": pr["strength"],
                "weight": int(pr["weight"]),
                "value": json.loads(pr["value"]),
                "source": pr["source"],
                "raw_text": pr["raw_text"] if (not is_priv or owner_pid == viewer_participant_id) else None,
                "confirmed_by_user": bool(pr["confirmed_by_user"]),
                "is_private": is_priv,
            })

        participants_out = [
            {
                "id": row["id"],
                "display_name": row["display_name"],
                "role": row["role"],
                "status": row["status"],
                "is_ready": bool(row["is_ready"]),
                "joined_at": int(row["joined_at"]),
                "preferences": prefs_by_part.get(row["id"], []),
            }
            for row in p_rows
        ]

        return {
            "id": sess["id"],
            "restaurant_id": sess["restaurant_id"],
            "host_participant_id": sess["host_participant_id"],
            "status": sess["status"],
            "requires_host_approval": bool(sess["requires_host_approval"]),
            "max_participants": int(sess["max_participants"]),
            "deadline_at": int(sess["deadline_at"]) if sess["deadline_at"] is not None else None,
            "expires_at": int(sess["expires_at"]),
            "version": int(sess["version"]),
            "split_mode": sess["split_mode"],
            "payment_mode": sess["payment_mode"],
            "delivery_address_id": sess["delivery_address_id"],
            "created_at": int(sess["created_at"]),
            "participants": participants_out,
        }

    def _append_event(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        session_version: int,
        event_type: str,
        participant_id: str | None = None,
        payload: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        ts = int(time.time()) if now is None else int(now)
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM session_events WHERE session_id = ?;",
            (session_id,),
        ).fetchone()
        next_seq = int(seq_row["next_seq"])
        event_id = str(uuid4())
        safe_payload = dict(payload or {})
        conn.execute(
            """
            INSERT INTO session_events (
                id, session_id, participant_id, type, payload, seq, session_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                event_id,
                session_id,
                participant_id,
                event_type,
                json.dumps(safe_payload, separators=(",", ":"), default=str),
                next_seq,
                session_version,
                ts,
            ),
        )
        return {
            "event_id": event_id,
            "seq": next_seq,
            "session_id": session_id,
            "session_version": session_version,
            "participant_id": participant_id,
            "type": event_type,
            "payload": safe_payload,
            "created_at": ts,
        }

    def _bump_version(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        expected_version: int,
        extra_set_sql: str = "",
        extra_params: Sequence[Any] = (),
        viewer_participant_id: str | None = None,
    ) -> int:
        set_clause = "version = version + 1"
        if extra_set_sql:
            set_clause += f", {extra_set_sql}"

        cur = conn.execute(
            f"UPDATE table_sessions SET {set_clause} WHERE id = ? AND version = ?;",
            (*extra_params, session_id, expected_version),
        )
        if cur.rowcount != 1:
            latest = self._build_sanitized_state(conn, session_id, viewer_participant_id=viewer_participant_id)
            raise SessionConflictError(
                f"Version conflict: expected {expected_version}, current version is {latest['version']}.",
                latest_state=latest,
                code="VERSION_CONFLICT",
            )
        return expected_version + 1

    def _publish_events(self, session_id: str, events: Sequence[dict[str, Any]]) -> None:
        for evt in events:
            self.sse_hub.publish(session_id, evt)

    def _assert_not_expired(self, sess: sqlite3.Row, now: int) -> None:
        if sess["status"] == "expired" or now >= int(sess["expires_at"]):
            raise SessionExpiredError("This table session has expired.")

    def _assert_not_terminal(
        self,
        conn: sqlite3.Connection,
        sess: sqlite3.Row,
        now: int,
        viewer_participant_id: str | None = None,
    ) -> None:
        self._assert_not_expired(sess, now)
        if sess["status"] in ("placed", "cancelled"):
            latest = self._build_sanitized_state(conn, sess["id"], viewer_participant_id=viewer_participant_id)
            raise SessionConflictError(
                f"Session is '{sess['status']}' and cannot be modified.",
                latest_state=latest,
                code="SESSION_TERMINAL",
            )

    def _require_active_participant(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        participant_id: str,
        *,
        require_host: bool = False,
        allow_pending: bool = False,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM participants WHERE id = ? AND session_id = ?;",
            (participant_id, session_id),
        ).fetchone()
        if row is None:
            raise UnauthorizedError("Invalid session participant credentials.")

        allowed_statuses = ("active", "pending") if allow_pending else ("active",)
        if row["status"] not in allowed_statuses:
            raise ForbiddenError(f"Participant is '{row['status']}' and cannot act in this session.")

        if require_host and row["role"] != "host":
            raise ForbiddenError("Only the active session host can perform this action.")

        return row

    def authenticate_participant(
        self,
        session_id: str,
        raw_guest_token: str | None,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Authenticates caller strictly within `session_id` via scoped guest token or user_id."""
        conn = get_sqlite_connection(self.db_path)
        try:
            self._load_session_row(conn, session_id)
            if raw_guest_token:
                cand_hash = hash_token(raw_guest_token.strip())
                row = conn.execute(
                    """
                    SELECT id, session_id, user_id, guest_token_hash, display_name, role, status
                    FROM participants
                    WHERE session_id = ? AND guest_token_hash = ?;
                    """,
                    (session_id, cand_hash),
                ).fetchone()
                if row is not None and row["guest_token_hash"] and hmac.compare_digest(cand_hash, row["guest_token_hash"]):
                    if row["status"] not in ("active", "pending"):
                        raise ForbiddenError(f"Participant is '{row['status']}' and credentials were revoked.")
                    return dict(row)
                raise UnauthorizedError("Invalid or revoked guest token for this session.")

            if user_id:
                row = conn.execute(
                    """
                    SELECT id, session_id, user_id, guest_token_hash, display_name, role, status
                    FROM participants
                    WHERE session_id = ? AND user_id = ? AND status IN ('active', 'pending');
                    """,
                    (session_id, user_id),
                ).fetchone()
                if row is not None:
                    return dict(row)
                raise UnauthorizedError("Authenticated user is not a member of this session.")

            raise UnauthorizedError("Authentication required: missing guest session cookie or token.")
        finally:
            conn.close()

    def create_session(
        self,
        *,
        display_name: str,
        user_id: str | None = None,
        restaurant_id: str = "spiceroute_default",
        requires_host_approval: bool = False,
        max_participants: int = TABLE_SPLIT_MAX_PARTICIPANTS,
        deadline_at: int | None = None,
        expires_in_seconds: int = TABLE_SPLIT_DEFAULT_EXPIRY_SECONDS,
        split_mode: str = "by_consumption",
        payment_mode: str = "each_pays_own",
        delivery_address_id: str | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        """Creates a new table session with a 128-bit hashed join token and host participant."""
        host_name = display_name.strip()
        if not host_name:
            raise SessionValidationError("display_name cannot be blank.")
        if max_participants < 1 or max_participants > TABLE_SPLIT_MAX_PARTICIPANTS:
            raise SessionValidationError(
                f"max_participants must be between 1 and {TABLE_SPLIT_MAX_PARTICIPANTS}."
            )
        if expires_in_seconds < 60 or expires_in_seconds > TABLE_SPLIT_MAX_EXPIRY_SECONDS:
            raise SessionValidationError(
                f"expires_in_seconds must be between 60 and {TABLE_SPLIT_MAX_EXPIRY_SECONDS} (24h max)."
            )
        if split_mode not in ("by_consumption", "equal"):
            raise SessionValidationError(f"Unsupported split_mode '{split_mode}'.")
        if payment_mode not in ("each_pays_own", "host_pays_all"):
            raise SessionValidationError(f"Unsupported payment_mode '{payment_mode}'.")
        if deadline_at is not None and deadline_at <= 0:
            raise SessionValidationError("deadline_at must be a positive Unix epoch timestamp.")

        ts = int(time.time()) if now is None else int(now)
        session_id = str(uuid4())
        host_pid = str(uuid4())
        raw_join_token = generate_join_token_128()
        join_token_hash = hash_token(raw_join_token)

        raw_host_token: str | None = None
        host_token_hash: str | None = None
        if user_id is None:
            raw_host_token = generate_guest_token()
            host_token_hash = hash_token(raw_host_token)

        expires_at = ts + int(expires_in_seconds)

        conn = get_sqlite_connection(self.db_path)
        events_to_publish: list[dict[str, Any]] = []
        try:
            self._begin_write(conn)
            try:
                conn.execute(
                    """
                    INSERT INTO table_sessions (
                        id, restaurant_id, host_participant_id, status, join_token_hash,
                        requires_host_approval, max_participants, deadline_at, expires_at,
                        version, split_mode, payment_mode, delivery_address_id, created_at
                    ) VALUES (?, ?, ?, 'collecting', ?, ?, ?, ?, ?, 1, ?, ?, ?, ?);
                    """,
                    (
                        session_id,
                        restaurant_id,
                        host_pid,
                        join_token_hash,
                        1 if requires_host_approval else 0,
                        max_participants,
                        deadline_at,
                        expires_at,
                        split_mode,
                        payment_mode,
                        delivery_address_id,
                        ts,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO participants (
                        id, session_id, user_id, guest_token_hash, display_name,
                        role, status, is_ready, joined_at
                    ) VALUES (?, ?, ?, ?, ?, 'host', 'active', 0, ?);
                    """,
                    (host_pid, session_id, user_id, host_token_hash, host_name, ts),
                )
                evt = self._append_event(
                    conn,
                    session_id=session_id,
                    session_version=1,
                    event_type="session.created",
                    participant_id=host_pid,
                    payload={
                        "host_participant_id": host_pid,
                        "host_display_name": host_name,
                        "status": "collecting",
                        "expires_at": expires_at,
                    },
                    now=ts,
                )
                events_to_publish.append(evt)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            state = self._build_sanitized_state(conn, session_id, viewer_participant_id=host_pid)
        finally:
            conn.close()

        self._publish_events(session_id, events_to_publish)
        return {
            "session": state,
            "session_id": session_id,
            "share_link": f"/table-sessions/{session_id}/join?token={raw_join_token}",
            "join_token": raw_join_token,
            "host_participant_id": host_pid,
            "host_token": raw_host_token,
        }

    def get_session_state(
        self,
        session_id: str,
        *,
        viewer_participant_id: str | None = None,
    ) -> dict[str, Any]:
        conn = get_sqlite_connection(self.db_path)
        try:
            return self._build_sanitized_state(conn, session_id, viewer_participant_id=viewer_participant_id)
        finally:
            conn.close()

    def join_session(
        self,
        session_id: str,
        *,
        join_token: str,
        display_name: str,
        user_id: str | None = None,
        expected_version: int | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        guest_name = display_name.strip()
        if not guest_name:
            raise SessionValidationError("display_name cannot be blank.")
        if not join_token or not join_token.strip():
            raise ForbiddenError("Missing join token.")

        ts = int(time.time()) if now is None else int(now)
        conn = get_sqlite_connection(self.db_path)
        events_to_publish: list[dict[str, Any]] = []

        try:
            self._begin_write(conn)
            try:
                sess = self._load_session_row(conn, session_id)
                # Verify join token constant-time before disclosing expiry, lock, or capacity state
                if not verify_token(join_token.strip(), sess["join_token_hash"]):
                    raise ForbiddenError("Invalid or rotated join token.")

                self._assert_not_expired(sess, ts)

                if sess["status"] not in OPEN_FOR_JOIN_STATUSES:
                    latest = self._build_sanitized_state(conn, session_id)
                    raise SessionConflictError(
                        f"Session is '{sess['status']}' and is not open for joining.",
                        latest_state=latest,
                        code="SESSION_NOT_JOINABLE",
                    )

                if expected_version is not None and expected_version != int(sess["version"]):
                    latest = self._build_sanitized_state(conn, session_id)
                    raise SessionConflictError(
                        f"Version conflict: expected {expected_version}, current is {sess['version']}.",
                        latest_state=latest,
                        code="VERSION_CONFLICT",
                    )

                # Enforce server-side participant cap (active + pending <= max_participants, max 12)
                cnt_row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM participants WHERE session_id = ? AND status IN ('active', 'pending');",
                    (session_id,),
                ).fetchone()
                current_count = int(cnt_row["cnt"])
                max_cap = min(int(sess["max_participants"]), TABLE_SPLIT_MAX_PARTICIPANTS)
                if current_count >= max_cap:
                    latest = self._build_sanitized_state(conn, session_id)
                    raise SessionConflictError(
                        f"Session is full: maximum of {max_cap} participants reached.",
                        latest_state=latest,
                        code="MAX_PARTICIPANTS_REACHED",
                    )

                pid = str(uuid4())
                p_status = "pending" if bool(sess["requires_host_approval"]) else "active"
                raw_guest_token: str | None = None
                guest_token_hash: str | None = None
                if user_id is None:
                    raw_guest_token = generate_guest_token()
                    guest_token_hash = hash_token(raw_guest_token)

                new_version = self._bump_version(conn, session_id, int(sess["version"]))
                conn.execute(
                    """
                    INSERT INTO participants (
                        id, session_id, user_id, guest_token_hash, display_name,
                        role, status, is_ready, joined_at
                    ) VALUES (?, ?, ?, ?, ?, 'member', ?, 0, ?);
                    """,
                    (pid, session_id, user_id, guest_token_hash, guest_name, p_status, ts),
                )
                event_type = "participant.pending" if p_status == "pending" else "participant.joined"
                evt = self._append_event(
                    conn,
                    session_id=session_id,
                    session_version=new_version,
                    event_type=event_type,
                    participant_id=pid,
                    payload={
                        "participant_id": pid,
                        "display_name": guest_name,
                        "status": p_status,
                        "role": "member",
                    },
                    now=ts,
                )
                events_to_publish.append(evt)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

            state = self._build_sanitized_state(conn, session_id, viewer_participant_id=pid)
        finally:
            conn.close()

        self._publish_events(session_id, events_to_publish)
        return {
            "participant_id": pid,
            "guest_token": raw_guest_token,
            "status": p_status,
            "session": state,
        }

    def approve_participant(
        self,
        session_id: str,
        *,
        host_participant_id: str,
        target_participant_id: str,
        expected_version: int,
        now: int | None = None,
    ) -> dict[str, Any]:
        ts = int(time.time()) if now is None else int(now)
        conn = get_sqlite_connection(self.db_path)
        events_to_publish: list[dict[str, Any]] = []

        try:
            self._begin_write(conn)
            try:
                sess = self._load_session_row(conn, session_id)
                self._assert_not_terminal(conn, sess, ts, viewer_participant_id=host_participant_id)
                self._require_active_participant(conn, session_id, host_participant_id, require_host=True)

                target = conn.execute(
                    "SELECT * FROM participants WHERE id = ? AND session_id = ?;",
                    (target_participant_id, session_id),
                ).fetchone()
                if target is None:
                    raise SessionNotFoundError(f"Participant '{target_participant_id}' not found.")

                if target["status"] != "pending":
                    latest = self._build_sanitized_state(conn, session_id, viewer_participant_id=host_participant_id)
                    raise SessionConflictError(
                        f"Participant '{target_participant_id}' is '{target['status']}', not 'pending'.",
                        latest_state=latest,
                        code="INVALID_PARTICIPANT_STATE",
                    )

                new_version = self._bump_version(
                    conn, session_id, expected_version, viewer_participant_id=host_participant_id
                )
                conn.execute(
                    "UPDATE participants SET status = 'active' WHERE id = ? AND session_id = ?;",
                    (target_participant_id, session_id),
                )
                evt = self._append_event(
                    conn,
                    session_id=session_id,
                    session_version=new_version,
                    event_type="participant.approved",
                    participant_id=target_participant_id,
                    payload={"participant_id": target_participant_id, "approved_by": host_participant_id},
                    now=ts,
                )
                events_to_publish.append(evt)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

            state = self._build_sanitized_state(conn, session_id, viewer_participant_id=host_participant_id)
        finally:
            conn.close()

        self._publish_events(session_id, events_to_publish)
        return state

    def remove_participant(
        self,
        session_id: str,
        *,
        host_participant_id: str,
        target_participant_id: str,
        expected_version: int,
        now: int | None = None,
    ) -> dict[str, Any]:
        ts = int(time.time()) if now is None else int(now)
        if host_participant_id == target_participant_id:
            raise SessionValidationError("Host cannot remove themselves; use /leave or /transfer-host.")

        conn = get_sqlite_connection(self.db_path)
        events_to_publish: list[dict[str, Any]] = []

        try:
            self._begin_write(conn)
            try:
                sess = self._load_session_row(conn, session_id)
                self._assert_not_terminal(conn, sess, ts, viewer_participant_id=host_participant_id)
                self._require_active_participant(conn, session_id, host_participant_id, require_host=True)

                target = conn.execute(
                    "SELECT * FROM participants WHERE id = ? AND session_id = ?;",
                    (target_participant_id, session_id),
                ).fetchone()
                if target is None:
                    raise SessionNotFoundError(f"Participant '{target_participant_id}' not found.")

                new_version = self._bump_version(
                    conn, session_id, expected_version, viewer_participant_id=host_participant_id
                )
                conn.execute(
                    "UPDATE participants SET status = 'removed', guest_token_hash = NULL WHERE id = ? AND session_id = ?;",
                    (target_participant_id, session_id),
                )
                evt = self._append_event(
                    conn,
                    session_id=session_id,
                    session_version=new_version,
                    event_type="participant.removed",
                    participant_id=target_participant_id,
                    payload={"participant_id": target_participant_id, "removed_by": host_participant_id},
                    now=ts,
                )
                events_to_publish.append(evt)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

            state = self._build_sanitized_state(conn, session_id, viewer_participant_id=host_participant_id)
        finally:
            conn.close()

        self._publish_events(session_id, events_to_publish)
        return state

    def transfer_host(
        self,
        session_id: str,
        *,
        host_participant_id: str,
        target_participant_id: str,
        expected_version: int,
        now: int | None = None,
    ) -> dict[str, Any]:
        ts = int(time.time()) if now is None else int(now)
        if host_participant_id == target_participant_id:
            raise SessionValidationError("Target participant is already the session host.")

        conn = get_sqlite_connection(self.db_path)
        events_to_publish: list[dict[str, Any]] = []

        try:
            self._begin_write(conn)
            try:
                sess = self._load_session_row(conn, session_id)
                self._assert_not_terminal(conn, sess, ts, viewer_participant_id=host_participant_id)
                self._require_active_participant(conn, session_id, host_participant_id, require_host=True)

                target = conn.execute(
                    "SELECT * FROM participants WHERE id = ? AND session_id = ?;",
                    (target_participant_id, session_id),
                ).fetchone()
                if target is None:
                    raise SessionNotFoundError(f"Participant '{target_participant_id}' not found.")
                if target["status"] != "active":
                    latest = self._build_sanitized_state(conn, session_id, viewer_participant_id=host_participant_id)
                    raise SessionConflictError(
                        "Target participant must be active to become host.",
                        latest_state=latest,
                        code="INVALID_HOST_TARGET",
                    )

                new_version = self._bump_version(
                    conn,
                    session_id,
                    expected_version,
                    extra_set_sql="host_participant_id = ?",
                    extra_params=(target_participant_id,),
                    viewer_participant_id=host_participant_id,
                )
                conn.execute(
                    "UPDATE participants SET role = 'member' WHERE id = ? AND session_id = ?;",
                    (host_participant_id, session_id),
                )
                conn.execute(
                    "UPDATE participants SET role = 'host' WHERE id = ? AND session_id = ?;",
                    (target_participant_id, session_id),
                )
                evt = self._append_event(
                    conn,
                    session_id=session_id,
                    session_version=new_version,
                    event_type="session.host_transferred",
                    participant_id=host_participant_id,
                    payload={
                        "old_host_id": host_participant_id,
                        "new_host_id": target_participant_id,
                        "reason": "explicit_transfer",
                    },
                    now=ts,
                )
                events_to_publish.append(evt)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

            state = self._build_sanitized_state(conn, session_id, viewer_participant_id=host_participant_id)
        finally:
            conn.close()

        self._publish_events(session_id, events_to_publish)
        return state

    def leave_session(
        self,
        session_id: str,
        *,
        participant_id: str,
        expected_version: int,
        now: int | None = None,
    ) -> dict[str, Any]:
        """
        Marks participant as left and clears guest token.
        If the departing participant is host:
          - Promotes earliest-joined active member and emits `session.host_transferred`.
          - If zero active members remain, transitions session to `cancelled` and emits `session.cancelled`.
        """
        ts = int(time.time()) if now is None else int(now)
        conn = get_sqlite_connection(self.db_path)
        events_to_publish: list[dict[str, Any]] = []

        try:
            self._begin_write(conn)
            try:
                sess = self._load_session_row(conn, session_id)
                self._assert_not_terminal(conn, sess, ts, viewer_participant_id=participant_id)
                actor = self._require_active_participant(conn, session_id, participant_id, allow_pending=True)

                is_host = actor["role"] == "host" or sess["host_participant_id"] == participant_id

                # Look up earliest-joined active peer before updating version
                next_host_row = None
                if is_host:
                    next_host_row = conn.execute(
                        """
                        SELECT id, display_name
                        FROM participants
                        WHERE session_id = ? AND id != ? AND status = 'active'
                        ORDER BY joined_at ASC, rowid ASC
                        LIMIT 1;
                        """,
                        (session_id, participant_id),
                    ).fetchone()

                if is_host and next_host_row is not None:
                    extra_sql = "host_participant_id = ?"
                    extra_params: tuple[Any, ...] = (next_host_row["id"],)
                elif is_host:
                    extra_sql = "host_participant_id = NULL, status = 'cancelled'"
                    extra_params = ()
                else:
                    extra_sql = ""
                    extra_params = ()

                new_version = self._bump_version(
                    conn,
                    session_id,
                    expected_version,
                    extra_set_sql=extra_sql,
                    extra_params=extra_params,
                )

                conn.execute(
                    "UPDATE participants SET status = 'left', role = 'member', guest_token_hash = NULL WHERE id = ? AND session_id = ?;",
                    (participant_id, session_id),
                )

                left_evt = self._append_event(
                    conn,
                    session_id=session_id,
                    session_version=new_version,
                    event_type="participant.left",
                    participant_id=participant_id,
                    payload={"participant_id": participant_id, "was_host": is_host},
                    now=ts,
                )
                events_to_publish.append(left_evt)

                if is_host and next_host_row is not None:
                    conn.execute(
                        "UPDATE participants SET role = 'host' WHERE id = ? AND session_id = ?;",
                        (next_host_row["id"], session_id),
                    )
                    transfer_evt = self._append_event(
                        conn,
                        session_id=session_id,
                        session_version=new_version,
                        event_type="session.host_transferred",
                        participant_id=next_host_row["id"],
                        payload={
                            "old_host_id": participant_id,
                            "new_host_id": next_host_row["id"],
                            "reason": "host_left",
                        },
                        now=ts,
                    )
                    events_to_publish.append(transfer_evt)
                elif is_host:
                    cancel_evt = self._append_event(
                        conn,
                        session_id=session_id,
                        session_version=new_version,
                        event_type="session.cancelled",
                        participant_id=participant_id,
                        payload={"reason": "host_left_no_active_members"},
                        now=ts,
                    )
                    events_to_publish.append(cancel_evt)

                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

            state = self._build_sanitized_state(conn, session_id)
        finally:
            conn.close()

        self._publish_events(session_id, events_to_publish)
        return state

    def lock_session(
        self,
        session_id: str,
        *,
        host_participant_id: str,
        expected_version: int,
        now: int | None = None,
    ) -> dict[str, Any]:
        ts = int(time.time()) if now is None else int(now)
        conn = get_sqlite_connection(self.db_path)
        events_to_publish: list[dict[str, Any]] = []

        try:
            self._begin_write(conn)
            try:
                sess = self._load_session_row(conn, session_id)
                self._assert_not_terminal(conn, sess, ts, viewer_participant_id=host_participant_id)
                self._require_active_participant(conn, session_id, host_participant_id, require_host=True)

                if sess["status"] in TERMINAL_STATUSES or sess["status"] == "locked":
                    latest = self._build_sanitized_state(conn, session_id, viewer_participant_id=host_participant_id)
                    raise SessionConflictError(
                        f"Cannot lock session from status '{sess['status']}'.",
                        latest_state=latest,
                        code="INVALID_LOCK_TRANSITION",
                    )

                new_version = self._bump_version(
                    conn,
                    session_id,
                    expected_version,
                    extra_set_sql="status = 'locked'",
                    viewer_participant_id=host_participant_id,
                )
                evt = self._append_event(
                    conn,
                    session_id=session_id,
                    session_version=new_version,
                    event_type="session.locked",
                    participant_id=host_participant_id,
                    payload={"locked_by": host_participant_id},
                    now=ts,
                )
                events_to_publish.append(evt)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

            state = self._build_sanitized_state(conn, session_id, viewer_participant_id=host_participant_id)
        finally:
            conn.close()

        self._publish_events(session_id, events_to_publish)
        return state

    def unlock_session(
        self,
        session_id: str,
        *,
        host_participant_id: str,
        expected_version: int,
        now: int | None = None,
    ) -> dict[str, Any]:
        ts = int(time.time()) if now is None else int(now)
        conn = get_sqlite_connection(self.db_path)
        events_to_publish: list[dict[str, Any]] = []

        try:
            self._begin_write(conn)
            try:
                sess = self._load_session_row(conn, session_id)
                self._assert_not_terminal(conn, sess, ts, viewer_participant_id=host_participant_id)
                self._require_active_participant(conn, session_id, host_participant_id, require_host=True)

                if sess["status"] != "locked":
                    latest = self._build_sanitized_state(conn, session_id, viewer_participant_id=host_participant_id)
                    raise SessionConflictError(
                        f"Cannot unlock session from status '{sess['status']}'.",
                        latest_state=latest,
                        code="INVALID_UNLOCK_TRANSITION",
                    )

                new_version = self._bump_version(
                    conn,
                    session_id,
                    expected_version,
                    extra_set_sql="status = 'collecting'",
                    viewer_participant_id=host_participant_id,
                )
                evt = self._append_event(
                    conn,
                    session_id=session_id,
                    session_version=new_version,
                    event_type="session.unlocked",
                    participant_id=host_participant_id,
                    payload={"unlocked_by": host_participant_id},
                    now=ts,
                )
                events_to_publish.append(evt)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

            state = self._build_sanitized_state(conn, session_id, viewer_participant_id=host_participant_id)
        finally:
            conn.close()

        self._publish_events(session_id, events_to_publish)
        return state

    def rotate_join_link(
        self,
        session_id: str,
        *,
        host_participant_id: str,
        expected_version: int,
        now: int | None = None,
    ) -> dict[str, Any]:
        ts = int(time.time()) if now is None else int(now)
        new_raw_token = generate_join_token_128()
        new_hash = hash_token(new_raw_token)

        conn = get_sqlite_connection(self.db_path)
        events_to_publish: list[dict[str, Any]] = []

        try:
            self._begin_write(conn)
            try:
                sess = self._load_session_row(conn, session_id)
                self._assert_not_terminal(conn, sess, ts, viewer_participant_id=host_participant_id)
                self._require_active_participant(conn, session_id, host_participant_id, require_host=True)

                new_version = self._bump_version(
                    conn,
                    session_id,
                    expected_version,
                    extra_set_sql="join_token_hash = ?",
                    extra_params=(new_hash,),
                    viewer_participant_id=host_participant_id,
                )
                evt = self._append_event(
                    conn,
                    session_id=session_id,
                    session_version=new_version,
                    event_type="session.link_rotated",
                    participant_id=host_participant_id,
                    payload={"rotated_by": host_participant_id},
                    now=ts,
                )
                events_to_publish.append(evt)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

            state = self._build_sanitized_state(conn, session_id, viewer_participant_id=host_participant_id)
        finally:
            conn.close()

        self._publish_events(session_id, events_to_publish)
        return {
            "session": state,
            "join_token": new_raw_token,
            "share_link": f"/table-sessions/{session_id}/join?token={new_raw_token}",
        }

    def extend_expiry(
        self,
        session_id: str,
        *,
        host_participant_id: str,
        extend_seconds: int,
        expected_version: int,
        now: int | None = None,
    ) -> dict[str, Any]:
        ts = int(time.time()) if now is None else int(now)
        if extend_seconds <= 0:
            raise SessionValidationError("extend_seconds must be positive.")

        conn = get_sqlite_connection(self.db_path)
        events_to_publish: list[dict[str, Any]] = []

        try:
            self._begin_write(conn)
            try:
                sess = self._load_session_row(conn, session_id)
                self._assert_not_terminal(conn, sess, ts, viewer_participant_id=host_participant_id)
                self._require_active_participant(conn, session_id, host_participant_id, require_host=True)

                max_expires_at = int(sess["created_at"]) + TABLE_SPLIT_MAX_EXPIRY_SECONDS
                new_expires_at = min(int(sess["expires_at"]) + extend_seconds, max_expires_at)

                new_version = self._bump_version(
                    conn,
                    session_id,
                    expected_version,
                    extra_set_sql="expires_at = ?",
                    extra_params=(new_expires_at,),
                    viewer_participant_id=host_participant_id,
                )
                evt = self._append_event(
                    conn,
                    session_id=session_id,
                    session_version=new_version,
                    event_type="session.extended",
                    participant_id=host_participant_id,
                    payload={"expires_at": new_expires_at},
                    now=ts,
                )
                events_to_publish.append(evt)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

            state = self._build_sanitized_state(conn, session_id, viewer_participant_id=host_participant_id)
        finally:
            conn.close()

        self._publish_events(session_id, events_to_publish)
        return state

    def update_preferences(
        self,
        session_id: str,
        *,
        participant_id: str,
        expected_version: int,
        preferences: Sequence[Mapping[str, Any]],
        is_ready: bool | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        ts = int(time.time()) if now is None else int(now)
        conn = get_sqlite_connection(self.db_path)
        events_to_publish: list[dict[str, Any]] = []

        try:
            self._begin_write(conn)
            try:
                sess = self._load_session_row(conn, session_id)
                self._assert_not_terminal(conn, sess, ts, viewer_participant_id=participant_id)
                self._require_active_participant(conn, session_id, participant_id)

                if sess["status"] not in MUTABLE_PREF_STATUSES:
                    latest = self._build_sanitized_state(conn, session_id, viewer_participant_id=participant_id)
                    raise SessionConflictError(
                        f"Preferences cannot be modified while session is '{sess['status']}'.",
                        latest_state=latest,
                        code="SESSION_LOCKED",
                    )

                for pref in preferences:
                    kind = str(pref["kind"])
                    strength = str(pref["strength"]).upper()
                    if kind in ("diet", "allergen") and strength != "HARD":
                        raise SessionValidationError(f"Preference kind '{kind}' MUST have strength='HARD'.")

                new_version = self._bump_version(
                    conn, session_id, expected_version, viewer_participant_id=participant_id
                )

                conn.execute("DELETE FROM preferences WHERE participant_id = ?;", (participant_id,))
                for pref in preferences:
                    conn.execute(
                        """
                        INSERT INTO preferences (
                            id, participant_id, kind, strength, weight, value,
                            source, raw_text, parser_confidence, confirmed_by_user, is_private
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            str(uuid4()),
                            participant_id,
                            str(pref["kind"]),
                            str(pref["strength"]).upper(),
                            int(pref.get("weight", 1)),
                            json.dumps(pref.get("value", {})),
                            str(pref.get("source", "chip")),
                            pref.get("raw_text"),
                            pref.get("parser_confidence"),
                            1 if pref.get("confirmed_by_user", True) else 0,
                            1 if pref.get("is_private", False) else 0,
                        ),
                    )

                if is_ready is not None:
                    conn.execute(
                        "UPDATE participants SET is_ready = ? WHERE id = ? AND session_id = ?;",
                        (1 if is_ready else 0, participant_id, session_id),
                    )

                # CRITICAL PRIVACY RULE: Never embed raw preference values or raw_text in SSE events!
                evt = self._append_event(
                    conn,
                    session_id=session_id,
                    session_version=new_version,
                    event_type="preference.updated",
                    participant_id=participant_id,
                    payload={
                        "participant_id": participant_id,
                        "preference_count": len(preferences),
                        "is_ready": is_ready,
                    },
                    now=ts,
                )
                events_to_publish.append(evt)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

            state = self._build_sanitized_state(conn, session_id, viewer_participant_id=participant_id)
        finally:
            conn.close()

        self._publish_events(session_id, events_to_publish)
        return state

    def post_message(
        self,
        session_id: str,
        *,
        participant_id: str,
        expected_version: int,
        message: str,
        now: int | None = None,
    ) -> dict[str, Any]:
        text = message.strip()
        if not text:
            raise SessionValidationError("message cannot be empty.")

        ts = int(time.time()) if now is None else int(now)
        conn = get_sqlite_connection(self.db_path)
        events_to_publish: list[dict[str, Any]] = []

        try:
            self._begin_write(conn)
            try:
                sess = self._load_session_row(conn, session_id)
                self._assert_not_terminal(conn, sess, ts, viewer_participant_id=participant_id)
                actor = self._require_active_participant(conn, session_id, participant_id)

                new_version = self._bump_version(
                    conn, session_id, expected_version, viewer_participant_id=participant_id
                )
                evt = self._append_event(
                    conn,
                    session_id=session_id,
                    session_version=new_version,
                    event_type="session.message",
                    participant_id=participant_id,
                    payload={
                        "participant_id": participant_id,
                        "display_name": actor["display_name"],
                        "message": text,
                    },
                    now=ts,
                )
                events_to_publish.append(evt)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

            state = self._build_sanitized_state(conn, session_id, viewer_participant_id=participant_id)
        finally:
            conn.close()

        self._publish_events(session_id, events_to_publish)
        return state

    def get_replay_bundle(
        self,
        session_id: str,
        *,
        after_seq: int | None = None,
        after_version: int | None = None,
        after_event_id: str | None = None,
    ) -> tuple[bool, list[dict[str, Any]], int]:
        """
        Loads ordered persisted events for reconnect replay along with current `max_seq`.
        Returns `(needs_snapshot, events, max_seq)`:
        - If `after_event_id` is supplied as a UUID (or non-integer) that isn't found in history,
          OR if `resolved_seq > max_seq` / `after_version > current_version`, `needs_snapshot=True`.
        - If neither `after_seq`, `after_version`, nor `after_event_id` is given, returns all events cleanly
          and sets `needs_snapshot=True` only if history is empty.
        """
        conn = get_sqlite_connection(self.db_path)
        try:
            sess = self._load_session_row(conn, session_id)
            max_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM session_events WHERE session_id = ?;",
                (session_id,),
            ).fetchone()
            max_seq = int(max_row["max_seq"])
            resolved_seq = after_seq

            if resolved_seq is None and after_event_id is not None:
                cleaned_id = after_event_id.strip()
                if cleaned_id.isdigit():
                    resolved_seq = int(cleaned_id)
                else:
                    match_row = conn.execute(
                        "SELECT seq FROM session_events WHERE session_id = ? AND id = ?;",
                        (session_id, cleaned_id),
                    ).fetchone()
                    if match_row is None:
                        return True, [], max_seq
                    resolved_seq = int(match_row["seq"])

            if resolved_seq is not None:
                if resolved_seq > max_seq:
                    return True, [], max_seq
                rows = conn.execute(
                    "SELECT * FROM session_events WHERE session_id = ? AND seq > ? ORDER BY seq ASC;",
                    (session_id, resolved_seq),
                ).fetchall()
                needs_snapshot = False
            elif after_version is not None:
                if after_version > int(sess["version"]):
                    return True, [], max_seq
                rows = conn.execute(
                    "SELECT * FROM session_events WHERE session_id = ? AND session_version > ? ORDER BY seq ASC;",
                    (session_id, after_version),
                ).fetchall()
                needs_snapshot = False
            else:
                rows = conn.execute(
                    "SELECT * FROM session_events WHERE session_id = ? ORDER BY seq ASC;",
                    (session_id,),
                ).fetchall()
                needs_snapshot = len(rows) == 0

            events = [
                {
                    "event_id": r["id"],
                    "seq": int(r["seq"]),
                    "session_id": r["session_id"],
                    "session_version": int(r["session_version"]),
                    "participant_id": r["participant_id"],
                    "type": r["type"],
                    "payload": json.loads(r["payload"] or "{}"),
                    "created_at": int(r["created_at"]),
                }
                for r in rows
            ]
            return needs_snapshot, events, max_seq
        finally:
            conn.close()

    def list_events_since(
        self,
        session_id: str,
        *,
        after_seq: int | None = None,
        after_version: int | None = None,
        after_event_id: str | None = None,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Backwards-compatible wrapper returning `(needs_snapshot, events)`."""
        needs_snapshot, events, _ = self.get_replay_bundle(
            session_id,
            after_seq=after_seq,
            after_version=after_version,
            after_event_id=after_event_id,
        )
        return needs_snapshot, events

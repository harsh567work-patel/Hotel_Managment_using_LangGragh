"""
tests.test_table_split_stage4 — Stage 4 Session Lifecycle, Auth, Optimistic Concurrency,
SSE Reconnect/Privacy, Rate Limiting, and Expiry/Purge Job Verification.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from api import app
from table_split.api_sessions import get_rate_limiter, get_session_service, get_sse_hub
from table_split.db.engine import get_sqlite_connection
from table_split.db.migrations.runner import migrate_down, migrate_up
from table_split.rate_limit import RateLimitRule, SlidingWindowRateLimiter
from table_split.services.session_reaper import run_expiry_and_retention_job
from table_split.services.session_service import SessionService
from table_split.sse import SSEHub


@pytest.fixture
def stage4_env(tmp_path: Path) -> Generator[tuple[TestClient, Path, SessionService, SlidingWindowRateLimiter, SSEHub], None, None]:
    """Configures isolated SQLite DB, fresh SSEHub, and rate limiter per test."""
    db_file = tmp_path / "stage4_sessions.db"
    hub = SSEHub()
    limiter = SlidingWindowRateLimiter({
        "join_ip": RateLimitRule(max_requests=100, window_seconds=60),
        "join_token": RateLimitRule(max_requests=100, window_seconds=60),
        "preference_participant": RateLimitRule(max_requests=100, window_seconds=60),
        "message_participant": RateLimitRule(max_requests=100, window_seconds=60),
    })
    service = SessionService(db_path=db_file, sse_hub=hub)

    app.dependency_overrides[get_sse_hub] = lambda: hub
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    app.dependency_overrides[get_session_service] = lambda: service

    client = TestClient(app)
    try:
        yield client, db_file, service, limiter, hub
    finally:
        app.dependency_overrides.clear()


def _parse_sse_frames(raw_text: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for block in raw_text.strip().split("\n\n"):
        if not block.strip() or block.strip().startswith(":"):
            continue
        frame: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith("id:"):
                frame["id"] = line[3:].strip()
            elif line.startswith("event:"):
                frame["event"] = line[6:].strip()
            elif line.startswith("data:"):
                frame["data"] = json.loads(line[5:].strip())
        if frame:
            frames.append(frame)
    return frames


# ===========================================================================
# 1. Migration 003 Reversibility
# ===========================================================================
def test_migration_003_up_and_down(tmp_path: Path) -> None:
    db_path = tmp_path / "mig003.db"
    applied = migrate_up(db_path)
    assert "003" in applied

    conn = get_sqlite_connection(db_path)
    evt_cols = {r[1] for r in conn.execute("PRAGMA table_info(session_events);").fetchall()}
    sess_cols = {r[1] for r in conn.execute("PRAGMA table_info(table_sessions);").fetchall()}
    conn.close()

    assert {"seq", "session_version"}.issubset(evt_cols)
    assert "sensitive_data_purged_at" in sess_cols

    steps_to_003 = len(applied) - applied.index("003")
    rolled = migrate_down(db_path, steps=steps_to_003)
    assert "003" in rolled

    conn2 = get_sqlite_connection(db_path)
    evt_cols_after = {r[1] for r in conn2.execute("PRAGMA table_info(session_events);").fetchall()}
    sess_cols_after = {r[1] for r in conn2.execute("PRAGMA table_info(table_sessions);").fetchall()}
    conn2.close()

    assert "seq" not in evt_cols_after
    assert "session_version" not in evt_cols_after
    assert "sensitive_data_purged_at" not in sess_cols_after


# ===========================================================================
# 2. Session Create, Scoped HttpOnly Cookies, Participant Cap (13th rejected)
# ===========================================================================
def test_create_session_cookie_and_token_security(stage4_env: Any) -> None:
    client, db_file, _, _, _ = stage4_env

    res = client.post("/table-sessions", json={"display_name": "Host Aresh"})
    assert res.status_code == 201
    data = res.json()

    sid = data["session_id"]
    join_token = data["join_token"]
    host_token = data["host_token"]
    assert len(join_token) >= 22  # 128-bit URL-safe
    assert host_token is not None
    assert data["share_link"] == f"/table-sessions/{sid}/join?token={join_token}"
    assert data["session"]["version"] == 1
    assert data["session"]["expires_at"] == data["session"]["created_at"] + 7200  # 2h default

    # Verify HttpOnly scoped cookie header on create response
    set_cookie = res.headers.get("set-cookie", "")
    assert f"ts_guest_{sid}=" in set_cookie
    assert "HttpOnly" in set_cookie or "httponly" in set_cookie.lower()
    assert f"Path=/table-sessions/{sid}" in set_cookie or f"path=/table-sessions/{sid}" in set_cookie.lower()

    # Verify plaintext tokens are NOT stored in SQLite
    conn = get_sqlite_connection(db_file)
    sess_row = conn.execute("SELECT join_token_hash FROM table_sessions WHERE id = ?;", (sid,)).fetchone()
    part_row = conn.execute("SELECT guest_token_hash FROM participants WHERE session_id = ?;", (sid,)).fetchone()
    conn.close()
    assert sess_row["join_token_hash"] != join_token
    assert part_row["guest_token_hash"] != host_token

    # Verify host client's automatic scoped HttpOnly cookie authenticates Session A without extra header
    lock_self = client.post(f"/table-sessions/{sid}/lock", json={"version": 1})
    assert lock_self.status_code == 200

    # Verify host token from Session A is rejected on Session B (using a separate uncookied client)
    client_other = TestClient(app)
    res2 = client_other.post("/table-sessions", json={"display_name": "Other Host"})
    sid2 = res2.json()["session_id"]
    lock_bad = client.post(
        f"/table-sessions/{sid2}/lock",
        json={"version": 1},
        headers={"X-Guest-Token": host_token},
    )
    assert lock_bad.status_code == 401


def test_participant_cap_rejects_13th_joiner(stage4_env: Any) -> None:
    client, _, _, _, _ = stage4_env
    res = client.post("/table-sessions", json={"display_name": "P1 Host"})
    sid = res.json()["session_id"]
    jtok = res.json()["join_token"]

    # Join 11 additional members (total = 12 active participants)
    for i in range(2, 13):
        jr = client.post(
            f"/table-sessions/{sid}/join",
            json={"display_name": f"Guest {i}", "join_token": jtok},
        )
        assert jr.status_code == 200, f"Guest {i} failed: {jr.text}"

    # 13th joiner MUST receive 409 Conflict with clear cap error
    r13 = client.post(
        f"/table-sessions/{sid}/join",
        json={"display_name": "Unlucky 13", "join_token": jtok},
    )
    assert r13.status_code == 409
    body = r13.json()
    assert body["code"] == "MAX_PARTICIPANTS_REACHED"
    assert "12" in body["detail"]
    assert len(body["latest_state"]["participants"]) == 12


# ===========================================================================
# 3. Optimistic Concurrency (Concurrent Edits -> 409 with latest_state)
# ===========================================================================
def test_concurrent_edits_return_409_with_latest_state(stage4_env: Any) -> None:
    client, _db_file, service, _, _ = stage4_env

    # Create session and join a second active member
    c = service.create_session(display_name="Host")
    sid = c["session_id"]
    jtok = c["join_token"]

    j = service.join_session(sid, join_token=jtok, display_name="Bob")
    bob_tok = j["guest_token"]
    host_tok = c["host_token"]
    assert j["session"]["version"] == 2

    # Two concurrent HTTP mutations racing on identical last-seen version=2
    def host_lock() -> httpx.Response:
        return client.post(
            f"/table-sessions/{sid}/lock",
            json={"version": 2},
            headers={"X-Guest-Token": host_tok},
        )

    def bob_prefs() -> httpx.Response:
        return client.put(
            f"/table-sessions/{sid}/preferences",
            json={
                "version": 2,
                "preferences": [{"kind": "spice_max", "strength": "SOFT", "value": {"max": 2}}],
                "is_ready": True,
            },
            headers={"X-Guest-Token": bob_tok},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut1 = pool.submit(host_lock)
        fut2 = pool.submit(bob_prefs)
        r1 = fut1.result()
        r2 = fut2.result()

    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 409], f"Expected one 200 and one 409, got {codes}"

    conflict_resp = r1 if r1.status_code == 409 else r2
    winner_resp = r2 if r1.status_code == 409 else r1
    conflict_body = conflict_resp.json()
    assert conflict_body["code"] in ("VERSION_CONFLICT", "SESSION_LOCKED")
    assert conflict_body["latest_state"]["version"] == 3
    assert conflict_body["latest_state"]["version"] == winner_resp.json()["version"]


# ===========================================================================
# 4. Token Rotation Invalidates Old Link & Host Approval / Remove
# ===========================================================================
def test_token_rotation_invalidates_old_link_and_approval_flow(stage4_env: Any) -> None:
    client, _, _, _, _ = stage4_env

    c = client.post(
        "/table-sessions",
        json={"display_name": "Host", "requires_host_approval": True},
    ).json()
    sid = c["session_id"]
    old_token = c["join_token"]
    host_tok = c["host_token"]

    # Rotate link at version=1 -> version=2
    rot = client.post(
        f"/table-sessions/{sid}/rotate-link",
        json={"version": 1},
        headers={"X-Guest-Token": host_tok},
    )
    assert rot.status_code == 200
    new_token = rot.json()["join_token"]
    assert new_token != old_token
    assert rot.json()["session"]["version"] == 2

    # Old join token rejected immediately
    bad_join = client.post(
        f"/table-sessions/{sid}/join",
        json={"display_name": "Intruder", "join_token": old_token},
    )
    assert bad_join.status_code == 403

    # New join token succeeds as 'pending'
    ok_join = client.post(
        f"/table-sessions/{sid}/join",
        json={"display_name": "Friend Sara", "join_token": new_token},
    )
    assert ok_join.status_code == 200
    sara_pid = ok_join.json()["participant_id"]
    sara_tok = ok_join.json()["guest_token"]
    assert ok_join.json()["status"] == "pending"

    # Non-host cannot approve
    non_host_approve = client.post(
        f"/table-sessions/{sid}/participants/{sara_pid}/approve",
        json={"version": 3},
        headers={"X-Guest-Token": sara_tok},
    )
    assert non_host_approve.status_code == 403

    # Host approves Sara (version 3 -> 4)
    app_res = client.post(
        f"/table-sessions/{sid}/participants/{sara_pid}/approve",
        json={"version": 3},
        headers={"X-Guest-Token": host_tok},
    )
    assert app_res.status_code == 200
    sara_state = next(p for p in app_res.json()["participants"] if p["id"] == sara_pid)
    assert sara_state["status"] == "active"

    # Remove revokes Sara's guest token immediately
    rem_res = client.post(
        f"/table-sessions/{sid}/participants/{sara_pid}/remove",
        json={"version": 4},
        headers={"X-Guest-Token": host_tok},
    )
    assert rem_res.status_code == 200
    revoked_call = client.put(
        f"/table-sessions/{sid}/preferences",
        json={"version": 5, "preferences": [], "is_ready": True},
        headers={"X-Guest-Token": sara_tok},
    )
    assert revoked_call.status_code == 401


# ===========================================================================
# 5. Host Transfer & Auto-Promotion / Cancellation on Leave
# ===========================================================================
def test_explicit_host_transfer_and_leave_auto_promotion(stage4_env: Any) -> None:
    client, _, _, _, _ = stage4_env

    c = client.post("/table-sessions", json={"display_name": "Host1"}).json()
    sid = c["session_id"]
    jtok = c["join_token"]
    h1_tok = c["host_token"]

    j2 = client.post(f"/table-sessions/{sid}/join", json={"display_name": "Early Member", "join_token": jtok}).json()
    p2_id, p2_tok = j2["participant_id"], j2["guest_token"]

    j3 = client.post(f"/table-sessions/{sid}/join", json={"display_name": "Late Member", "join_token": jtok}).json()
    p3_id, p3_tok = j3["participant_id"], j3["guest_token"]

    # 1. Explicit transfer Host1 -> Late Member (version 3 -> 4)
    tr = client.post(
        f"/table-sessions/{sid}/participants/{p3_id}/transfer-host",
        json={"version": 3},
        headers={"X-Guest-Token": h1_tok},
    )
    assert tr.status_code == 200
    assert tr.json()["host_participant_id"] == p3_id

    # Old host can no longer lock
    old_lock = client.post(f"/table-sessions/{sid}/lock", json={"version": 4}, headers={"X-Guest-Token": h1_tok})
    assert old_lock.status_code == 403

    # 2. Current host (Late Member) leaves -> earliest-joined active member (Host1 joined before Early Member, wait let's check!)
    # Host1 joined at t0 (first), Early Member joined second; so Host1 becomes host again!
    leave_p3 = client.post(f"/table-sessions/{sid}/leave", json={"version": 4}, headers={"X-Guest-Token": p3_tok})
    assert leave_p3.status_code == 200
    assert leave_p3.json()["host_participant_id"] == c["host_participant_id"]
    assert leave_p3.json()["status"] == "collecting"

    # 3. Host1 leaves -> Early Member (p2) promoted!
    leave_h1 = client.post(f"/table-sessions/{sid}/leave", json={"version": 5}, headers={"X-Guest-Token": h1_tok})
    assert leave_h1.status_code == 200
    assert leave_h1.json()["host_participant_id"] == p2_id

    # 4. Last active member (p2) leaves -> session automatically cancels!
    leave_last = client.post(f"/table-sessions/{sid}/leave", json={"version": 6}, headers={"X-Guest-Token": p2_tok})
    assert leave_last.status_code == 200
    assert leave_last.json()["status"] == "cancelled"
    assert leave_last.json()["host_participant_id"] is None

    # Verify events recorded host transfer & cancellation
    evts = client.get(f"/table-sessions/{sid}/events?last_seq=0&follow=false")
    frames = _parse_sse_frames(evts.text)
    event_types = [f["event"] for f in frames]
    assert "session.host_transferred" in event_types
    assert "session.cancelled" in event_types


# ===========================================================================
# 6. Privacy Isolation Scan Across GET and SSE
# ===========================================================================
def test_private_data_never_leaks_through_get_or_sse(stage4_env: Any) -> None:
    client, _, _, _, _ = stage4_env

    secret_note = "PRIVATE_ALLERGY_MEDICAL_SECRET_XYZ_987"
    secret_budget = 43210

    host = client.post("/table-sessions", json={"display_name": "Alice (Host)"}).json()
    sid = host["session_id"]
    jtok = host["join_token"]
    alice_tok = host["host_token"]

    bob = client.post(f"/table-sessions/{sid}/join", json={"display_name": "Bob", "join_token": jtok}).json()
    bob_tok = bob["guest_token"]

    # Alice saves a PRIVATE preference
    up = client.put(
        f"/table-sessions/{sid}/preferences",
        json={
            "version": 2,
            "is_ready": True,
            "preferences": [
                {
                    "kind": "budget_max_paise",
                    "strength": "HARD",
                    "value": {"max_paise": secret_budget},
                    "raw_text": secret_note,
                    "is_private": True,
                },
                {
                    "kind": "cuisine_like",
                    "strength": "SOFT",
                    "value": {"cuisine": "SOUTH_INDIAN_PUBLIC"},
                    "is_private": False,
                },
            ],
        },
        headers={"X-Guest-Token": alice_tok},
    )
    assert up.status_code == 200

    # Alice CAN see her own private preference
    alice_get = client.get(f"/table-sessions/{sid}", headers={"X-Guest-Token": alice_tok})
    assert secret_note in alice_get.text
    assert str(secret_budget) in alice_get.text

    # Scan GET response as Bob -> ZERO secret leakage
    bob_get = client.get(f"/table-sessions/{sid}", headers={"X-Guest-Token": bob_tok})
    assert bob_get.status_code == 200
    assert secret_note not in bob_get.text
    assert str(secret_budget) not in bob_get.text
    assert "SOUTH_INDIAN_PUBLIC" in bob_get.text  # Public preference visible

    # Scan truly anonymous GET response -> ZERO secret leakage
    anon_client = TestClient(app)
    anon_get = anon_client.get(f"/table-sessions/{sid}")
    assert secret_note not in anon_get.text
    assert str(secret_budget) not in anon_get.text

    # Scan SSE event stream (replay AND snapshot) as Bob and anonymous -> ZERO secret leakage
    bob_sse = client.get(f"/table-sessions/{sid}/events?last_seq=0&follow=false", headers={"X-Guest-Token": bob_tok})
    assert bob_sse.status_code == 200
    assert secret_note not in bob_sse.text
    assert str(secret_budget) not in bob_sse.text

    snap_sse = anon_client.get(
        f"/table-sessions/{sid}/events?follow=false",
        headers={"Last-Event-ID": "999999"},
    )
    assert snap_sse.status_code == 200
    snap_frames = _parse_sse_frames(snap_sse.text)
    assert len(snap_frames) == 1
    assert snap_frames[0]["event"] == "session.snapshot"
    assert int(snap_frames[0]["id"]) == 3
    assert "SOUTH_INDIAN_PUBLIC" in snap_sse.text
    assert secret_note not in snap_sse.text
    assert str(secret_budget) not in snap_sse.text

    # Unknown non-numeric Last-Event-ID triggers sanitized session.snapshot for Bob without Alice's secrets
    bob_snap = client.get(
        f"/table-sessions/{sid}/events?last_event_id=unknown_old_id&follow=false",
        headers={"X-Guest-Token": bob_tok},
    )
    assert bob_snap.status_code == 200
    bob_snap_frames = _parse_sse_frames(bob_snap.text)
    assert len(bob_snap_frames) == 1
    assert bob_snap_frames[0]["event"] == "session.snapshot"
    assert secret_note not in bob_snap.text
    assert str(secret_budget) not in bob_snap.text

    # 409 latest_state returned to Bob must ALSO strip Alice's private preferences
    bob_409 = client.put(
        f"/table-sessions/{sid}/preferences",
        json={"version": 1, "preferences": [], "is_ready": True},
        headers={"X-Guest-Token": bob_tok},
    )
    assert bob_409.status_code == 409
    assert secret_note not in bob_409.text
    assert str(secret_budget) not in bob_409.text


# ===========================================================================
# 7. Reconnect After Missing 10 Events & Live Async SSE Streaming
# ===========================================================================
def test_sse_reconnect_after_missing_10_events_and_live_stream(stage4_env: Any) -> None:
    client, _, service, _, hub = stage4_env

    c = client.post("/table-sessions", json={"display_name": "Host"}).json()
    sid = c["session_id"]
    jtok = c["join_token"]
    htok = c["host_token"]

    j = client.post(f"/table-sessions/{sid}/join", json={"display_name": "Peer", "join_token": jtok}).json()
    ptok = j["guest_token"]

    # Baseline has 2 events: seq=1 (created), seq=2 (joined). Version is now 2.
    # Emit 10 more events while client is disconnected!
    ver = 2
    for idx in range(1, 11):
        res = client.post(
            f"/table-sessions/{sid}/messages",
            json={"version": ver, "message": f"Missed chat message #{idx}"},
            headers={"X-Guest-Token": ptok},
        )
        assert res.status_code == 200
        ver = res.json()["version"]

    # Client reconnects passing Last-Event-ID: 2 (last event it saw before disconnect)
    reconn = client.get(
        f"/table-sessions/{sid}/events?follow=false",
        headers={"Last-Event-ID": "2", "X-Guest-Token": htok},
    )
    assert reconn.status_code == 200
    frames = _parse_sse_frames(reconn.text)
    assert len(frames) == 10
    assert [int(f["id"]) for f in frames] == list(range(3, 13))
    assert [f["data"]["payload"]["message"] for f in frames] == [
        f"Missed chat message #{idx}" for idx in range(1, 11)
    ]

    # Reconnect passing `last_version=7` receives exact events for versions 8..12
    ver_reconn = client.get(f"/table-sessions/{sid}/events?last_version=7&follow=false")
    v_frames = _parse_sse_frames(ver_reconn.text)
    assert [f["data"]["session_version"] for f in v_frames] == [8, 9, 10, 11, 12]

    # Live async subscription receives newly published event pushed via SSEHub
    async def verify_live_push() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            received: list[str] = []

            async def _reader() -> None:
                async with ac.stream(
                    "GET",
                    f"/table-sessions/{sid}/events?last_seq=12&max_events=1",
                ) as resp:
                    async for line in resp.aiter_lines():
                        if line.startswith("data:"):
                            received.append(line)
                            break

            task = asyncio.create_task(_reader())
            # Wait briefly until subscriber attaches to SSEHub
            for _ in range(40):
                if hub.subscriber_count(sid) >= 1:
                    break
                await asyncio.sleep(0.01)

            service.post_message(sid, participant_id=j["participant_id"], expected_version=ver, message="LIVE_SSE_EVENT")
            await asyncio.wait_for(task, timeout=3.0)
            assert len(received) == 1
            assert "LIVE_SSE_EVENT" in received[0]

    asyncio.run(verify_live_push())


# ===========================================================================
# 8. Lock/Unlock, Extend Expiry (Max 24h) & Reaper Job
# ===========================================================================
def test_lock_unlock_extend_cap_and_reaper_expiry_purge(stage4_env: Any) -> None:
    client, db_file, service, _, _ = stage4_env

    t0 = 1700000000
    created = service.create_session(display_name="Host", now=t0)
    sid = created["session_id"]
    host_pid = created["host_participant_id"]
    assert created["session"]["expires_at"] == t0 + 7200  # 2 hours default

    # Host tries extending by 48 hours -> strictly clamped at created_at + 24 hours (86400s)
    extended = service.extend_expiry(
        sid, host_participant_id=host_pid, extend_seconds=48 * 3600, expected_version=1, now=t0 + 10
    )
    assert extended["expires_at"] == t0 + 86400

    # Submit sensitive preference at t0 + 20
    service.update_preferences(
        sid,
        participant_id=host_pid,
        expected_version=2,
        preferences=[{"kind": "allergen", "strength": "HARD", "value": {"allergen": "peanuts"}, "raw_text": "SENSITIVE_PEANUT_DATA", "is_private": True}],
        now=t0 + 20,
    )

    # Lock blocks preference modifications (409)
    locked = service.lock_session(sid, host_participant_id=host_pid, expected_version=3, now=t0 + 30)
    assert locked["status"] == "locked"
    with pytest.raises(Exception) as exc_info:
        service.update_preferences(sid, participant_id=host_pid, expected_version=4, preferences=[], now=t0 + 40)
    assert "SESSION_LOCKED" in getattr(exc_info.value, "code", "")

    # Unlock restores collecting state
    unlocked = service.unlock_session(sid, host_participant_id=host_pid, expected_version=4, now=t0 + 50)
    assert unlocked["status"] == "collecting"

    # Reaper BEFORE expiry does nothing
    r_before = run_expiry_and_retention_job(db_path=db_file, now=t0 + 1000)
    assert r_before["expired_count"] == 0
    assert r_before["purged_session_count"] == 0

    # Reaper AFTER expiry (t0 + 86400 + 10) moves session -> expired, keeps prefs within retention window
    r_expired = run_expiry_and_retention_job(db_path=db_file, now=t0 + 86410, retention_seconds=86400)
    assert r_expired["expired_count"] == 1
    assert sid in r_expired["expired_session_ids"]
    assert r_expired["purged_session_count"] == 0

    state_exp = service.get_session_state(sid, viewer_participant_id=host_pid)
    assert state_exp["status"] == "expired"
    assert len(state_exp["participants"][0]["preferences"]) == 1  # Not yet purged until retention elapses

    # Join token fails with 410 Gone after expiration
    join_exp = client.post(
        f"/table-sessions/{sid}/join",
        json={"display_name": "Late", "join_token": created["join_token"]},
    )
    assert join_exp.status_code == 410

    # Reaper PAST retention window (expires_at + 24h) deletes sensitive preferences & token hashes
    r_purged = run_expiry_and_retention_job(db_path=db_file, now=t0 + 86400 + 86401, retention_seconds=86400)
    assert r_purged["purged_session_count"] == 1
    assert r_purged["deleted_preferences_count"] == 1

    # Verify DB preferences deleted & guest_token_hash nulled
    conn = get_sqlite_connection(db_file)
    pref_cnt = conn.execute("SELECT COUNT(*) FROM preferences;").fetchone()[0]
    tok_cnt = conn.execute("SELECT COUNT(*) FROM participants WHERE guest_token_hash IS NOT NULL;").fetchone()[0]
    conn.close()
    assert pref_cnt == 0
    assert tok_cnt == 0


# ===========================================================================
# 9. Rate Limiting (429 Too Many Requests)
# ===========================================================================
def test_rate_limits_join_ip_token_prefs_and_messages(stage4_env: Any) -> None:
    client, _, _, limiter, _ = stage4_env

    # Configure tight deterministic rate limits
    limiter.set_rule("join_ip", max_requests=2, window_seconds=60)
    limiter.set_rule("join_token", max_requests=4, window_seconds=60)
    limiter.set_rule("preference_participant", max_requests=2, window_seconds=60)
    limiter.set_rule("message_participant", max_requests=2, window_seconds=60)

    c = client.post("/table-sessions", json={"display_name": "Host"}).json()
    sid = c["session_id"]
    jtok = c["join_token"]
    htok = c["host_token"]

    # 1. join_ip allows 2, 3rd from same IP returns 429
    assert client.post(f"/table-sessions/{sid}/join", json={"display_name": "G1", "join_token": jtok}).status_code == 200
    assert client.post(f"/table-sessions/{sid}/join", json={"display_name": "G2", "join_token": jtok}).status_code == 200
    r_ip_429 = client.post(f"/table-sessions/{sid}/join", json={"display_name": "G3", "join_token": jtok})
    assert r_ip_429.status_code == 429
    assert "Retry-After" in r_ip_429.headers

    # 2. join_token limit tested with distinct X-Forwarded-For IPs
    assert client.post(
        f"/table-sessions/{sid}/join",
        json={"display_name": "G4", "join_token": jtok},
        headers={"X-Forwarded-For": "10.0.0.10"},
    ).status_code == 200
    assert client.post(
        f"/table-sessions/{sid}/join",
        json={"display_name": "G5", "join_token": jtok},
        headers={"X-Forwarded-For": "10.0.0.11"},
    ).status_code == 200
    r_tok_429 = client.post(
        f"/table-sessions/{sid}/join",
        json={"display_name": "G6", "join_token": jtok},
        headers={"X-Forwarded-For": "10.0.0.12"},
    )
    assert r_tok_429.status_code == 429

    # 3. preference updates per participant limit
    cur_ver = client.get(f"/table-sessions/{sid}").json()["version"]
    assert client.put(
        f"/table-sessions/{sid}/preferences",
        json={"version": cur_ver, "preferences": [], "is_ready": False},
        headers={"X-Guest-Token": htok},
    ).status_code == 200
    assert client.put(
        f"/table-sessions/{sid}/preferences",
        json={"version": cur_ver + 1, "preferences": [], "is_ready": True},
        headers={"X-Guest-Token": htok},
    ).status_code == 200
    r_pref_429 = client.put(
        f"/table-sessions/{sid}/preferences",
        json={"version": cur_ver + 2, "preferences": [], "is_ready": False},
        headers={"X-Guest-Token": htok},
    )
    assert r_pref_429.status_code == 429

    # 4. message updates per participant limit
    m_ver = cur_ver + 2
    assert client.post(
        f"/table-sessions/{sid}/messages",
        json={"version": m_ver, "message": "Hi 1"},
        headers={"X-Guest-Token": htok},
    ).status_code == 200
    assert client.post(
        f"/table-sessions/{sid}/messages",
        json={"version": m_ver + 1, "message": "Hi 2"},
        headers={"X-Guest-Token": htok},
    ).status_code == 200
    r_msg_429 = client.post(
        f"/table-sessions/{sid}/messages",
        json={"version": m_ver + 2, "message": "Hi 3"},
        headers={"X-Guest-Token": htok},
    )
    assert r_msg_429.status_code == 429


# ===========================================================================
# 10. Edge Cases: Query Join Token, Join 409, Pending Guard, Self-Transfer, Terminal & UUID Reconnect
# ===========================================================================
def test_join_version_conflict_pending_guard_and_terminal_states(stage4_env: Any) -> None:
    client, _, _, _, _ = stage4_env

    c = client.post(
        "/table-sessions",
        json={"display_name": "Host", "requires_host_approval": True, "deadline_at": 1800000000},
    )
    assert c.status_code == 201
    c_json = c.json()
    sid = c_json["session_id"]
    jtok = c_json["join_token"]
    htok = c_json["host_token"]
    h_pid = c_json["host_participant_id"]

    # Self-transfer rejected cleanly (422)
    self_tr = client.post(
        f"/table-sessions/{sid}/participants/{h_pid}/transfer-host",
        json={"version": 1},
        headers={"X-Guest-Token": htok},
    )
    assert self_tr.status_code == 422

    # Join with mismatched version returns 409 with latest_state
    bad_ver_join = client.post(
        f"/table-sessions/{sid}/join?token={jtok}",
        json={"display_name": "Guest", "version": 99},
    )
    assert bad_ver_join.status_code == 409
    assert bad_ver_join.json()["code"] == "VERSION_CONFLICT"
    assert bad_ver_join.json()["latest_state"]["version"] == 1

    # Join via query ?token=... fallback succeeds as pending
    ok_join = client.post(
        f"/table-sessions/{sid}/join?token={jtok}",
        json={"display_name": "Pending Guest", "version": 1},
    )
    assert ok_join.status_code == 200
    p_tok = ok_join.json()["guest_token"]
    p_pid = ok_join.json()["participant_id"]
    assert ok_join.json()["status"] == "pending"

    # Pending participant cannot update preferences or post messages until approved (403)
    pend_pref = client.put(
        f"/table-sessions/{sid}/preferences",
        json={"version": 2, "preferences": [], "is_ready": True},
        headers={"X-Guest-Token": p_tok},
    )
    assert pend_pref.status_code == 403
    pend_msg = client.post(
        f"/table-sessions/{sid}/messages",
        json={"version": 2, "message": "Hello?"},
        headers={"X-Guest-Token": p_tok},
    )
    assert pend_msg.status_code == 403

    # HTTP extend clamps expiry to created_at + 86400
    ext = client.post(
        f"/table-sessions/{sid}/extend",
        json={"version": 2, "extend_seconds": 48 * 3600},
        headers={"X-Guest-Token": htok},
    )
    assert ext.status_code == 200
    assert ext.json()["expires_at"] == ext.json()["created_at"] + 86400

    # Approve pending guest -> active (version 3 -> 4), then test UUID Last-Event-ID replay
    app_r = client.post(
        f"/table-sessions/{sid}/participants/{p_pid}/approve",
        json={"version": 3},
        headers={"X-Guest-Token": htok},
    )
    assert app_r.status_code == 200

    all_evts = _parse_sse_frames(client.get(f"/table-sessions/{sid}/events?last_seq=0&follow=false").text)
    second_uuid = all_evts[1]["data"]["event_id"]
    uuid_reconn = _parse_sse_frames(
        client.get(
            f"/table-sessions/{sid}/events?follow=false",
            headers={"Last-Event-ID": second_uuid},
        ).text
    )
    assert [int(f["id"]) for f in uuid_reconn] == [3, 4]

    # Leave active guest, add another pending guest, then host leaves -> session cancels
    assert client.post(f"/table-sessions/{sid}/leave", json={"version": 4}, headers={"X-Guest-Token": p_tok}).status_code == 200
    pend2 = client.post(f"/table-sessions/{sid}/join?token={jtok}", json={"display_name": "Pending2", "version": 5})
    assert pend2.status_code == 200
    pend2_tok = pend2.json()["guest_token"]

    assert client.post(f"/table-sessions/{sid}/leave", json={"version": 6}, headers={"X-Guest-Token": htok}).status_code == 200

    # Cancelled session rejects mutations cleanly (409)
    term_leave = client.post(f"/table-sessions/{sid}/leave", json={"version": 7}, headers={"X-Guest-Token": pend2_tok})
    assert term_leave.status_code == 409
    assert term_leave.json()["code"] == "SESSION_TERMINAL"

    term_join = client.post(f"/table-sessions/{sid}/join?token={jtok}", json={"display_name": "TooLate"})
    assert term_join.status_code == 409
    assert term_join.json()["code"] == "SESSION_NOT_JOINABLE"

    final_state = client.get(f"/table-sessions/{sid}").json()
    assert final_state["status"] == "cancelled"
    assert final_state["deadline_at"] == 1800000000


# ===========================================================================
# 11. 4-Participant Simulation Script Verification
# ===========================================================================
def test_4_participant_simulation_script(tmp_path: Path) -> None:
    from scripts.simulate_table_session import run_simulation

    summary = asyncio.run(run_simulation(db_path=tmp_path / "sim.db"))
    assert summary["subscriber_streams_verified"] == 4
    assert len(summary["event_sequence"]) == 8
    assert summary["event_sequence"][0][1] == "session.created"
    assert summary["event_sequence"][-1][1] == "session.locked"

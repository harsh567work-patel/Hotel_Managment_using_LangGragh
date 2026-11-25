"""
scripts/simulate_table_session.py — Simulates 4 participants joining a Table Split session
concurrently and asserts all 4 SSE subscribers receive the exact identical causal event sequence.

Run standalone:
    python -m scripts.simulate_table_session
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import httpx

from api import app
from table_split.api_sessions import get_rate_limiter, get_session_service, get_sse_hub
from table_split.rate_limit import RateLimitRule, SlidingWindowRateLimiter
from table_split.services.session_service import SessionService
from table_split.sse import SSEHub


async def run_simulation(db_path: Path | None = None) -> dict[str, Any]:
    """
    Runs end-to-end 4-participant realtime SSE simulation:
    1. Host creates session (seq=1).
    2. 4 participants (Bob, Carol, Dave, Eve) join (seq=2..5) and open concurrent SSE streams.
    3. Participants submit public/private preferences and chat message, then Host locks room.
    4. Confirms every participant's SSE stream converged to the exact same ordered `(seq, type, session_version)` sequence.
    """
    cleanup_dir: tempfile.TemporaryDirectory[str] | None = None
    if db_path is None:
        cleanup_dir = tempfile.TemporaryDirectory()
        db_path = Path(cleanup_dir.name) / "sim_table_split.db"

    hub = SSEHub()
    limiter = SlidingWindowRateLimiter({
        "join_ip": RateLimitRule(max_requests=100, window_seconds=60),
        "join_token": RateLimitRule(max_requests=100, window_seconds=60),
        "preference_participant": RateLimitRule(max_requests=100, window_seconds=60),
        "message_participant": RateLimitRule(max_requests=100, window_seconds=60),
    })
    service = SessionService(db_path=db_path, sse_hub=hub)

    app.dependency_overrides[get_sse_hub] = lambda: hub
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    app.dependency_overrides[get_session_service] = lambda: service

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://simserver") as client:
            # 1. Host creates session
            create_resp = await client.post("/table-sessions", json={"display_name": "Host Aresh"})
            create_resp.raise_for_status()
            host_data = create_resp.json()
            sid = host_data["session_id"]
            join_token = host_data["join_token"]
            host_tok = host_data["host_token"]

            # 2. 4 participants join using share token
            names = ["Bob", "Carol", "Dave", "Eve"]
            guest_tokens: dict[str, str] = {}
            for name in names:
                jr = await client.post(
                    f"/table-sessions/{sid}/join",
                    json={"display_name": name, "join_token": join_token},
                )
                jr.raise_for_status()
                guest_tokens[name] = jr.json()["guest_token"]

            # Expect 8 total events per stream (seq 1..8):
            # 1: session.created
            # 2..5: participant.joined x4
            # 6: preference.updated (Bob, with private secret redacted!)
            # 7: session.message (Carol)
            # 8: session.locked (Host)
            expected_total_events = 8
            streams: dict[str, list[tuple[int, str, int]]] = {name: [] for name in names}
            raw_frames_by_user: dict[str, list[str]] = {name: [] for name in names}

            async def _listen(name: str, token: str) -> None:
                async with client.stream(
                    "GET",
                    f"/table-sessions/{sid}/events?last_seq=0&max_events={expected_total_events}",
                    headers={"X-Guest-Token": token},
                ) as resp:
                    resp.raise_for_status()
                    cur_seq = 0
                    cur_event = ""
                    async for line in resp.aiter_lines():
                        if line.startswith("id:"):
                            cur_seq = int(line[3:].strip())
                        elif line.startswith("event:"):
                            cur_event = line[6:].strip()
                        elif line.startswith("data:"):
                            raw_payload = line[5:].strip()
                            raw_frames_by_user[name].append(raw_payload)
                            parsed = json.loads(raw_payload)
                            streams[name].append((cur_seq, cur_event, int(parsed["session_version"])))
                            if len(streams[name]) >= expected_total_events:
                                break

            listeners = [asyncio.create_task(_listen(name, guest_tokens[name])) for name in names]

            # Wait briefly until all 4 live SSE subscribers are attached
            for _ in range(50):
                if hub.subscriber_count(sid) == 4:
                    break
                await asyncio.sleep(0.01)

            # 3. Emit 3 live synchronized mutations (seq 6, 7, 8)
            secret_marker = "BOB_PRIVATE_SECRET_DO_NOT_BROADCAST"
            pref_res = await client.put(
                f"/table-sessions/{sid}/preferences",
                json={
                    "version": 5,
                    "is_ready": True,
                    "preferences": [
                        {
                            "kind": "allergen",
                            "strength": "HARD",
                            "value": {"allergen": "peanuts"},
                            "raw_text": secret_marker,
                            "is_private": True,
                        }
                    ],
                },
                headers={"X-Guest-Token": guest_tokens["Bob"]},
            )
            pref_res.raise_for_status()

            msg_res = await client.post(
                f"/table-sessions/{sid}/messages",
                json={"version": 6, "message": "Let's lock in dinner!"},
                headers={"X-Guest-Token": guest_tokens["Carol"]},
            )
            msg_res.raise_for_status()

            lock_res = await client.post(
                f"/table-sessions/{sid}/lock",
                json={"version": 7},
                headers={"X-Guest-Token": host_tok},
            )
            lock_res.raise_for_status()

            await asyncio.wait_for(asyncio.gather(*listeners), timeout=5.0)

            # Assert all 4 participants received the EXACT identical causal sequence and zero secret leak
            canonical_seq = streams[names[0]]
            assert len(canonical_seq) == expected_total_events
            for name in names[1:]:
                assert streams[name] == canonical_seq, f"Event stream mismatch for {name}: {streams[name]} vs {canonical_seq}"

            for name in names:
                joined_raw = "\n".join(raw_frames_by_user[name])
                assert secret_marker not in joined_raw, f"Private data leaked into {name}'s SSE stream!"

            return {
                "session_id": sid,
                "participant_count": 5,
                "subscriber_streams_verified": len(names),
                "event_sequence": canonical_seq,
            }
    finally:
        app.dependency_overrides.clear()
        if cleanup_dir is not None:
            cleanup_dir.cleanup()


def main() -> int:  # pragma: no cover
    summary = asyncio.run(run_simulation())
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

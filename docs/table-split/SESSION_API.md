# Table Split Session Lifecycle & Realtime Sync API

This document specifies all endpoints, authentication scopes, concurrency semantics, rate limits, retention policies, and Server-Sent Events (SSE) payloads for **Table Split Mode** (Stage 4).

---

## 1. Authentication, Scoped Cookies & Tokens

### 1.1 Join Token (Share Link)
- **Entropy & Storage**: Generated via `secrets.token_urlsafe(16)` (**128-bit** cryptographic entropy). Stored only as a SHA-256 hash in `table_sessions.join_token_hash` and compared with `hmac.compare_digest`.
- **Expiration & Rotation**: Expires automatically with the session (`expires_at`). Calling `POST /table-sessions/{id}/rotate-link` immediately replaces `join_token_hash`, invalidating the previous share link (`HTTP 403`).

### 1.2 Guest Cookies
- Name-only guest diners receive a 256-bit secret token returned in an **`HttpOnly`**, `SameSite=Lax` cookie scoped strictly to that single session:
  - Cookie Name: `ts_guest_{session_id}`
  - Cookie Path: `/table-sessions/{session_id}`
- API scripts/tests may also supply `X-Guest-Token: <token>` or `Authorization: Bearer <token>`. Server-side auth strictly verifies `participants.session_id == session_id` so tokens from Session A cannot access Session B (`HTTP 401`).
- Leaving (`/leave`) or being removed (`/remove`) immediately clears `guest_token_hash` and revokes access.

---

## 2. Optimistic Concurrency Control (`HTTP 409`)

Every mutating endpoint accepts/requires the client's last-seen `version: int`:
- Writes execute atomically in SQLite WAL transactions with `BEGIN IMMEDIATE` and `UPDATE table_sessions SET version = version + 1 WHERE id = ? AND version = ?`.
- On mismatch, the server returns **HTTP 409 Conflict** containing:
  ```json
  {
    "detail": "Version conflict: expected 2, current version is 3.",
    "code": "VERSION_CONFLICT",
    "latest_state": { "...": "full sanitized session snapshot" }
  }
  ```
- Clients can inspect `latest_state`, merge or prompt the user, and retry cleanly.

---

## 3. REST Endpoints Reference

| Method & Path | Auth | Request Body | Description & Status Codes |
|---|---|---|---|
| `POST /table-sessions` | Public / Optional User | `{display_name, requires_host_approval?, max_participants?, expires_in_seconds?, ...}` | Creates session (`201`). Returns `{session_id, share_link, join_token, host_participant_id, host_token?, session}`. Sets `HttpOnly` cookie if guest host. Default expiry: `7200s` (2h), max `86400s` (24h). Participant cap default/max: `12`. |
| `GET /table-sessions/{id}` | Public / Optional Auth | - | Returns sanitized state: `id`, `status`, `version`, `deadline_at`, `expires_at`, `participants` (`display_name`, `role`, `status`, `is_ready`). Private preferences (`is_private=true`) are stripped for everyone except the owner. |
| `POST /table-sessions/{id}/join` | Public + Join Token | `{display_name, join_token?, version?}` (`?token=` query fallback) | Verifies join token constant-time. Rate-limited per IP and per token (`429`). `410` if expired, `409` (`MAX_PARTICIPANTS_REACHED`) for 13th participant, `403` for invalid/rotated token. Sets scoped `HttpOnly` cookie. |
| `POST /table-sessions/{id}/participants/{pid}/approve` | Host Only | `{version}` | Moves `pending` participant -> `active` (`200`, `403`, `409`). |
| `POST /table-sessions/{id}/participants/{pid}/remove` | Host Only | `{version}` | Moves participant -> `removed` and revokes guest token (`200`, `403`, `409`). |
| `POST /table-sessions/{id}/participants/{pid}/transfer-host` | Host Only | `{version}` | Transfers `host` role to another active participant (`200`, `403`, `409`, `422` on self-transfer). |
| `POST /table-sessions/{id}/leave` | Participant | `{version}` | Marks caller `left`. If host leaves: promotes earliest-joined `active` member (`joined_at ASC, rowid ASC`), or sets `status = "cancelled"` if no active members remain. |
| `POST /table-sessions/{id}/lock` | Host Only | `{version}` | Locks session (`locked`), blocking new joins and preference edits (`200`, `403`, `409`). |
| `POST /table-sessions/{id}/unlock` | Host Only | `{version}` | Unlocks session back to `collecting` (`200`, `403`, `409`). |
| `POST /table-sessions/{id}/rotate-link` | Host Only | `{version}` | Replaces `join_token_hash`, invalidating old share links (`200`, returns new `join_token` + `share_link`). |
| `POST /table-sessions/{id}/extend` | Host Only | `{version, extend_seconds}` | Extends `expires_at` up to strict cap `created_at + 86400s` (24h max). |
| `PUT /table-sessions/{id}/preferences` | Active Participant | `{version, preferences, is_ready?}` | Replaces caller's preferences cleanly (diet/allergen forced `HARD`). Rate-limited (`429`). `409` if locked. |
| `POST /table-sessions/{id}/messages` | Active Participant | `{version, message}` | Emits a chat message event to all subscribers. Rate-limited per participant (`429`). |
| `GET /table-sessions/{id}/events` | Public / Optional Auth | Headers: `Last-Event-ID`; Query: `last_event_id`, `last_seq`, `last_version`, `follow`, `max_events`, `heartbeat_seconds` | **Server-Sent Events (`text/event-stream`)** streaming missed + live events. Emits `session.snapshot` if reconnect cursor is missing/stale or out-of-range. |

---

## 4. Server-Sent Events (SSE) Protocol

### Frame Wire Format
Every SSE message carries a monotonically increasing integer `seq` as the SSE `id:`, the canonical event name as `event:`, and JSON `data:`:

```text
id: 6
event: preference.updated
data: {"event_id":"...","seq":6,"session_id":"...","session_version":6,"participant_id":"...","type":"preference.updated","payload":{"participant_id":"...","preference_count":1,"is_ready":true},"created_at":1732550000}

```

### Reconnect Contract
1. Browser/client supplies `Last-Event-ID: <seq|uuid>` (or query `?last_event_id=...`, `?last_seq=<n>`, or `?last_version=<v>`).
2. Server subscribes to the live `SSEHub` before reading SQLite `session_events` in causal `seq ASC` order, replaying every missed event cleanly and deduplicating live events by `seq`.
3. If `Last-Event-ID` does not match any stored event (e.g. unknown UUID) or the supplied `seq`/`version` cursor exceeds the latest persisted cursor, the server immediately yields a **`session.snapshot`** frame with `id: <current_max_seq>` containing the sanitized session state before streaming live events.

### Event Type Catalog

| Event `type` | Trigger | Safe Payload Fields |
|---|---|---|
| `session.created` | Session created | `host_participant_id`, `host_display_name`, `status`, `expires_at` |
| `participant.pending` | Guest joined needing approval | `participant_id`, `display_name`, `status`, `role` |
| `participant.joined` | Participant admitted immediately | `participant_id`, `display_name`, `status`, `role` |
| `participant.approved` | Host approved pending member | `participant_id`, `approved_by` |
| `participant.removed` | Host removed participant | `participant_id`, `removed_by` |
| `participant.left` | Participant left session | `participant_id`, `was_host` |
| `session.host_transferred` | Explicit transfer OR host left | `old_host_id`, `new_host_id`, `reason` (`explicit_transfer` \| `host_left`) |
| `session.locked` | Host locked session | `locked_by` |
| `session.unlocked` | Host unlocked session | `unlocked_by` |
| `session.link_rotated` | Host rotated share token | `rotated_by` *(raw token never broadcast over SSE)* |
| `session.extended` | Host extended session TTL | `expires_at` |
| `preference.updated` | Preferences/readiness updated | `participant_id`, `preference_count`, `is_ready` *(private `value`/`raw_text` NEVER included)* |
| `session.message` | Participant posted message | `participant_id`, `display_name`, `message` |
| `session.expired` | Reaper expired overdue session | `reason` (`ttl_expired`), `expired_at` |
| `session.cancelled` | Last active host left session | `reason` (`host_left_no_active_members`) |
| `session.sensitive_data_purged` | Reaper deleted expired private data | `deleted_preferences`, `purged_at` |
| `session.snapshot` | SSE catch-up fallback | `session_version`, `session` (sanitized per viewer) |

---

## 5. Rate Limits & Retention Lifecycle

### Rate Limits (`HTTP 429` + `Retry-After`)
Enforced via `SlidingWindowRateLimiter`:
- `join_ip`: Default 15 requests / 60s per client IP.
- `join_token`: Default 25 requests / 60s per `(session_id, join_token_hash)`.
- `preference_participant`: Default 30 requests / 60s per `(session_id, participant_id)`.
- `message_participant`: Default 20 requests / 60s per `(session_id, participant_id)`.

### Expiry & Sensitive Data Retention Reaper
A background maintenance task (`run_expiry_and_retention_job`, scheduled periodically via FastAPI lifespan) executes two idempotent phases:
1. Moves active sessions past `expires_at` (2h default, up to 24h max) to `status = 'expired'` and emits `session.expired`.
2. Deletes `preferences` records and clears `guest_token_hash` once `expires_at + retention_seconds` (default 24h after expiry) elapses, marking `sensitive_data_purged_at` and writing `session.sensitive_data_purged`.

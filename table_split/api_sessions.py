"""
table_split.api_sessions — FastAPI endpoints for Stage 4 Table Split Session Lifecycle,
Guest Token Auth (scoped HttpOnly cookies), Optimistic Concurrency (409), Rate Limiting, and SSE Sync.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from config import (
    TABLE_SPLIT_DB_PATH,
    TABLE_SPLIT_DEFAULT_EXPIRY_SECONDS,
    TABLE_SPLIT_MAX_PARTICIPANTS,
)
from table_split.rate_limit import RateLimitExceededError, SlidingWindowRateLimiter
from table_split.security import hash_token
from table_split.services.preference_parser import (
    GroqPreferenceParser,
    PreferenceParser,
    build_menu_vocabulary_from_json,
    record_parser_log,
)
from table_split.services.session_service import (
    ForbiddenError,
    SessionConflictError,
    SessionError,
    SessionExpiredError,
    SessionNotFoundError,
    SessionService,
    SessionValidationError,
    UnauthorizedError,
)
from table_split.sse import SSEHub, format_sse_frame

router = APIRouter(tags=["Table Sessions"])

_default_sse_hub = SSEHub()
_default_rate_limiter = SlidingWindowRateLimiter()


def get_sse_hub() -> SSEHub:
    return _default_sse_hub


def get_session_service(sse_hub: Annotated[SSEHub, Depends(get_sse_hub)]) -> SessionService:
    return SessionService(db_path=TABLE_SPLIT_DB_PATH, sse_hub=sse_hub)


def get_rate_limiter() -> SlidingWindowRateLimiter:
    return _default_rate_limiter


def get_preference_parser() -> PreferenceParser:
    return GroqPreferenceParser()


SessionServiceDep = Annotated[SessionService, Depends(get_session_service)]
RateLimiterDep = Annotated[SlidingWindowRateLimiter, Depends(get_rate_limiter)]
PreferenceParserDep = Annotated[PreferenceParser, Depends(get_preference_parser)]


def _cookie_name(session_id: str) -> str:
    return f"ts_guest_{session_id}"


def _cookie_path(session_id: str) -> str:
    return f"/table-sessions/{session_id}"


def _extract_raw_token(request: Request, session_id: str) -> tuple[str | None, str | None]:
    """
    Extracts `(raw_guest_token, user_id)` strictly scoped to `session_id`.
    Accepts HttpOnly session cookie `ts_guest_{session_id}`, `X-Guest-Token`, or `Authorization: Bearer`.
    """
    cookie_token = request.cookies.get(_cookie_name(session_id))
    header_token = request.headers.get("X-Guest-Token")
    auth_header = request.headers.get("Authorization", "")
    bearer_token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else None
    raw_token = header_token or bearer_token or cookie_token
    user_id = request.headers.get("X-User-Id")
    return raw_token, user_id


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _handle_session_error(exc: SessionError | RateLimitExceededError) -> JSONResponse | HTTPException:
    if isinstance(exc, SessionConflictError):
        return JSONResponse(
            status_code=409,
            content={
                "detail": str(exc),
                "code": exc.code,
                "latest_state": exc.latest_state,
            },
        )
    if isinstance(exc, RateLimitExceededError):
        return JSONResponse(
            status_code=429,
            content={"detail": str(exc), "code": "RATE_LIMIT_EXCEEDED", "bucket": exc.bucket},
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    if isinstance(exc, SessionNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, UnauthorizedError):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, ForbiddenError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, SessionExpiredError):
        return HTTPException(status_code=410, detail=str(exc))
    if isinstance(exc, SessionValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


# ── Pydantic Request Schemas ─────────────────────────────────────────────────
class CreateSessionRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=64)
    user_id: str | None = None
    restaurant_id: str = "spiceroute_default"
    requires_host_approval: bool = False
    max_participants: int = Field(default=TABLE_SPLIT_MAX_PARTICIPANTS, ge=1, le=TABLE_SPLIT_MAX_PARTICIPANTS)
    deadline_at: int | None = None
    expires_in_seconds: int = Field(default=TABLE_SPLIT_DEFAULT_EXPIRY_SECONDS)
    split_mode: str = "by_consumption"
    payment_mode: str = "each_pays_own"
    delivery_address_id: str | None = None


class JoinSessionRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=64)
    join_token: str | None = None
    user_id: str | None = None
    version: int | None = Field(default=None, ge=1)


class VersionedMutationRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    version: int = Field(..., ge=1, description="Client's last-seen session version for optimistic locking")


class ExtendSessionRequest(BaseModel):
    version: int = Field(..., ge=1)
    extend_seconds: int = Field(..., ge=1)


class PreferenceInput(BaseModel):
    id: str | None = None
    kind: str
    strength: str = "HARD"
    weight: int = Field(default=1, ge=1, le=5)
    value: Any = Field(default_factory=dict)
    source: str = "chip"
    raw_text: str | None = None
    parser_confidence: float | None = None
    confirmed_by_user: bool = True
    is_private: bool = False


class UpdatePreferencesRequest(BaseModel):
    version: int = Field(..., ge=1)
    preferences: list[PreferenceInput] = Field(default_factory=list)
    is_ready: bool | None = None


class UpsertPreferencesRequest(BaseModel):
    version: int = Field(..., ge=1)
    preferences: list[PreferenceInput] = Field(default_factory=list)
    is_ready: bool | None = None
    replace_all: bool = False


class ParsePreferencesRequest(BaseModel):
    text: str = Field(..., max_length=2000)
    locale: str = Field(default="en-IN", max_length=32)


class PostMessageRequest(BaseModel):
    version: int = Field(..., ge=1)
    message: str = Field(..., min_length=1, max_length=1000)


def _set_scoped_guest_cookie(
    response: Response,
    request: Request,
    session_id: str,
    token: str,
    expires_at: int,
) -> None:
    """Stores guest token in an HttpOnly cookie strictly scoped to one session path."""
    max_age = max(60, int(expires_at) - int(time.time()))
    response.set_cookie(
        key=_cookie_name(session_id),
        value=token,
        httponly=True,
        samesite="lax",
        secure=(request.url.scheme == "https"),
        path=_cookie_path(session_id),
        max_age=max_age,
    )


# ── Endpoints ────────────────────────────────────────────────────────────────
@router.post("/table-sessions", status_code=201)
def create_table_session(
    payload: CreateSessionRequest,
    request: Request,
    response: Response,
    service: SessionServiceDep,
) -> dict[str, Any]:
    try:
        result = service.create_session(
            display_name=payload.display_name,
            user_id=payload.user_id,
            restaurant_id=payload.restaurant_id,
            requires_host_approval=payload.requires_host_approval,
            max_participants=payload.max_participants,
            deadline_at=payload.deadline_at,
            expires_in_seconds=payload.expires_in_seconds,
            split_mode=payload.split_mode,
            payment_mode=payload.payment_mode,
            delivery_address_id=payload.delivery_address_id,
        )
    except SessionError as exc:
        err = _handle_session_error(exc)
        if isinstance(err, JSONResponse):
            return err  # type: ignore[return-value]
        raise err from exc

    host_token = result.get("host_token")
    session_id = result["session_id"]
    if host_token:
        _set_scoped_guest_cookie(
            response,
            request,
            session_id,
            host_token,
            int(result["session"]["expires_at"]),
        )
    return result


@router.get("/table-sessions/{session_id}")
def get_table_session(
    session_id: str,
    request: Request,
    service: SessionServiceDep,
) -> dict[str, Any]:
    raw_token, user_id = _extract_raw_token(request, session_id)
    viewer_pid: str | None = None
    if raw_token or user_id:
        with contextlib.suppress(SessionError):
            caller = service.authenticate_participant(session_id, raw_token, user_id=user_id)
            viewer_pid = caller["id"]

    try:
        return service.get_session_state(session_id, viewer_participant_id=viewer_pid)
    except SessionError as exc:
        err = _handle_session_error(exc)
        if isinstance(err, JSONResponse):
            return err  # type: ignore[return-value]
        raise err from exc


@router.post("/table-sessions/{session_id}/join")
def join_table_session(
    session_id: str,
    payload: JoinSessionRequest,
    request: Request,
    response: Response,
    service: SessionServiceDep,
    limiter: RateLimiterDep,
    token: Annotated[str | None, Query(description="Join token query fallback from share link")] = None,
) -> Any:
    raw_join_token = payload.join_token or token or ""
    ip = _client_ip(request)

    try:
        limiter.check_and_consume("join_ip", ip)
        token_bucket_id = f"{session_id}:{hash_token(raw_join_token) if raw_join_token else 'missing'}"
        limiter.check_and_consume("join_token", token_bucket_id)

        result = service.join_session(
            session_id,
            join_token=raw_join_token,
            display_name=payload.display_name,
            user_id=payload.user_id,
            expected_version=payload.version,
        )
    except (SessionError, RateLimitExceededError) as exc:
        err = _handle_session_error(exc)
        if isinstance(err, JSONResponse):
            return err
        raise err from exc

    guest_token = result.get("guest_token")
    if guest_token:
        _set_scoped_guest_cookie(
            response,
            request,
            session_id,
            guest_token,
            int(result["session"]["expires_at"]),
        )
    return result


@router.post("/table-sessions/{session_id}/participants/{pid}/approve")
def approve_participant(
    session_id: str,
    pid: str,
    payload: VersionedMutationRequest,
    request: Request,
    service: SessionServiceDep,
) -> Any:
    raw_token, user_id = _extract_raw_token(request, session_id)
    try:
        caller = service.authenticate_participant(session_id, raw_token, user_id=user_id)
        return service.approve_participant(
            session_id,
            host_participant_id=caller["id"],
            target_participant_id=pid,
            expected_version=payload.version,
        )
    except SessionError as exc:
        err = _handle_session_error(exc)
        if isinstance(err, JSONResponse):
            return err
        raise err from exc


@router.post("/table-sessions/{session_id}/participants/{pid}/remove")
def remove_participant(
    session_id: str,
    pid: str,
    payload: VersionedMutationRequest,
    request: Request,
    service: SessionServiceDep,
) -> Any:
    raw_token, user_id = _extract_raw_token(request, session_id)
    try:
        caller = service.authenticate_participant(session_id, raw_token, user_id=user_id)
        return service.remove_participant(
            session_id,
            host_participant_id=caller["id"],
            target_participant_id=pid,
            expected_version=payload.version,
        )
    except SessionError as exc:
        err = _handle_session_error(exc)
        if isinstance(err, JSONResponse):
            return err
        raise err from exc


@router.post("/table-sessions/{session_id}/participants/{pid}/transfer-host")
def transfer_host(
    session_id: str,
    pid: str,
    payload: VersionedMutationRequest,
    request: Request,
    service: SessionServiceDep,
) -> Any:
    raw_token, user_id = _extract_raw_token(request, session_id)
    try:
        caller = service.authenticate_participant(session_id, raw_token, user_id=user_id)
        return service.transfer_host(
            session_id,
            host_participant_id=caller["id"],
            target_participant_id=pid,
            expected_version=payload.version,
        )
    except SessionError as exc:
        err = _handle_session_error(exc)
        if isinstance(err, JSONResponse):
            return err
        raise err from exc


@router.post("/table-sessions/{session_id}/leave")
def leave_session(
    session_id: str,
    payload: VersionedMutationRequest,
    request: Request,
    response: Response,
    service: SessionServiceDep,
) -> Any:
    raw_token, user_id = _extract_raw_token(request, session_id)
    try:
        caller = service.authenticate_participant(session_id, raw_token, user_id=user_id)
        updated = service.leave_session(
            session_id,
            participant_id=caller["id"],
            expected_version=payload.version,
        )
        response.delete_cookie(key=_cookie_name(session_id), path=_cookie_path(session_id))
        return updated
    except SessionError as exc:
        err = _handle_session_error(exc)
        if isinstance(err, JSONResponse):
            return err
        raise err from exc


@router.post("/table-sessions/{session_id}/lock")
def lock_session(
    session_id: str,
    payload: VersionedMutationRequest,
    request: Request,
    service: SessionServiceDep,
) -> Any:
    raw_token, user_id = _extract_raw_token(request, session_id)
    try:
        caller = service.authenticate_participant(session_id, raw_token, user_id=user_id)
        return service.lock_session(
            session_id,
            host_participant_id=caller["id"],
            expected_version=payload.version,
        )
    except SessionError as exc:
        err = _handle_session_error(exc)
        if isinstance(err, JSONResponse):
            return err
        raise err from exc


@router.post("/table-sessions/{session_id}/unlock")
def unlock_session(
    session_id: str,
    payload: VersionedMutationRequest,
    request: Request,
    service: SessionServiceDep,
) -> Any:
    raw_token, user_id = _extract_raw_token(request, session_id)
    try:
        caller = service.authenticate_participant(session_id, raw_token, user_id=user_id)
        return service.unlock_session(
            session_id,
            host_participant_id=caller["id"],
            expected_version=payload.version,
        )
    except SessionError as exc:
        err = _handle_session_error(exc)
        if isinstance(err, JSONResponse):
            return err
        raise err from exc


@router.post("/table-sessions/{session_id}/rotate-link")
def rotate_link(
    session_id: str,
    payload: VersionedMutationRequest,
    request: Request,
    service: SessionServiceDep,
) -> Any:
    raw_token, user_id = _extract_raw_token(request, session_id)
    try:
        caller = service.authenticate_participant(session_id, raw_token, user_id=user_id)
        return service.rotate_join_link(
            session_id,
            host_participant_id=caller["id"],
            expected_version=payload.version,
        )
    except SessionError as exc:
        err = _handle_session_error(exc)
        if isinstance(err, JSONResponse):
            return err
        raise err from exc


@router.post("/table-sessions/{session_id}/extend")
def extend_session(
    session_id: str,
    payload: ExtendSessionRequest,
    request: Request,
    service: SessionServiceDep,
) -> Any:
    raw_token, user_id = _extract_raw_token(request, session_id)
    try:
        caller = service.authenticate_participant(session_id, raw_token, user_id=user_id)
        return service.extend_expiry(
            session_id,
            host_participant_id=caller["id"],
            extend_seconds=payload.extend_seconds,
            expected_version=payload.version,
        )
    except SessionError as exc:
        err = _handle_session_error(exc)
        if isinstance(err, JSONResponse):
            return err
        raise err from exc


@router.put("/table-sessions/{session_id}/preferences")
def update_preferences(
    session_id: str,
    payload: UpdatePreferencesRequest,
    request: Request,
    service: SessionServiceDep,
    limiter: RateLimiterDep,
) -> Any:
    raw_token, user_id = _extract_raw_token(request, session_id)
    try:
        caller = service.authenticate_participant(session_id, raw_token, user_id=user_id)
        limiter.check_and_consume("preference_participant", f"{session_id}:{caller['id']}")
        return service.update_preferences(
            session_id,
            participant_id=caller["id"],
            expected_version=payload.version,
            preferences=[p.model_dump() for p in payload.preferences],
            is_ready=payload.is_ready,
        )
    except (SessionError, RateLimitExceededError) as exc:
        err = _handle_session_error(exc)
        if isinstance(err, JSONResponse):
            return err
        raise err from exc


@router.post("/table-sessions/{session_id}/preferences")
def upsert_preferences(
    session_id: str,
    payload: UpsertPreferencesRequest,
    request: Request,
    service: SessionServiceDep,
    limiter: RateLimiterDep,
) -> Any:
    """Upserts confirmed preferences (chips or user-confirmed parsed entries) for the calling participant."""
    raw_token, user_id = _extract_raw_token(request, session_id)
    try:
        caller = service.authenticate_participant(session_id, raw_token, user_id=user_id)
        limiter.check_and_consume("preference_participant", f"{session_id}:{caller['id']}")
        return service.upsert_preferences(
            session_id,
            participant_id=caller["id"],
            expected_version=payload.version,
            preferences=[p.model_dump() for p in payload.preferences],
            is_ready=payload.is_ready,
            replace_all=payload.replace_all,
        )
    except (SessionError, RateLimitExceededError) as exc:
        err = _handle_session_error(exc)
        if isinstance(err, JSONResponse):
            return err
        raise err from exc


@router.post("/table-sessions/{session_id}/preferences/parse")
def parse_free_text_preferences(
    session_id: str,
    payload: ParsePreferencesRequest,
    request: Request,
    service: SessionServiceDep,
    limiter: RateLimiterDep,
    parser: PreferenceParserDep,
) -> Any:
    """
    Parses free-text dining preferences (English/Hinglish/Hindi) via the provider-agnostic
    PreferenceParser into unconfirmed candidate chips. Nothing enters the engine until confirmed.
    """
    raw_token, user_id = _extract_raw_token(request, session_id)
    try:
        caller = service.authenticate_participant(session_id, raw_token, user_id=user_id)
        service.assert_can_edit_preferences(session_id, caller["id"])
        limiter.check_and_consume("preference_participant", f"{session_id}:{caller['id']}")

        vocab = build_menu_vocabulary_from_json()
        parsed = parser.parse(
            text=payload.text,
            locale=payload.locale,
            menu_vocabulary=vocab,
        )
        log_id = record_parser_log(
            service.db_path,
            session_id=session_id,
            participant_id=caller["id"],
            locale=payload.locale,
            raw_input=payload.text,
            parsed=parsed,
            error_code=None if parsed.status == "success" else parsed.status.upper(),
        )
        return {
            "log_id": log_id,
            "status": parsed.status,
            "fallback_to_chips": parsed.fallback_to_chips,
            "friendly_message": parsed.friendly_message,
            "items": [it.model_dump() for it in parsed.items],
            "low_confidence_items": [it.model_dump() for it in parsed.items if it.needs_confirmation_prompt],
        }
    except (SessionError, RateLimitExceededError) as exc:
        err = _handle_session_error(exc)
        if isinstance(err, JSONResponse):
            return err
        raise err from exc


@router.post("/table-sessions/{session_id}/messages")
def post_session_message(
    session_id: str,
    payload: PostMessageRequest,
    request: Request,
    service: SessionServiceDep,
    limiter: RateLimiterDep,
) -> Any:
    raw_token, user_id = _extract_raw_token(request, session_id)
    try:
        caller = service.authenticate_participant(session_id, raw_token, user_id=user_id)
        limiter.check_and_consume("message_participant", f"{session_id}:{caller['id']}")
        return service.post_message(
            session_id,
            participant_id=caller["id"],
            expected_version=payload.version,
            message=payload.message,
        )
    except (SessionError, RateLimitExceededError) as exc:
        err = _handle_session_error(exc)
        if isinstance(err, JSONResponse):
            return err
        raise err from exc


@router.get("/table-sessions/{session_id}/events")
async def stream_session_events(
    session_id: str,
    request: Request,
    service: SessionServiceDep,
    hub: Annotated[SSEHub, Depends(get_sse_hub)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    last_event_id_query: Annotated[str | None, Query(alias="last_event_id")] = None,
    last_seq: Annotated[int | None, Query(ge=0)] = None,
    last_version: Annotated[int | None, Query(ge=0)] = None,
    follow: Annotated[bool, Query()] = True,
    max_events: Annotated[int | None, Query(ge=1, le=500)] = None,
    heartbeat_seconds: Annotated[float, Query(ge=0.5, le=60.0)] = 15.0,
) -> StreamingResponse:
    """
    Server-Sent Events (SSE) stream emitting state-change events with session version.
    Supports reconnect via `Last-Event-ID` header or `last_seq` / `last_version` query params,
    replaying missed events or emitting a sanitized `session.snapshot`.
    """
    effective_event_id = last_event_id or last_event_id_query
    # Register live subscriber BEFORE querying DB so events committed during replay setup are never missed.
    sub = hub.register(session_id)
    try:
        needs_snapshot, missed_events, current_max_seq = service.get_replay_bundle(
            session_id,
            after_seq=last_seq,
            after_version=last_version,
            after_event_id=effective_event_id,
        )
        # Always authenticate snapshot scope if caller presented valid credentials, else anonymous (no private prefs!)
        raw_token, user_id = _extract_raw_token(request, session_id)
        viewer_pid: str | None = None
        if raw_token or user_id:
            with contextlib.suppress(SessionError):
                caller = service.authenticate_participant(session_id, raw_token, user_id=user_id)
                viewer_pid = caller["id"]

        snapshot_state = service.get_session_state(session_id, viewer_participant_id=viewer_pid) if needs_snapshot else None
    except SessionError as exc:
        hub.unregister(session_id, sub)
        err = _handle_session_error(exc)
        if isinstance(err, HTTPException):
            raise err from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        hub.unregister(session_id, sub)
        raise

    async def event_generator() -> AsyncIterator[str]:
        sent_count = 0
        last_sent_seq = last_seq or 0

        try:
            if snapshot_state is not None:
                snap_seq = current_max_seq
                last_sent_seq = max(last_sent_seq, snap_seq)
                snap_payload = {
                    "session_version": snapshot_state["version"],
                    "session": snapshot_state,
                }
                yield format_sse_frame(snap_seq, "session.snapshot", snap_payload)
                sent_count += 1
                if max_events is not None and sent_count >= max_events:
                    return

            for evt in missed_events:
                last_sent_seq = max(last_sent_seq, int(evt["seq"]))
                yield format_sse_frame(evt["seq"], evt["type"], evt)
                sent_count += 1
                if max_events is not None and sent_count >= max_events:
                    return

            if not follow:
                return

            while True:
                if await request.is_disconnected():
                    break
                try:
                    live_evt = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat_seconds)
                    seq_val = int(live_evt.get("seq", 0))
                    if seq_val <= last_sent_seq:
                        continue
                    last_sent_seq = seq_val
                    yield format_sse_frame(seq_val, str(live_evt["type"]), live_evt)
                    sent_count += 1
                    if max_events is not None and sent_count >= max_events:
                        return
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            hub.unregister(session_id, sub)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

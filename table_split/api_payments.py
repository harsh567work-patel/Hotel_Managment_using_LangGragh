"""
table_split.api_payments — FastAPI endpoints for Stage 9 Per-Person Payment Orchestration,
Webhooks (raw-body HMAC + replay protection), Host Timeout Resolution, Tracking, and Refunds.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from config import TABLE_SPLIT_DB_PATH
from table_split.api_sessions import get_rate_limiter, get_sse_hub
from table_split.gateways.razorpay_adapter import RazorpayPaymentProvider
from table_split.gateways.simulated import SimulatedRazorpayProvider
from table_split.payment import (
    DEFAULT_PAYMENT_WINDOW_SECONDS,
    InvalidSignatureError,
    PaymentError,
    PaymentProvider,
    PriceDriftError,
    UnsupportedPaymentMethodError,
)
from table_split.pricing import PricingEngine, StubPricingEngine
from table_split.rate_limit import RateLimitExceededError, SlidingWindowRateLimiter
from table_split.services.payment_reconciler import reconcile_pending_payments
from table_split.services.payment_service import (
    PaymentConflictError,
    PaymentForbiddenError,
    PaymentNotFoundError,
    PaymentOrchestrator,
    PaymentUnauthorizedError,
    PaymentValidationError,
    PaymentWindowExpiredError,
)
from table_split.sse import SSEHub

router = APIRouter(tags=["Table Payments"])


@lru_cache(maxsize=1)
def _default_payment_provider() -> PaymentProvider:
    mode = os.getenv("PAYMENT_GATEWAY_MODE", "simulated").strip().lower()
    if mode in ("razorpay_test", "razorpay_live", "razorpay"):
        return RazorpayPaymentProvider()
    return SimulatedRazorpayProvider()


def get_payment_provider() -> PaymentProvider:
    return _default_payment_provider()


def get_pricing_engine() -> PricingEngine:
    return StubPricingEngine()


def get_payment_orchestrator(
    sse_hub: Annotated[SSEHub, Depends(get_sse_hub)],
    provider: Annotated[PaymentProvider, Depends(get_payment_provider)],
    pricing_engine: Annotated[PricingEngine, Depends(get_pricing_engine)],
) -> PaymentOrchestrator:
    return PaymentOrchestrator(
        db_path=TABLE_SPLIT_DB_PATH,
        provider=provider,
        pricing_engine=pricing_engine,
        sse_hub=sse_hub,
    )


OrchestratorDep = Annotated[PaymentOrchestrator, Depends(get_payment_orchestrator)]
RateLimiterDep = Annotated[SlidingWindowRateLimiter, Depends(get_rate_limiter)]


def _extract_raw_token(request: Request, session_id: str) -> str | None:
    cookie_token = request.cookies.get(f"ts_guest_{session_id}")
    if cookie_token:
        return cookie_token
    header_token = request.headers.get("X-Guest-Token")
    if header_token:
        return header_token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return None


def _handle_payment_error(exc: Exception) -> HTTPException | JSONResponse:
    if isinstance(exc, RateLimitExceededError):
        return JSONResponse(
            status_code=429,
            content={"detail": str(exc), "code": "RATE_LIMITED"},
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    if isinstance(exc, PaymentUnauthorizedError):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, (PaymentForbiddenError, InvalidSignatureError)):
        return JSONResponse(status_code=403, content={"detail": str(exc), "code": exc.code})
    if isinstance(exc, PaymentNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PriceDriftError):
        return JSONResponse(
            status_code=409,
            content={
                "detail": str(exc),
                "code": exc.code,
                "expected_total_paise": exc.expected_total_paise,
                "rechecked_total_paise": exc.rechecked_total_paise,
            },
        )
    if isinstance(exc, (PaymentWindowExpiredError, PaymentConflictError)):
        return JSONResponse(
            status_code=409,
            content={
                "detail": str(exc),
                "code": exc.code,
                "payment_state": exc.state,
            },
        )
    if isinstance(exc, (UnsupportedPaymentMethodError, PaymentValidationError)):
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": exc.code})
    if isinstance(exc, PaymentError):
        return JSONResponse(status_code=400, content={"detail": str(exc), "code": exc.code})
    return HTTPException(status_code=400, detail=str(exc))


# ── Pydantic Request Schemas ─────────────────────────────────────────────────
class StartPaymentsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payment_mode: str | None = None
    window_seconds: int = Field(default=DEFAULT_PAYMENT_WINDOW_SECONDS)


class InitiatePayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str = "upi_qr"
    idempotency_key: str | None = None


class VerifyCheckoutPaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gateway_order_id: str = Field(..., min_length=1)
    gateway_payment_id: str = Field(..., min_length=1)
    gateway_signature: str = Field(..., min_length=1)


class HostResolveWindowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    choice: str
    extend_window_seconds: int = Field(default=DEFAULT_PAYMENT_WINDOW_SECONDS)


class CancelOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="host_cancelled", min_length=1, max_length=120)


class ReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stale_after_seconds: int = Field(default=0, ge=0)


# ── REST Endpoints ───────────────────────────────────────────────────────────
@router.get("/table-sessions/{session_id}/payments")
def get_payment_status(
    session_id: str,
    request: Request,
    orchestrator: OrchestratorDep,
) -> Any:
    raw_token = _extract_raw_token(request, session_id)
    try:
        orchestrator.authenticate_caller(session_id, raw_token)
        return orchestrator.get_payment_state(session_id)
    except (PaymentError, RateLimitExceededError) as exc:
        res = _handle_payment_error(exc)
        if isinstance(res, JSONResponse):
            return res
        raise res from exc


@router.post("/table-sessions/{session_id}/payments/start")
def start_payment_window(
    session_id: str,
    payload: StartPaymentsRequest,
    request: Request,
    orchestrator: OrchestratorDep,
) -> Any:
    raw_token = _extract_raw_token(request, session_id)
    try:
        host = orchestrator.authenticate_caller(session_id, raw_token, require_host=True)
        return orchestrator.start_payments(
            session_id,
            host_participant_id=str(host["id"]),
            payment_mode=payload.payment_mode,
            window_seconds=payload.window_seconds,
        )
    except (PaymentError, RateLimitExceededError) as exc:
        res = _handle_payment_error(exc)
        if isinstance(res, JSONResponse):
            return res
        raise res from exc


@router.post("/table-sessions/{session_id}/payments/pay")
def pay_my_share(
    session_id: str,
    payload: InitiatePayRequest,
    request: Request,
    orchestrator: OrchestratorDep,
    limiter: RateLimiterDep,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    raw_token = _extract_raw_token(request, session_id)
    try:
        caller = orchestrator.authenticate_caller(session_id, raw_token)
        limiter.check_and_consume("preference_participant", f"pay:{session_id}:{caller['id']}")
        return orchestrator.initiate_payment(
            session_id,
            participant_id=str(caller["id"]),
            method=payload.method,
            idempotency_key=idempotency_key_header or payload.idempotency_key,
        )
    except (PaymentError, RateLimitExceededError) as exc:
        res = _handle_payment_error(exc)
        if isinstance(res, JSONResponse):
            return res
        raise res from exc


@router.post("/table-sessions/{session_id}/payments/verify")
def verify_checkout_callback(
    session_id: str,
    payload: VerifyCheckoutPaymentRequest,
    request: Request,
    orchestrator: OrchestratorDep,
) -> Any:
    raw_token = _extract_raw_token(request, session_id)
    try:
        caller = orchestrator.authenticate_caller(session_id, raw_token)
        return orchestrator.verify_checkout_payment(
            session_id,
            participant_id=str(caller["id"]),
            gateway_order_id=payload.gateway_order_id,
            gateway_payment_id=payload.gateway_payment_id,
            gateway_signature=payload.gateway_signature,
        )
    except (PaymentError, RateLimitExceededError) as exc:
        res = _handle_payment_error(exc)
        if isinstance(res, JSONResponse):
            return res
        raise res from exc


@router.post("/table-sessions/{session_id}/payments/host-resolve")
def resolve_expired_payment_window(
    session_id: str,
    payload: HostResolveWindowRequest,
    request: Request,
    orchestrator: OrchestratorDep,
) -> Any:
    raw_token = _extract_raw_token(request, session_id)
    try:
        host = orchestrator.authenticate_caller(session_id, raw_token, require_host=True)
        return orchestrator.resolve_expired_window(
            session_id,
            host_participant_id=str(host["id"]),
            choice=payload.choice,
            extend_window_seconds=payload.extend_window_seconds,
        )
    except (PaymentError, RateLimitExceededError) as exc:
        res = _handle_payment_error(exc)
        if isinstance(res, JSONResponse):
            return res
        raise res from exc


@router.post("/table-sessions/{session_id}/payments/host-settlement")
def mark_informational_host_settlement(
    session_id: str,
    request: Request,
    orchestrator: OrchestratorDep,
) -> Any:
    raw_token = _extract_raw_token(request, session_id)
    try:
        caller = orchestrator.authenticate_caller(session_id, raw_token)
        return orchestrator.mark_host_settlement_paid(
            session_id,
            participant_id=str(caller["id"]),
        )
    except (PaymentError, RateLimitExceededError) as exc:
        res = _handle_payment_error(exc)
        if isinstance(res, JSONResponse):
            return res
        raise res from exc


@router.post("/table-sessions/{session_id}/payments/cancel")
def cancel_and_refund_order(
    session_id: str,
    payload: CancelOrderRequest,
    request: Request,
    orchestrator: OrchestratorDep,
) -> Any:
    raw_token = _extract_raw_token(request, session_id)
    try:
        host = orchestrator.authenticate_caller(session_id, raw_token, require_host=True)
        return orchestrator.cancel_or_request_refund(
            session_id,
            actor_participant_id=str(host["id"]),
            reason=payload.reason,
        )
    except (PaymentError, RateLimitExceededError) as exc:
        res = _handle_payment_error(exc)
        if isinstance(res, JSONResponse):
            return res
        raise res from exc


@router.post("/table-sessions/{session_id}/payments/reconcile")
def trigger_reconciliation_sweep(
    session_id: str,
    payload: ReconcileRequest,
    request: Request,
    orchestrator: OrchestratorDep,
) -> Any:
    raw_token = _extract_raw_token(request, session_id)
    try:
        orchestrator.authenticate_caller(session_id, raw_token, require_host=True)
        return reconcile_pending_payments(
            db_path=orchestrator.db_path,
            provider=orchestrator.provider,
            sse_hub=orchestrator.sse_hub,
            stale_after_seconds=payload.stale_after_seconds,
        )
    except (PaymentError, RateLimitExceededError) as exc:
        res = _handle_payment_error(exc)
        if isinstance(res, JSONResponse):
            return res
        raise res from exc


@router.get("/table-sessions/{session_id}/tracking")
def get_shared_order_tracking(
    session_id: str,
    request: Request,
    orchestrator: OrchestratorDep,
) -> Any:
    raw_token = _extract_raw_token(request, session_id)
    try:
        orchestrator.authenticate_caller(session_id, raw_token)
        state = orchestrator.get_payment_state(session_id)
        if state["kitchen_order"] is None:
            raise PaymentNotFoundError(f"No kitchen order placed yet for session '{session_id}'.")
        return state["kitchen_order"]
    except (PaymentError, RateLimitExceededError) as exc:
        res = _handle_payment_error(exc)
        if isinstance(res, JSONResponse):
            return res
        raise res from exc


@router.post("/table-sessions/{session_id}/kitchen/accept")
def accept_placed_kitchen_order(
    session_id: str,
    orchestrator: OrchestratorDep,
) -> Any:
    try:
        return orchestrator.accept_kitchen_order(session_id)
    except (PaymentError, RateLimitExceededError) as exc:
        res = _handle_payment_error(exc)
        if isinstance(res, JSONResponse):
            return res
        raise res from exc


@router.post("/table-split/webhooks/payment")
@router.post("/table-sessions/webhooks/razorpay")
async def receive_payment_webhook(
    request: Request,
    orchestrator: OrchestratorDep,
    x_razorpay_signature: str | None = Header(default=None, alias="X-Razorpay-Signature"),
    x_razorpay_event_id: str | None = Header(default=None, alias="X-Razorpay-Event-Id"),
) -> Any:
    raw_body = await request.body()
    try:
        return orchestrator.handle_webhook(
            raw_body=raw_body,
            signature=x_razorpay_signature or "",
            event_id_header=x_razorpay_event_id,
        )
    except InvalidSignatureError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc), "code": exc.code})
    except PaymentError as exc:
        res = _handle_payment_error(exc)
        if isinstance(res, JSONResponse):
            return res
        raise res from exc

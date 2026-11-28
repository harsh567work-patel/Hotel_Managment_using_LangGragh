"""
table_split.payment — Pure Stage 9 payment domain contracts, signature verification,
NPCI-compliant UPI Intent/QR helpers (excluding deprecated UPI Collect), and Stage 7 price re-check.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator

from table_split.models import validate_strict_paise
from table_split.pricing import (
    DEFAULT_GST_BPS,
    PricingEngine,
    PricingLineInput,
    ProposalPricingResult,
    StubPricingEngine,
)

PaymentMode = Literal["each_pays_own", "host_pays_all"]
AllowedPaymentMethod = Literal["upi_intent", "upi_qr", "card", "netbanking"]
HostResolutionAction = Literal["cover_unpaid", "drop_unpaid_re_split", "cancel_and_refund"]

ALLOWED_PAYMENT_METHODS: frozenset[str] = frozenset({"upi_intent", "upi_qr", "card", "netbanking"})
# NPCI deprecated standard UPI Collect effective Feb 28, 2026; restaurant orders reject it strictly.
REJECTED_COLLECT_METHODS: frozenset[str] = frozenset({"upi_collect", "collect", "vpa_collect"})

DEFAULT_PAYMENT_WINDOW_SECONDS = 15 * 60  # 15 minutes
MIN_PAYMENT_WINDOW_SECONDS = 60
MAX_PAYMENT_WINDOW_SECONDS = 30 * 60  # 30 minutes max

PAYMENT_STATES: frozenset[str] = frozenset({
    "created",
    "pending",
    "captured",
    "failed",
    "refunded",
    "partially_refunded",
})


class PaymentError(Exception):
    """Base error for Table Split Stage 9 payment operations."""

    def __init__(self, message: str, *, code: str = "PAYMENT_ERROR") -> None:
        self.code = code
        super().__init__(message)


class UnsupportedPaymentMethodError(PaymentError):
    """Raised when an unsupported or NPCI-deprecated method (e.g. UPI Collect) is requested."""

    def __init__(self, method: str) -> None:
        super().__init__(
            f"Payment method '{method}' is not supported. Use upi_intent, upi_qr, card, or netbanking "
            "(UPI Collect was deprecated by NPCI effective 28 Feb 2026).",
            code="UNSUPPORTED_PAYMENT_METHOD",
        )


class InvalidSignatureError(PaymentError):
    """Raised on failed checkout or webhook HMAC signature check."""

    def __init__(self, message: str = "Invalid gateway cryptographic signature.") -> None:
        super().__init__(message, code="INVALID_SIGNATURE")


class PriceDriftError(PaymentError):
    """Raised when Stage 7 price re-check detects stale menu prices before freezing payments."""

    def __init__(
        self,
        message: str,
        *,
        expected_total_paise: int,
        rechecked_total_paise: int,
        rechecked_pricing: ProposalPricingResult,
    ) -> None:
        self.expected_total_paise = expected_total_paise
        self.rechecked_total_paise = rechecked_total_paise
        self.rechecked_pricing = rechecked_pricing
        super().__init__(message, code="PRICE_DRIFT_DETECTED")


def validate_payment_method(method: str) -> AllowedPaymentMethod:
    """Normalizes and enforces allowed Indian checkout methods, blocking deprecated UPI Collect."""
    norm = method.strip().lower().replace("-", "_")
    if norm in REJECTED_COLLECT_METHODS:
        raise UnsupportedPaymentMethodError(method)
    if norm not in ALLOWED_PAYMENT_METHODS:
        raise UnsupportedPaymentMethodError(method)
    return norm  # type: ignore[return-value]


def validate_payment_window_seconds(window_seconds: int | None) -> int:
    """Validates paying window in [60, 1800] seconds (1 to 30 minutes, default 15 minutes)."""
    if window_seconds is None:
        return DEFAULT_PAYMENT_WINDOW_SECONDS
    if isinstance(window_seconds, bool) or not isinstance(window_seconds, int):
        raise PaymentError("window_seconds must be a strict integer.", code="INVALID_WINDOW")
    if window_seconds < MIN_PAYMENT_WINDOW_SECONDS or window_seconds > MAX_PAYMENT_WINDOW_SECONDS:
        raise PaymentError(
            f"window_seconds must be between {MIN_PAYMENT_WINDOW_SECONDS} and {MAX_PAYMENT_WINDOW_SECONDS} (max 30 min).",
            code="INVALID_WINDOW",
        )
    return window_seconds


def format_paise_decimal_inr(raw_paise: int) -> str:
    """Converts integer paise to exact INR decimal string without float arithmetic (e.g., 25050 -> '250.50')."""
    paise = validate_strict_paise(raw_paise)
    rupees, rem = divmod(paise, 100)
    return f"{rupees}.{rem:02d}"


def build_upi_intent_uri(
    *,
    payee_vpa: str,
    payee_name: str,
    amount_paise: int,
    transaction_ref: str,
    note: str = "SpiceRoute Table Split",
) -> str:
    """Builds a standard NPCI UPI Intent deep link URI without Collect/VPA push."""
    paise = validate_strict_paise(amount_paise)
    if paise <= 0:
        raise PaymentError("UPI amount_paise must be > 0.", code="INVALID_AMOUNT")
    am = format_paise_decimal_inr(paise)
    return (
        f"upi://pay?pa={quote(payee_vpa, safe='@')}"
        f"&pn={quote(payee_name, safe='')}"
        f"&tr={quote(transaction_ref, safe='')}"
        f"&tn={quote(note, safe='')}"
        f"&am={am}&cu=INR"
    )


def build_upi_qr_payload(
    *,
    payee_vpa: str,
    payee_name: str,
    amount_paise: int,
    transaction_ref: str,
    note: str = "SpiceRoute Table Split",
) -> str:
    """Builds a Dynamic UPI QR payload URI containing fixed integer-derived INR amount and ref."""
    return build_upi_intent_uri(
        payee_vpa=payee_vpa,
        payee_name=payee_name,
        amount_paise=amount_paise,
        transaction_ref=transaction_ref,
        note=note,
    )


def compute_razorpay_checkout_signature(*, order_id: str, payment_id: str, key_secret: str) -> str:
    """Computes Razorpay Checkout HMAC-SHA256 signature over `order_id|payment_id`."""
    payload = f"{order_id}|{payment_id}".encode()
    return hmac.new(key_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_razorpay_checkout_signature(
    *,
    order_id: str,
    payment_id: str,
    signature: str,
    key_secret: str,
) -> bool:
    """Constant-time verification of Razorpay checkout response signature."""
    if not order_id or not payment_id or not signature or not key_secret:
        return False
    expected = compute_razorpay_checkout_signature(
        order_id=order_id,
        payment_id=payment_id,
        key_secret=key_secret,
    )
    return hmac.compare_digest(expected, signature.strip().lower())


def compute_razorpay_webhook_signature(*, raw_body: bytes, webhook_secret: str) -> str:
    """Computes Razorpay webhook HMAC-SHA256 digest over raw unparsed request body bytes."""
    return hmac.new(webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_razorpay_webhook_signature(
    *,
    raw_body: bytes,
    signature: str,
    webhook_secret: str,
) -> bool:
    """Constant-time verification of Razorpay raw webhook body against X-Razorpay-Signature."""
    if not signature or not webhook_secret:
        return False
    expected = compute_razorpay_webhook_signature(raw_body=raw_body, webhook_secret=webhook_secret)
    return hmac.compare_digest(expected, signature.strip().lower())


class GatewayOrderRequest(BaseModel):
    """Input to create exactly one single-payer order on the payment gateway."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    participant_id: str
    amount_paise: int
    currency: Literal["INR"] = "INR"
    method: AllowedPaymentMethod = "upi_qr"
    receipt: str = Field(..., min_length=1, max_length=40)
    idempotency_key: str = Field(..., min_length=1, max_length=64)
    notes: dict[str, str] = Field(default_factory=dict)

    @field_validator("amount_paise", mode="before")
    @classmethod
    def _strict_amount(cls, v: Any) -> int:
        amt = validate_strict_paise(v)
        if amt <= 0:
            raise ValueError("Gateway order amount_paise must be > 0.")
        return amt


class GatewayOrderResult(BaseModel):
    """Normalized order response from a PaymentProvider adapter."""

    model_config = ConfigDict(extra="forbid")

    gateway: str
    gateway_order_id: str
    amount_paise: int
    currency: str = "INR"
    receipt: str
    status: str
    method: AllowedPaymentMethod
    upi_intent_uri: str | None = None
    upi_qr_payload: str | None = None
    checkout_options: dict[str, Any] = Field(default_factory=dict)


class GatewayPaymentResult(BaseModel):
    """Normalized payment record returned when querying a gateway order or payment."""

    model_config = ConfigDict(extra="forbid")

    gateway: str
    gateway_order_id: str
    gateway_payment_id: str
    amount_paise: int
    currency: str = "INR"
    status: Literal["created", "pending", "captured", "failed", "refunded"]
    method: str = "upi_qr"
    failure_reason: str | None = None


class GatewayRefundResult(BaseModel):
    """Normalized refund result from a PaymentProvider adapter."""

    model_config = ConfigDict(extra="forbid")

    gateway: str
    gateway_refund_id: str
    gateway_payment_id: str
    amount_paise: int
    status: Literal["created", "pending", "processed", "failed"]
    receipt: str | None = None


class ParsedWebhookPayload(BaseModel):
    """Normalized event parsed from verified raw webhook JSON."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    event_type: str
    gateway_order_id: str | None = None
    gateway_payment_id: str | None = None
    gateway_refund_id: str | None = None
    amount_paise: int | None = None
    method: str | None = None
    status: str | None = None
    failure_reason: str | None = None
    occurred_at: int = 0
    raw_payload: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class PaymentProvider(Protocol):
    """
    Provider-agnostic Stage 9 payment gateway interface.
    Implementations handle Razorpay live/test APIs cleanly without leaking SDK specifics into services.
    """

    gateway_name: str

    def create_order(self, req: GatewayOrderRequest) -> GatewayOrderResult: ...

    def fetch_order_payments(self, gateway_order_id: str) -> list[GatewayPaymentResult]: ...

    def fetch_payment(self, gateway_payment_id: str) -> GatewayPaymentResult: ...

    def create_refund(
        self,
        *,
        gateway_payment_id: str,
        amount_paise: int,
        receipt: str,
        notes: Mapping[str, str] | None = None,
    ) -> GatewayRefundResult: ...

    def verify_checkout_signature(
        self,
        *,
        gateway_order_id: str,
        gateway_payment_id: str,
        gateway_signature: str,
    ) -> bool: ...

    def verify_webhook_signature(
        self,
        *,
        raw_body: bytes,
        signature: str,
    ) -> bool: ...

    def parse_webhook_event(
        self,
        *,
        raw_body: bytes,
        event_id: str | None = None,
    ) -> ParsedWebhookPayload: ...


def build_participant_receipt(session_id: str, attempt_no: int, participant_id: str) -> str:
    """Produces a deterministic Razorpay-compliant <=40-char receipt string."""
    s_short = session_id.replace("-", "")[:12]
    p_short = participant_id.replace("-", "")[:12]
    return f"sr_{s_short}_{p_short}_{attempt_no}"[:40]


def derive_pay_idempotency_key(
    *,
    session_id: str,
    participant_id: str,
    proposal_id: str,
    client_key: str | None,
) -> str:
    """Derives a unique scoped idempotency key (respecting caller's key while scoping to session/payer)."""
    raw = (client_key or f"auto:{session_id}:{proposal_id}:{participant_id}").strip()
    digest = hashlib.sha256(f"{session_id}|{participant_id}|{raw}".encode()).hexdigest()[:48]
    return f"pay_{digest}"


def recheck_proposal_pricing(
    *,
    lines: Sequence[PricingLineInput],
    participant_ids: Sequence[str],
    expected_grand_total_paise: int | None = None,
    expected_allocations_by_pid: Mapping[str, int] | None = None,
    delivery_fee_paise: int = 0,
    pricing_engine: PricingEngine | None = None,
) -> ProposalPricingResult:
    """
    Runs a live Stage 7 PricingEngine calculation against current line inputs and verifies
    that neither grand_total_paise nor any participant allocation drifted before freezing payments.
    """
    engine: PricingEngine = pricing_engine or StubPricingEngine()
    rechecked = engine.calculate_proposal_pricing(
        lines,
        participant_ids,
        delivery_fee_paise=delivery_fee_paise,
        default_tax_rate_bps=DEFAULT_GST_BPS,
    )

    if expected_grand_total_paise is not None:
        exp_total = validate_strict_paise(expected_grand_total_paise)
        if rechecked.grand_total_paise != exp_total:
            raise PriceDriftError(
                f"Menu total drifted from {exp_total}p to {rechecked.grand_total_paise}p. Re-approve before paying.",
                expected_total_paise=exp_total,
                rechecked_total_paise=rechecked.grand_total_paise,
                rechecked_pricing=rechecked,
            )

    if expected_allocations_by_pid is not None:
        for pid in participant_ids:
            exp_share = validate_strict_paise(expected_allocations_by_pid.get(pid, 0))
            actual_share = rechecked.allocations[pid].total_paise if pid in rechecked.allocations else 0
            if actual_share != exp_share:
                raise PriceDriftError(
                    f"Allocation for '{pid}' drifted from {exp_share}p to {actual_share}p.",
                    expected_total_paise=sum(validate_strict_paise(v) for v in expected_allocations_by_pid.values()),
                    rechecked_total_paise=rechecked.grand_total_paise,
                    rechecked_pricing=rechecked,
                )

    return rechecked

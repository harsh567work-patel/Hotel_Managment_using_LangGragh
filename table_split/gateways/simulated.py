"""
table_split.gateways.simulated — Deterministic Razorpay sandbox simulator for tests & offline runs.

Implements the exact PaymentProvider protocol including real HMAC-SHA256 checkout & raw-body
webhook signature checks, order tracking, capture/failure/pending-stuck simulation, and refunds.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from table_split.gateways.razorpay_adapter import RazorpayPaymentProvider
from table_split.models import validate_strict_paise
from table_split.payment import (
    GatewayOrderRequest,
    GatewayOrderResult,
    GatewayPaymentResult,
    GatewayRefundResult,
    ParsedWebhookPayload,
    PaymentError,
    build_upi_intent_uri,
    build_upi_qr_payload,
    compute_razorpay_checkout_signature,
    compute_razorpay_webhook_signature,
    validate_payment_method,
    verify_razorpay_checkout_signature,
    verify_razorpay_webhook_signature,
)


class SimulatedRazorpayProvider:
    """Thread-safe deterministic Razorpay simulator enforcing real HMAC cryptography."""

    gateway_name: str = "razorpay"

    def __init__(
        self,
        *,
        key_id: str = "rzp_test_sim_spiceroute",
        key_secret: str = "sim_key_secret_for_tests",
        webhook_secret: str = "sim_webhook_secret_for_tests",
        merchant_vpa: str = "spiceroute@icici",
        merchant_name: str = "SpiceRoute Kitchen",
    ) -> None:
        self.key_id = key_id
        self.key_secret = key_secret
        self.webhook_secret = webhook_secret
        self.merchant_vpa = merchant_vpa
        self.merchant_name = merchant_name

        self._lock = threading.Lock()
        self._seq = 0
        self.orders: dict[str, dict[str, Any]] = {}
        self.payments_by_id: dict[str, dict[str, Any]] = {}
        self.payments_by_order: dict[str, list[str]] = {}
        self.refunds_by_id: dict[str, dict[str, Any]] = {}
        self.refund_calls: list[dict[str, Any]] = []

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq:06d}_{uuid4().hex[:6]}"

    def create_order(self, req: GatewayOrderRequest) -> GatewayOrderResult:
        method = validate_payment_method(req.method)
        amount = validate_strict_paise(req.amount_paise)
        with self._lock:
            order_id = self._next_id("order_sim")
            self.orders[order_id] = {
                "id": order_id,
                "amount": amount,
                "currency": req.currency,
                "receipt": req.receipt[:40],
                "status": "created",
                "method": method,
                "notes": dict(req.notes),
                "session_id": req.session_id,
                "participant_id": req.participant_id,
            }
            self.payments_by_order.setdefault(order_id, [])

        upi_intent_uri = None
        upi_qr_payload = None
        if method == "upi_intent":
            upi_intent_uri = build_upi_intent_uri(
                payee_vpa=self.merchant_vpa,
                payee_name=self.merchant_name,
                amount_paise=amount,
                transaction_ref=order_id,
            )
        elif method == "upi_qr":
            upi_qr_payload = build_upi_qr_payload(
                payee_vpa=self.merchant_vpa,
                payee_name=self.merchant_name,
                amount_paise=amount,
                transaction_ref=order_id,
            )

        return GatewayOrderResult(
            gateway=self.gateway_name,
            gateway_order_id=order_id,
            amount_paise=amount,
            currency=req.currency,
            receipt=req.receipt,
            status="created",
            method=method,
            upi_intent_uri=upi_intent_uri,
            upi_qr_payload=upi_qr_payload,
            checkout_options={
                "key": self.key_id,
                "order_id": order_id,
                "amount": amount,
                "currency": req.currency,
                "name": self.merchant_name,
                "simulated": True,
            },
        )

    def simulate_payment_capture(
        self,
        gateway_order_id: str,
        *,
        payment_id: str | None = None,
        method: str | None = None,
        amount_paise: int | None = None,
    ) -> tuple[str, str]:
        """Simulates customer payment capture on Razorpay and returns `(payment_id, signature)`."""
        with self._lock:
            order = self.orders.get(gateway_order_id)
            if order is None:
                raise PaymentError(f"Unknown simulated order '{gateway_order_id}'.", code="ORDER_NOT_FOUND")
            pid = payment_id or self._next_id("pay_sim")
            amt = order["amount"] if amount_paise is None else validate_strict_paise(amount_paise)
            pm = str(method or order["method"])
            rec = {
                "id": pid,
                "order_id": gateway_order_id,
                "amount": amt,
                "currency": "INR",
                "status": "captured",
                "method": pm,
                "failure_reason": None,
            }
            self.payments_by_id[pid] = rec
            self.payments_by_order.setdefault(gateway_order_id, []).append(pid)
            order["status"] = "paid"
            sig = compute_razorpay_checkout_signature(
                order_id=gateway_order_id,
                payment_id=pid,
                key_secret=self.key_secret,
            )
            return pid, sig

    def simulate_payment_pending(
        self,
        gateway_order_id: str,
        *,
        payment_id: str | None = None,
        settle_on_gateway_later: bool = False,
    ) -> str:
        """Simulates an authorized/pending payment (optionally already settled on gateway side for reconciler)."""
        with self._lock:
            order = self.orders.get(gateway_order_id)
            if order is None:
                raise PaymentError(f"Unknown simulated order '{gateway_order_id}'.", code="ORDER_NOT_FOUND")
            pid = payment_id or self._next_id("pay_sim")
            rec = {
                "id": pid,
                "order_id": gateway_order_id,
                "amount": order["amount"],
                "currency": "INR",
                "status": "captured" if settle_on_gateway_later else "pending",
                "method": str(order["method"]),
                "failure_reason": None,
            }
            self.payments_by_id[pid] = rec
            self.payments_by_order.setdefault(gateway_order_id, []).append(pid)
            if settle_on_gateway_later:
                order["status"] = "paid"
            return pid

    def simulate_payment_failure(
        self,
        gateway_order_id: str,
        *,
        reason: str = "PAYMENT_DECLINED_BY_ISSUER",
        payment_id: str | None = None,
    ) -> str:
        with self._lock:
            order = self.orders.get(gateway_order_id)
            if order is None:
                raise PaymentError(f"Unknown simulated order '{gateway_order_id}'.", code="ORDER_NOT_FOUND")
            pid = payment_id or self._next_id("pay_sim")
            rec = {
                "id": pid,
                "order_id": gateway_order_id,
                "amount": order["amount"],
                "currency": "INR",
                "status": "failed",
                "method": str(order["method"]),
                "failure_reason": reason,
            }
            self.payments_by_id[pid] = rec
            self.payments_by_order.setdefault(gateway_order_id, []).append(pid)
            return pid

    def fetch_order_payments(self, gateway_order_id: str) -> list[GatewayPaymentResult]:
        with self._lock:
            pids = list(self.payments_by_order.get(gateway_order_id, []))
            out: list[GatewayPaymentResult] = []
            for pid in pids:
                rec = self.payments_by_id[pid]
                out.append(
                    GatewayPaymentResult(
                        gateway=self.gateway_name,
                        gateway_order_id=rec["order_id"],
                        gateway_payment_id=rec["id"],
                        amount_paise=int(rec["amount"]),
                        currency="INR",
                        status=rec["status"],
                        method=rec["method"],
                        failure_reason=rec["failure_reason"],
                    )
                )
            return out

    def fetch_payment(self, gateway_payment_id: str) -> GatewayPaymentResult:
        with self._lock:
            rec = self.payments_by_id.get(gateway_payment_id)
            if rec is None:
                raise PaymentError(f"Unknown simulated payment '{gateway_payment_id}'.", code="PAYMENT_NOT_FOUND")
            return GatewayPaymentResult(
                gateway=self.gateway_name,
                gateway_order_id=rec["order_id"],
                gateway_payment_id=rec["id"],
                amount_paise=int(rec["amount"]),
                currency="INR",
                status=rec["status"],
                method=rec["method"],
                failure_reason=rec["failure_reason"],
            )

    def create_refund(
        self,
        *,
        gateway_payment_id: str,
        amount_paise: int,
        receipt: str,
        notes: Mapping[str, str] | None = None,
    ) -> GatewayRefundResult:
        amt = validate_strict_paise(amount_paise)
        if amt <= 0:
            raise PaymentError("Refund amount_paise must be > 0.", code="INVALID_AMOUNT")
        with self._lock:
            rid = self._next_id("rfnd_sim")
            pay = self.payments_by_id.get(gateway_payment_id)
            if pay is not None and amt >= int(pay["amount"]):
                pay["status"] = "refunded"
            call_rec = {
                "gateway_refund_id": rid,
                "gateway_payment_id": gateway_payment_id,
                "amount_paise": amt,
                "receipt": receipt[:40],
                "notes": dict(notes or {}),
                "status": "processed",
            }
            self.refunds_by_id[rid] = call_rec
            self.refund_calls.append(call_rec)
            return GatewayRefundResult(
                gateway=self.gateway_name,
                gateway_refund_id=rid,
                gateway_payment_id=gateway_payment_id,
                amount_paise=amt,
                status="processed",
                receipt=receipt[:40],
            )

    def verify_checkout_signature(
        self,
        *,
        gateway_order_id: str,
        gateway_payment_id: str,
        gateway_signature: str,
    ) -> bool:
        return verify_razorpay_checkout_signature(
            order_id=gateway_order_id,
            payment_id=gateway_payment_id,
            signature=gateway_signature,
            key_secret=self.key_secret,
        )

    def verify_webhook_signature(
        self,
        *,
        raw_body: bytes,
        signature: str,
    ) -> bool:
        return verify_razorpay_webhook_signature(
            raw_body=raw_body,
            signature=signature,
            webhook_secret=self.webhook_secret,
        )

    def parse_webhook_event(
        self,
        *,
        raw_body: bytes,
        event_id: str | None = None,
    ) -> ParsedWebhookPayload:
        # Delegate webhook payload extraction cleanly to RazorpayPaymentProvider parser
        return RazorpayPaymentProvider(
            key_id=self.key_id,
            key_secret=self.key_secret,
            webhook_secret=self.webhook_secret,
        ).parse_webhook_event(raw_body=raw_body, event_id=event_id)

    def build_signed_webhook(
        self,
        *,
        event_type: str,
        gateway_order_id: str | None = None,
        gateway_payment_id: str | None = None,
        gateway_refund_id: str | None = None,
        amount_paise: int | None = None,
        status: str | None = None,
        method: str = "upi_qr",
        failure_reason: str | None = None,
        event_id: str | None = None,
        created_at: int | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        """Builds exact signed JSON bytes + Razorpay headers cleanly for tests & local sandbox."""
        ev_id = event_id or f"evt_sim_{uuid4().hex[:12]}"
        ts = int(time.time()) if created_at is None else int(created_at)
        payload: dict[str, Any] = {}
        if gateway_payment_id or event_type.startswith("payment."):
            payload["payment"] = {
                "entity": {
                    "id": gateway_payment_id,
                    "order_id": gateway_order_id,
                    "amount": amount_paise,
                    "currency": "INR",
                    "status": status or ("failed" if event_type == "payment.failed" else "captured"),
                    "method": method,
                    "error_description": failure_reason,
                }
            }
        if gateway_order_id and event_type == "order.paid":
            payload["order"] = {
                "entity": {
                    "id": gateway_order_id,
                    "amount": amount_paise,
                    "currency": "INR",
                    "status": "paid",
                }
            }
        if gateway_refund_id or event_type.startswith("refund."):
            payload["refund"] = {
                "entity": {
                    "id": gateway_refund_id,
                    "payment_id": gateway_payment_id,
                    "amount": amount_paise,
                    "status": status or "processed",
                }
            }
        body_obj = {
            "entity": "event",
            "account_id": "acc_sim",
            "event": event_type,
            "event_id": ev_id,
            "created_at": ts,
            "payload": payload,
        }
        raw_bytes = json.dumps(body_obj, separators=(",", ":")).encode("utf-8")
        sig = compute_razorpay_webhook_signature(raw_body=raw_bytes, webhook_secret=self.webhook_secret)
        return raw_bytes, {
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sig,
            "X-Razorpay-Event-Id": ev_id,
        }

"""
table_split.gateways.razorpay_adapter — Provider-agnostic Razorpay adapter via httpx REST APIs.

Implements:
- Orders API (`POST /v1/orders`, one order per payer in integer paise)
- NPCI-compliant Indian instruments (UPI Intent, Dynamic UPI QR, Cards, Netbanking; NO UPI Collect)
- Constant-time HMAC-SHA256 verification for Checkout (`order_id|payment_id`) & raw-body Webhooks
- Reconciliation (`GET /v1/orders/{id}/payments`, `GET /v1/payments/{id}`)
- Programmatic Refunds (`POST /v1/payments/{id}/refund`)
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

import httpx

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
    validate_payment_method,
    verify_razorpay_checkout_signature,
    verify_razorpay_webhook_signature,
)


class RazorpayPaymentProvider:
    """Production/Test-Mode Razorpay gateway adapter backed by Razorpay v1 REST endpoints."""

    gateway_name: str = "razorpay"

    def __init__(
        self,
        *,
        key_id: str | None = None,
        key_secret: str | None = None,
        webhook_secret: str | None = None,
        merchant_vpa: str | None = None,
        merchant_name: str = "SpiceRoute Kitchen",
        base_url: str = "https://api.razorpay.com/v1",
        timeout_seconds: float = 10.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.key_id = key_id if key_id is not None else os.getenv("RAZORPAY_KEY_ID", "")
        self.key_secret = key_secret if key_secret is not None else os.getenv("RAZORPAY_KEY_SECRET", "")
        self.webhook_secret = (
            webhook_secret if webhook_secret is not None else os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
        )
        self.merchant_vpa = merchant_vpa if merchant_vpa is not None else os.getenv("SPICEROUTE_UPI_VPA", "spiceroute@icici")
        self.merchant_name = merchant_name
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._injected_client = http_client

    def _require_api_credentials(self) -> tuple[str, str]:
        if not self.key_id or not self.key_secret:
            raise PaymentError(
                "Razorpay API credentials (RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET) are not configured.",
                code="GATEWAY_NOT_CONFIGURED",
            )
        return (self.key_id, self.key_secret)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        auth = self._require_api_credentials()
        url = f"{self.base_url}{path}"
        if self._injected_client is not None:
            resp = self._injected_client.request(
                method,
                url,
                auth=auth,
                json=json_body,
                timeout=self.timeout_seconds,
            )
        else:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                resp = client.request(method, url, auth=auth, json=json_body)

        if resp.status_code >= 400:
            raise PaymentError(
                f"Razorpay API error ({resp.status_code}) on {method} {path}: {resp.text[:300]}",
                code="GATEWAY_HTTP_ERROR",
            )
        data = resp.json()
        if not isinstance(data, dict):
            raise PaymentError("Unexpected non-object JSON returned from Razorpay.", code="GATEWAY_BAD_RESPONSE")
        return data

    def create_order(self, req: GatewayOrderRequest) -> GatewayOrderResult:
        method = validate_payment_method(req.method)
        amount = validate_strict_paise(req.amount_paise)
        payload = {
            "amount": amount,
            "currency": req.currency,
            "receipt": req.receipt[:40],
            "payment_capture": 1,
            "notes": {
                **req.notes,
                "session_id": req.session_id,
                "participant_id": req.participant_id,
                "preferred_method": method,
                "idempotency_key": req.idempotency_key,
            },
        }
        data = self._request("POST", "/orders", json_body=payload)
        order_id = str(data.get("id", ""))
        if not order_id:
            raise PaymentError("Razorpay create order response missing id.", code="GATEWAY_BAD_RESPONSE")

        upi_intent_uri = None
        upi_qr_payload = None
        if method == "upi_intent":
            upi_intent_uri = build_upi_intent_uri(
                payee_vpa=self.merchant_vpa,
                payee_name=self.merchant_name,
                amount_paise=amount,
                transaction_ref=order_id,
                note=f"SpiceRoute Order {req.receipt[:18]}",
            )
        elif method == "upi_qr":
            upi_qr_payload = build_upi_qr_payload(
                payee_vpa=self.merchant_vpa,
                payee_name=self.merchant_name,
                amount_paise=amount,
                transaction_ref=order_id,
                note=f"SpiceRoute Order {req.receipt[:18]}",
            )

        # Restrict checkout UI away from NPCI-deprecated UPI Collect
        checkout_opts = {
            "key": self.key_id,
            "order_id": order_id,
            "amount": amount,
            "currency": req.currency,
            "name": self.merchant_name,
            "method": {
                "upi": method in ("upi_intent", "upi_qr"),
                "card": method == "card",
                "netbanking": method == "netbanking",
            },
            "config": {
                "display": {
                    "blocks": {
                        "upi": {
                            "instruments": [
                                {"method": "upi", "flows": ["intent" if method == "upi_intent" else "qr"]}
                            ]
                        }
                    }
                }
            },
        }

        return GatewayOrderResult(
            gateway=self.gateway_name,
            gateway_order_id=order_id,
            amount_paise=int(data.get("amount", amount)),
            currency=str(data.get("currency", "INR")),
            receipt=str(data.get("receipt", req.receipt)),
            status=str(data.get("status", "created")),
            method=method,
            upi_intent_uri=upi_intent_uri,
            upi_qr_payload=upi_qr_payload,
            checkout_options=checkout_opts,
        )

    @staticmethod
    def _normalize_payment(item: Mapping[str, Any], fallback_order_id: str = "") -> GatewayPaymentResult:
        raw_status = str(item.get("status", "created")).lower()
        status_map: dict[str, Any] = {
            "created": "created",
            "authorized": "pending",  # Only treat captured funds as final capture
            "captured": "captured",
            "failed": "failed",
            "refunded": "refunded",
        }
        norm_status = status_map.get(raw_status, "pending")
        return GatewayPaymentResult(
            gateway="razorpay",
            gateway_order_id=str(item.get("order_id") or fallback_order_id),
            gateway_payment_id=str(item.get("id", "")),
            amount_paise=int(item.get("amount", 0)),
            currency=str(item.get("currency", "INR")),
            status=norm_status,
            method=str(item.get("method") or "upi_qr"),
            failure_reason=item.get("error_description") or item.get("error_reason"),
        )

    def fetch_order_payments(self, gateway_order_id: str) -> list[GatewayPaymentResult]:
        data = self._request("GET", f"/orders/{gateway_order_id}/payments")
        items = data.get("items") or []
        return [self._normalize_payment(it, fallback_order_id=gateway_order_id) for it in items if isinstance(it, Mapping)]

    def fetch_payment(self, gateway_payment_id: str) -> GatewayPaymentResult:
        data = self._request("GET", f"/payments/{gateway_payment_id}")
        return self._normalize_payment(data)

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
        body: dict[str, Any] = {
            "amount": amt,
            "speed": "normal",
            "receipt": receipt[:40],
            "notes": dict(notes or {}),
        }
        data = self._request("POST", f"/payments/{gateway_payment_id}/refund", json_body=body)
        raw_st = str(data.get("status", "pending")).lower()
        norm_st = raw_st if raw_st in ("created", "pending", "processed", "failed") else "pending"
        return GatewayRefundResult(
            gateway=self.gateway_name,
            gateway_refund_id=str(data.get("id", "")),
            gateway_payment_id=str(data.get("payment_id", gateway_payment_id)),
            amount_paise=int(data.get("amount", amt)),
            status=norm_st,  # type: ignore[arg-type]
            receipt=data.get("receipt"),
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
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            raise PaymentError(f"Malformed webhook JSON payload: {exc}", code="INVALID_WEBHOOK_JSON") from exc

        event_type = str(body.get("event", ""))
        payload_obj = body.get("payload") or {}
        payment_entity = (payload_obj.get("payment") or {}).get("entity") or {}
        order_entity = (payload_obj.get("order") or {}).get("entity") or {}
        refund_entity = (payload_obj.get("refund") or {}).get("entity") or {}

        gw_order_id = payment_entity.get("order_id") or order_entity.get("id")
        gw_payment_id = payment_entity.get("id") or refund_entity.get("payment_id")
        gw_refund_id = refund_entity.get("id")

        amount_raw = refund_entity.get("amount") or payment_entity.get("amount") or order_entity.get("amount")
        amount_paise = int(amount_raw) if amount_raw is not None else None

        status = str(
            refund_entity.get("status")
            or payment_entity.get("status")
            or order_entity.get("status")
            or ""
        ).lower() or None

        resolved_event_id = (event_id or "").strip()
        if not resolved_event_id:
            resolved_event_id = str(body.get("event_id") or f"{event_type}:{gw_order_id}:{gw_payment_id}:{gw_refund_id}")

        return ParsedWebhookPayload(
            event_id=resolved_event_id,
            event_type=event_type,
            gateway_order_id=str(gw_order_id) if gw_order_id else None,
            gateway_payment_id=str(gw_payment_id) if gw_payment_id else None,
            gateway_refund_id=str(gw_refund_id) if gw_refund_id else None,
            amount_paise=amount_paise,
            method=str(payment_entity.get("method")) if payment_entity.get("method") else None,
            status=status,
            failure_reason=payment_entity.get("error_description") or payment_entity.get("error_reason"),
            occurred_at=int(body.get("created_at") or 0),
            raw_payload=body,
        )

"""
table_split.db.payment_models — SQLAlchemy ORM definitions for Stage 9 Payment Orchestration tables.
Matches Migration 006 cleanly without modifying shared base ORM models in models.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from table_split.db.models import Base


def _now_ts() -> int:
    return int(datetime.now(UTC).timestamp())


class PaymentSessionRecord(Base):
    __tablename__ = "payment_sessions"

    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("table_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    proposal_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False
    )
    payment_mode: Mapped[str] = mapped_column(String(32), default="each_pays_own", nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, default=900, nullable=False)
    started_at: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)
    frozen_total_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    price_recheck_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    host_resolution: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[int] = mapped_column(Integer, default=_now_ts, nullable=False)

    __table_args__ = (
        CheckConstraint("payment_mode IN ('each_pays_own', 'host_pays_all')", name="ck_pay_sess_mode_valid"),
        CheckConstraint("window_seconds >= 60 AND window_seconds <= 1800", name="ck_pay_sess_window_range"),
        CheckConstraint("frozen_total_paise >= 0", name="ck_pay_sess_total_non_neg"),
    )


class PaymentObligationRecord(Base):
    __tablename__ = "payment_obligations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("table_sessions.id", ondelete="CASCADE"), nullable=False
    )
    proposal_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False
    )
    participant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    obligation_kind: Mapped[str] = mapped_column(String(32), default="share", nullable=False)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="unpaid", nullable=False)
    active_payment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, default=_now_ts, nullable=False)

    __table_args__ = (
        Index("idx_pay_obl_session", "session_id"),
    )


class PaymentGatewayDetailRecord(Base):
    __tablename__ = "payment_gateway_details"

    payment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("payments.id", ondelete="CASCADE"), primary_key=True
    )
    obligation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("payment_obligations.id", ondelete="CASCADE"), nullable=False
    )
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    gateway_payment_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    upi_intent_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    upi_qr_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    captured_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[int] = mapped_column(Integer, default=_now_ts, nullable=False)


class WebhookEventRecord(Base):
    __tablename__ = "webhook_events"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    gateway: Mapped[str] = mapped_column(String(32), default="razorpay", nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    gateway_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gateway_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_body: Mapped[str] = mapped_column(Text, nullable=False)
    signature_verified: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    processing_status: Mapped[str] = mapped_column(String(32), default="processed", nullable=False)
    received_at: Mapped[int] = mapped_column(Integer, default=_now_ts, nullable=False)


class PaymentRefundRecord(Base):
    __tablename__ = "payment_refunds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("table_sessions.id", ondelete="CASCADE"), nullable=False
    )
    payment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("payments.id", ondelete="CASCADE"), nullable=False
    )
    participant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    gateway_refund_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="created", nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, default=_now_ts, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, default=_now_ts, nullable=False)


class KitchenOrderRecord(Base):
    __tablename__ = "kitchen_orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("table_sessions.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    proposal_id: Mapped[str] = mapped_column(String(36), nullable=False)
    total_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    order_type: Mapped[str] = mapped_column(String(32), default="dine_in", nullable=False)
    kitchen_status: Mapped[str] = mapped_column(String(32), default="pending_acceptance", nullable=False)
    eta_minutes: Mapped[int] = mapped_column(Integer, default=25, nullable=False)
    placed_at: Mapped[int] = mapped_column(Integer, nullable=False)
    accepted_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    items_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    support_ticket_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class HostSettlementRecord(Base):
    __tablename__ = "host_settlements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("table_sessions.id", ondelete="CASCADE"), nullable=False
    )
    proposal_id: Mapped[str] = mapped_column(String(36), nullable=False)
    participant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    host_participant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="owes_host", nullable=False)
    informational_only: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    upi_intent_uri: Mapped[str] = mapped_column(Text, nullable=False)
    upi_qr_payload: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, default=_now_ts, nullable=False)


class PaymentReconciliationRecord(Base):
    __tablename__ = "payment_reconciliations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("table_sessions.id", ondelete="CASCADE"), nullable=False
    )
    payment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("payments.id", ondelete="CASCADE"), nullable=False
    )
    previous_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reconciled_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reconciled_at: Mapped[int] = mapped_column(Integer, default=_now_ts, nullable=False)

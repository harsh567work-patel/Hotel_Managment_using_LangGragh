"""
table_split.services.payment_service — Stage 9 Per-Person Payment Orchestrator.

Coordinates:
- Locking & freezing participant allocations with Stage 7 PricingEngine price re-check
- One gateway order per participant (`each_pays_own`) or single Host order (`host_pays_all`)
- Signature verification for checkout callbacks and raw-body HMAC webhooks
- Replay & duplicate-webhook deduplication, out-of-order monotonic transitions,
  and auto-refund on late capture after cancellation
- Serialized SQLite ACID once-only kitchen order placement (`UNIQUE(session_id)`)
- Expired window 3-option Host recovery (`cover_unpaid`, `drop_unpaid_re_split`, `cancel_and_refund`)
- Automated pre-acceptance refunds vs customer-support handoff after kitchen acceptance
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from config import (
    DELIVERY_BUFFER_MINUTES,
    RESTAURANT_LOCATION,
    TABLE_SPLIT_DB_PATH,
)
from graph.orders import format_remaining, get_remaining_seconds
from graph.state import OrderRecord
from table_split.db.dietary_repo import DEFAULT_MENU_JSON_PATH, _load_menu_json
from table_split.db.engine import get_sqlite_connection
from table_split.db.migrations.runner import migrate_up
from table_split.gateways.simulated import SimulatedRazorpayProvider
from table_split.models import validate_strict_paise
from table_split.payment import (
    DEFAULT_PAYMENT_WINDOW_SECONDS,
    GatewayOrderRequest,
    InvalidSignatureError,
    PaymentError,
    PaymentProvider,
    build_participant_receipt,
    build_upi_intent_uri,
    build_upi_qr_payload,
    derive_pay_idempotency_key,
    recheck_proposal_pricing,
    validate_payment_method,
    validate_payment_window_seconds,
)
from table_split.pricing import (
    DEFAULT_GST_BPS,
    PricingEngine,
    PricingLineInput,
    StubPricingEngine,
)
from table_split.security import hash_token, verify_token
from table_split.sse import SSEHub

_STATUS_RANK = {
    "created": 1,
    "pending": 2,
    "failed": 2,
    "captured": 3,
    "partially_refunded": 4,
    "refunded": 5,
}


class PaymentNotFoundError(PaymentError):
    def __init__(self, msg: str) -> None:
        super().__init__(msg, code="NOT_FOUND")


class PaymentUnauthorizedError(PaymentError):
    def __init__(self, msg: str) -> None:
        super().__init__(msg, code="UNAUTHORIZED")


class PaymentForbiddenError(PaymentError):
    def __init__(self, msg: str) -> None:
        super().__init__(msg, code="FORBIDDEN")


class PaymentConflictError(PaymentError):
    def __init__(self, msg: str, *, code: str = "PAYMENT_CONFLICT", state: dict[str, Any] | None = None) -> None:
        self.state = state
        super().__init__(msg, code=code)


class PaymentValidationError(PaymentError):
    def __init__(self, msg: str, *, code: str = "VALIDATION_ERROR") -> None:
        super().__init__(msg, code=code)


class PaymentWindowExpiredError(PaymentConflictError):
    def __init__(self, msg: str = "Payment window has expired; host resolution required.", state: dict[str, Any] | None = None) -> None:
        super().__init__(msg, code="PAYMENT_WINDOW_EXPIRED", state=state)


class PaymentOrchestrator:
    """ACID transactional Stage 9 Payment Orchestrator backed by SQLite WAL + PaymentProvider."""

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        provider: PaymentProvider | None = None,
        pricing_engine: PricingEngine | None = None,
        sse_hub: SSEHub | None = None,
        menu_path: str | Path | None = None,
        host_upi_vpa: str = "hostspiceroute@upi",
    ) -> None:
        self.db_path = Path(db_path or TABLE_SPLIT_DB_PATH)
        migrate_up(self.db_path)
        self.provider: PaymentProvider = provider or SimulatedRazorpayProvider()
        self.pricing_engine: PricingEngine = pricing_engine or StubPricingEngine()
        self.sse_hub = sse_hub
        self.menu_path = Path(menu_path) if menu_path else DEFAULT_MENU_JSON_PATH
        self.host_upi_vpa = host_upi_vpa

    # ------------------------------------------------------------------
    # SQLite Transaction & Auth Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _begin_write(conn: sqlite3.Connection) -> None:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE;")

    def _publish_events(self, session_id: str, events: Sequence[dict[str, Any]]) -> None:
        if self.sse_hub is None:
            return
        for evt in events:
            self.sse_hub.publish(session_id, evt)

    @staticmethod
    def _append_event(
        conn: sqlite3.Connection,
        *,
        session_id: str,
        session_version: int,
        event_type: str,
        participant_id: str | None,
        payload: Mapping[str, Any],
        now: int,
    ) -> dict[str, Any]:
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM session_events WHERE session_id = ?;",
            (session_id,),
        ).fetchone()
        next_seq = int(seq_row["next_seq"])
        event_id = str(uuid4())
        payload_json = json.dumps(dict(payload), separators=(",", ":"), default=str)
        conn.execute(
            """
            INSERT INTO session_events (
                id, session_id, participant_id, type, payload, seq, session_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (event_id, session_id, participant_id, event_type, payload_json, next_seq, session_version, now),
        )
        return {
            "event_id": event_id,
            "seq": next_seq,
            "session_id": session_id,
            "session_version": session_version,
            "participant_id": participant_id,
            "type": event_type,
            "payload": dict(payload),
            "created_at": now,
        }

    @staticmethod
    def _bump_version(
        conn: sqlite3.Connection,
        session_id: str,
        *,
        extra_set_sql: str | None = None,
        extra_params: tuple[Any, ...] = (),
    ) -> int:
        row = conn.execute("SELECT version FROM table_sessions WHERE id = ?;", (session_id,)).fetchone()
        if row is None:
            raise PaymentNotFoundError(f"Session '{session_id}' not found.")
        new_ver = int(row["version"]) + 1
        sql = "UPDATE table_sessions SET version = ?"
        params: list[Any] = [new_ver]
        if extra_set_sql:
            sql += f", {extra_set_sql}"
            params.extend(extra_params)
        sql += " WHERE id = ?;"
        params.append(session_id)
        conn.execute(sql, tuple(params))
        return new_ver

    @staticmethod
    def _load_session_row(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM table_sessions WHERE id = ?;", (session_id,)).fetchone()
        if row is None:
            raise PaymentNotFoundError(f"Table session '{session_id}' not found.")
        return row

    @staticmethod
    def _authenticate_participant_in_conn(
        conn: sqlite3.Connection,
        session_id: str,
        raw_token: str | None,
        *,
        require_host: bool = False,
    ) -> sqlite3.Row:
        if not raw_token or not raw_token.strip():
            raise PaymentUnauthorizedError("Missing guest token credential.")
        token_hash = hash_token(raw_token.strip())
        row = conn.execute(
            """
            SELECT * FROM participants
            WHERE session_id = ? AND guest_token_hash = ? AND status = 'active'
            LIMIT 1;
            """,
            (session_id, token_hash),
        ).fetchone()
        if row is None or not verify_token(raw_token.strip(), row["guest_token_hash"]):
            raise PaymentUnauthorizedError("Invalid or inactive participant credential for session.")
        if require_host and row["role"] != "host":
            raise PaymentForbiddenError("Only the session host can perform this payment action.")
        return row

    def authenticate_caller(
        self,
        session_id: str,
        raw_token: str | None,
        *,
        require_host: bool = False,
    ) -> dict[str, Any]:
        conn = get_sqlite_connection(self.db_path)
        try:
            _ = self._load_session_row(conn, session_id)
            row = self._authenticate_participant_in_conn(conn, session_id, raw_token, require_host=require_host)
            return dict(row)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Menu Price Lookup & Stage 7 Price Re-check
    # ------------------------------------------------------------------
    def _load_menu_catalog_map(self, conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        json_items = _load_menu_json(self.menu_path)
        ext_rows = conn.execute("SELECT * FROM menu_item_extensions;").fetchall()
        ext_by_id = {str(r["item_id"]): dict(r) for r in ext_rows}

        catalog: dict[str, dict[str, Any]] = {}
        for base in json_items:
            item_id = str(base.get("id", ""))
            if not item_id:
                continue
            ext = ext_by_id.get(item_id, {})
            price_paise = (
                int(ext["price_paise"])
                if ext.get("price_paise") is not None
                else int(Decimal(str(base.get("price", 0))) * 100)
            )
            catalog[item_id] = {
                "dish_id": item_id,
                "dish_name": str(base.get("dish_name", item_id)),
                "category": str(base.get("category", "Main Course")),
                "prep_time_minutes": int(base.get("prep_time_minutes", 20)),
                "price_paise": validate_strict_paise(price_paise),
                "tax_rate_bps": int(ext["tax_rate_bps"]) if ext.get("tax_rate_bps") is not None else DEFAULT_GST_BPS,
                "packaging_paise": int(ext["packaging_paise"]) if ext.get("packaging_paise") is not None else 0,
            }
        return catalog

    @staticmethod
    def _load_active_proposal_in_conn(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        prop = conn.execute(
            """
            SELECT * FROM proposals
            WHERE session_id = ? AND status IN ('active', 'approved')
            ORDER BY rowid DESC
            LIMIT 1;
            """,
            (session_id,),
        ).fetchone()
        if prop is None:
            raise PaymentValidationError("No active proposal found for session.", code="NO_ACTIVE_PROPOSAL")
        return prop

    @staticmethod
    def _derive_delivery_fee_paise(proposal_row: sqlite3.Row) -> int:
        try:
            expl = json.loads(proposal_row["explanation_json"] or "{}")
            pricing = expl.get("pricing") or {}
            fee = pricing.get("delivery_fee_paise", 0)
            return validate_strict_paise(fee)
        except (ValueError, TypeError, json.JSONDecodeError):
            return 0

    def _build_pricing_lines_from_proposal(
        self,
        conn: sqlite3.Connection,
        proposal_id: str,
        catalog: Mapping[str, Mapping[str, Any]],
    ) -> list[PricingLineInput]:
        line_rows = conn.execute(
            "SELECT * FROM proposal_lines WHERE proposal_id = ? ORDER BY rowid ASC;",
            (proposal_id,),
        ).fetchall()
        out: list[PricingLineInput] = []
        for lr in line_rows:
            mid = str(lr["menu_item_id"])
            cat_info = catalog.get(mid, {})
            unit_p = cat_info["price_paise"] if "price_paise" in cat_info else int(lr["unit_price_paise"])
            bps = cat_info.get("tax_rate_bps", DEFAULT_GST_BPS)
            pkg = cat_info.get("packaging_paise", 0)
            consumers = json.loads(lr["consumer_participant_ids"] or "[]")
            out.append(
                PricingLineInput(
                    menu_item_id=mid,
                    dish_name=str(cat_info.get("dish_name", mid)),
                    quantity=int(lr["quantity"]),
                    kind=str(lr["kind"]),  # type: ignore[arg-type]
                    consumer_participant_ids=[str(c) for c in consumers],
                    unit_price_paise=int(unit_p),
                    tax_rate_bps=int(bps),
                    packaging_paise=int(pkg),
                )
            )
        return out

    # ------------------------------------------------------------------
    # State Snapshot Builder
    # ------------------------------------------------------------------
    def _build_payment_state_in_conn(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        *,
        now: int | None = None,
    ) -> dict[str, Any]:
        ts = int(time.time()) if now is None else int(now)
        sess = self._load_session_row(conn, session_id)
        pay_sess = conn.execute("SELECT * FROM payment_sessions WHERE session_id = ?;", (session_id,)).fetchone()

        part_rows = conn.execute(
            "SELECT id, display_name, role, status FROM participants WHERE session_id = ? ORDER BY joined_at ASC, rowid ASC;",
            (session_id,),
        ).fetchall()
        name_map = {str(r["id"]): str(r["display_name"]) for r in part_rows}

        obligations_rows = conn.execute(
            """
            SELECT * FROM payment_obligations
            WHERE session_id = ? AND status != 'superseded'
            ORDER BY rowid ASC;
            """,
            (session_id,),
        ).fetchall()

        payments_rows = conn.execute(
            """
            SELECT p.*, pg.obligation_id, pg.method, pg.gateway_payment_id,
                   pg.upi_intent_uri, pg.upi_qr_payload, pg.failure_reason, pg.captured_at
            FROM payments p
            LEFT JOIN payment_gateway_details pg ON pg.payment_id = p.id
            WHERE p.session_id = ?
            ORDER BY p.created_at ASC, p.rowid ASC;
            """,
            (session_id,),
        ).fetchall()

        refund_rows = conn.execute(
            "SELECT * FROM payment_refunds WHERE session_id = ? ORDER BY created_at ASC, rowid ASC;",
            (session_id,),
        ).fetchall()

        settlement_rows = conn.execute(
            "SELECT * FROM host_settlements WHERE session_id = ? ORDER BY rowid ASC;",
            (session_id,),
        ).fetchall()

        ko_row = conn.execute("SELECT * FROM kitchen_orders WHERE session_id = ?;", (session_id,)).fetchone()

        def _ui_label(raw_status: str) -> str:
            if raw_status == "captured":
                return "Paid"
            if raw_status == "failed":
                return "Failed"
            if raw_status in ("refunded", "partially_refunded"):
                return "Refunded"
            return "Pending"

        obligations_out: list[dict[str, Any]] = []
        captured_total_paise = 0
        unpaid_total_paise = 0
        for ob in obligations_rows:
            amt = int(ob["amount_paise"])
            st = str(ob["status"])
            if st == "captured":
                captured_total_paise += amt
            elif st in ("unpaid", "pending", "failed"):
                unpaid_total_paise += amt
            obligations_out.append({
                "obligation_id": str(ob["id"]),
                "participant_id": str(ob["participant_id"]),
                "display_name": name_map.get(str(ob["participant_id"]), str(ob["participant_id"])),
                "obligation_kind": str(ob["obligation_kind"]),
                "amount_paise": amt,
                "status": st,
                "display_status": _ui_label(st),
                "active_payment_id": ob["active_payment_id"],
                "attempt_count": int(ob["attempt_count"]),
            })

        payments_out: list[dict[str, Any]] = []
        for pr in payments_rows:
            payments_out.append({
                "payment_id": str(pr["id"]),
                "obligation_id": pr["obligation_id"],
                "participant_id": str(pr["participant_id"]),
                "display_name": name_map.get(str(pr["participant_id"]), str(pr["participant_id"])),
                "gateway": str(pr["gateway"]),
                "gateway_order_id": str(pr["gateway_order_id"]),
                "gateway_payment_id": pr["gateway_payment_id"],
                "amount_paise": int(pr["amount_paise"]),
                "status": str(pr["status"]),
                "display_status": _ui_label(str(pr["status"])),
                "method": pr["method"],
                "upi_intent_uri": pr["upi_intent_uri"],
                "upi_qr_payload": pr["upi_qr_payload"],
                "failure_reason": pr["failure_reason"],
                "idempotency_key": str(pr["idempotency_key"]),
                "created_at": int(pr["created_at"]),
                "captured_at": pr["captured_at"],
            })

        refunded_total_paise = sum(int(r["amount_paise"]) for r in refund_rows if r["status"] != "failed")
        refunds_out = [
            {
                "refund_id": str(r["id"]),
                "payment_id": str(r["payment_id"]),
                "participant_id": str(r["participant_id"]),
                "display_name": name_map.get(str(r["participant_id"]), str(r["participant_id"])),
                "gateway_refund_id": str(r["gateway_refund_id"]),
                "amount_paise": int(r["amount_paise"]),
                "reason": str(r["reason"]),
                "status": str(r["status"]),
                "created_at": int(r["created_at"]),
            }
            for r in refund_rows
        ]

        settlements_out = [
            {
                "settlement_id": str(sr["id"]),
                "participant_id": str(sr["participant_id"]),
                "display_name": name_map.get(str(sr["participant_id"]), str(sr["participant_id"])),
                "host_participant_id": str(sr["host_participant_id"]),
                "host_name": name_map.get(str(sr["host_participant_id"]), str(sr["host_participant_id"])),
                "amount_paise": int(sr["amount_paise"]),
                "status": str(sr["status"]),
                "label": "Owes host" if sr["status"] == "owes_host" else "Settled (informational)",
                "informational_only": True,
                "notice": "Informational peer settlement link only; platform does not guarantee peer transfers.",
                "upi_intent_uri": str(sr["upi_intent_uri"]),
                "upi_qr_payload": str(sr["upi_qr_payload"]),
            }
            for sr in settlement_rows
        ]

        window_expired = False
        seconds_remaining = 0
        if pay_sess is not None:
            exp_at = int(pay_sess["expires_at"])
            seconds_remaining = max(0, exp_at - ts)
            window_expired = str(sess["status"]) == "paying" and ts >= exp_at and unpaid_total_paise > 0

        kitchen_order_out: dict[str, Any] | None = None
        if ko_row is not None:
            order_items = json.loads(ko_row["items_json"] or "[]")
            order_rec: OrderRecord = {
                "order_id": str(ko_row["id"]),
                "items": order_items,
                "total_price": int(ko_row["total_paise"]) / 100.0,
                "order_type": str(ko_row["order_type"]),
                "eta_minutes": int(ko_row["eta_minutes"]),
                "placed_at": float(ko_row["placed_at"]),
                "status": "served" if ko_row["kitchen_status"] == "served" else "cooking",
            }
            rem_sec = 0 if ko_row["kitchen_status"] in ("served", "cancelled") else get_remaining_seconds(order_rec, now=float(ts))
            kitchen_order_out = {
                "order_id": str(ko_row["id"]),
                "session_id": str(ko_row["session_id"]),
                "proposal_id": str(ko_row["proposal_id"]),
                "total_paise": int(ko_row["total_paise"]),
                "kitchen_status": str(ko_row["kitchen_status"]),
                "eta_minutes": int(ko_row["eta_minutes"]),
                "remaining_seconds": rem_sec,
                "remaining_formatted": format_remaining(rem_sec),
                "placed_at": int(ko_row["placed_at"]),
                "accepted_at": ko_row["accepted_at"],
                "items": order_items,
                "support_ticket_id": ko_row["support_ticket_id"],
            }

        available_host_resolutions = (
            ["cover_unpaid", "drop_unpaid_re_split", "cancel_and_refund"] if window_expired else []
        )

        return {
            "session_id": str(sess["id"]),
            "session_status": str(sess["status"]),
            "version": int(sess["version"]),
            "payment_mode": str(pay_sess["payment_mode"]) if pay_sess else str(sess["payment_mode"]),
            "host_participant_id": sess["host_participant_id"],
            "frozen_total_paise": int(pay_sess["frozen_total_paise"]) if pay_sess else 0,
            "captured_total_paise": captured_total_paise,
            "unpaid_total_paise": unpaid_total_paise,
            "refunded_total_paise": refunded_total_paise,
            "window_seconds": int(pay_sess["window_seconds"]) if pay_sess else DEFAULT_PAYMENT_WINDOW_SECONDS,
            "started_at": int(pay_sess["started_at"]) if pay_sess else None,
            "expires_at": int(pay_sess["expires_at"]) if pay_sess else None,
            "seconds_remaining": seconds_remaining,
            "window_expired": window_expired,
            "host_resolution": pay_sess["host_resolution"] if pay_sess else None,
            "available_host_resolutions": available_host_resolutions,
            "obligations": obligations_out,
            "payments": payments_out,
            "refunds": refunds_out,
            "host_settlements": settlements_out,
            "kitchen_order": kitchen_order_out,
        }

    def get_payment_state(self, session_id: str, *, now: int | None = None) -> dict[str, Any]:
        conn = get_sqlite_connection(self.db_path)
        try:
            return self._build_payment_state_in_conn(conn, session_id, now=now)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Step 1: Lock -> Paying with Stage 7 Price Re-check
    # ------------------------------------------------------------------
    def start_payments(
        self,
        session_id: str,
        *,
        host_participant_id: str,
        payment_mode: str | None = None,
        window_seconds: int | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        ts = int(time.time()) if now is None else int(now)
        win_sec = validate_payment_window_seconds(window_seconds)

        conn = get_sqlite_connection(self.db_path)
        events_to_publish: list[dict[str, Any]] = []
        try:
            self._begin_write(conn)
            try:
                sess = self._load_session_row(conn, session_id)
                if sess["host_participant_id"] != host_participant_id:
                    raise PaymentForbiddenError("Only the host can start the payment window.")
                if sess["status"] in ("placed", "cancelled", "expired") or int(sess["expires_at"]) <= ts:
                    raise PaymentConflictError(f"Cannot start payment from session status '{sess['status']}'.")
                if sess["status"] != "locked":
                    raise PaymentConflictError(
                        f"Session must be 'locked' before starting payment (currently '{sess['status']}').",
                        code="SESSION_NOT_LOCKED",
                    )

                mode = (payment_mode or str(sess["payment_mode"]) or "each_pays_own").strip()
                if mode not in ("each_pays_own", "host_pays_all"):
                    raise PaymentValidationError(f"Invalid payment_mode '{mode}'.")

                proposal = self._load_active_proposal_in_conn(conn, session_id)
                proposal_id = str(proposal["id"])
                catalog = self._load_menu_catalog_map(conn)

                # Load active participants in deterministic order
                part_rows = conn.execute(
                    "SELECT id, display_name FROM participants WHERE session_id = ? AND status = 'active' ORDER BY joined_at ASC, rowid ASC;",
                    (session_id,),
                ).fetchall()
                participant_ids = [str(r["id"]) for r in part_rows]
                if not participant_ids:
                    raise PaymentValidationError("No active participants in session.")

                alloc_rows = conn.execute(
                    "SELECT participant_id, total_paise FROM allocations WHERE proposal_id = ?;",
                    (proposal_id,),
                ).fetchall()
                stored_allocs = {str(r["participant_id"]): int(r["total_paise"]) for r in alloc_rows}
                expected_total = sum(stored_allocs.values())

                # Verify that every consumer referenced in lines is active cleanly
                pricing_lines = self._build_pricing_lines_from_proposal(conn, proposal_id, catalog)
                delivery_fee = self._derive_delivery_fee_paise(proposal)

                # Stage 7 PricingEngine price re-check before freezing obligations
                rechecked = recheck_proposal_pricing(
                    lines=pricing_lines,
                    participant_ids=participant_ids,
                    expected_grand_total_paise=expected_total if alloc_rows else None,
                    expected_allocations_by_pid=stored_allocs if alloc_rows else None,
                    delivery_fee_paise=delivery_fee,
                    pricing_engine=self.pricing_engine,
                )

                digest_src = json.dumps(
                    {
                        "proposal_id": proposal_id,
                        "pricing_version": rechecked.pricing_version,
                        "grand_total_paise": rechecked.grand_total_paise,
                        "allocations": {k: v.total_paise for k, v in sorted(rechecked.allocations.items())},
                    },
                    sort_keys=True,
                )
                digest = hashlib.sha256(digest_src.encode("utf-8")).hexdigest()

                expires_at = min(ts + win_sec, int(sess["expires_at"]))
                conn.execute(
                    """
                    INSERT INTO payment_sessions (
                        session_id, proposal_id, payment_mode, window_seconds,
                        started_at, expires_at, frozen_total_paise, price_recheck_digest,
                        host_resolution, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        proposal_id = excluded.proposal_id,
                        payment_mode = excluded.payment_mode,
                        window_seconds = excluded.window_seconds,
                        started_at = excluded.started_at,
                        expires_at = excluded.expires_at,
                        frozen_total_paise = excluded.frozen_total_paise,
                        price_recheck_digest = excluded.price_recheck_digest,
                        host_resolution = NULL,
                        updated_at = excluded.updated_at;
                    """,
                    (session_id, proposal_id, mode, win_sec, ts, expires_at, rechecked.grand_total_paise, digest, ts),
                )

                # Create frozen obligations cleanly (preserve any carried-forward captured rows if re-split!)
                if mode == "each_pays_own":
                    for pid in participant_ids:
                        share = rechecked.allocations[pid].total_paise
                        if share <= 0:
                            continue
                        existing_ob = conn.execute(
                            """
                            SELECT id, status FROM payment_obligations
                            WHERE session_id = ? AND proposal_id = ? AND participant_id = ? AND obligation_kind = 'share';
                            """,
                            (session_id, proposal_id, pid),
                        ).fetchone()
                        if existing_ob is None:
                            conn.execute(
                                """
                                INSERT INTO payment_obligations (
                                    id, session_id, proposal_id, participant_id, obligation_kind,
                                    amount_paise, status, active_payment_id, attempt_count, updated_at
                                ) VALUES (?, ?, ?, ?, 'share', ?, 'unpaid', NULL, 0, ?);
                                """,
                                (str(uuid4()), session_id, proposal_id, pid, share, ts),
                            )
                else:
                    # host_pays_all mode: single host gateway obligation + informational peer settlement URIs
                    existing_host_ob = conn.execute(
                        """
                        SELECT id FROM payment_obligations
                        WHERE session_id = ? AND proposal_id = ? AND participant_id = ? AND obligation_kind = 'host_all';
                        """,
                        (session_id, proposal_id, host_participant_id),
                    ).fetchone()
                    if existing_host_ob is None and rechecked.grand_total_paise > 0:
                        conn.execute(
                            """
                            INSERT INTO payment_obligations (
                                id, session_id, proposal_id, participant_id, obligation_kind,
                                amount_paise, status, active_payment_id, attempt_count, updated_at
                            ) VALUES (?, ?, ?, ?, 'host_all', ?, 'unpaid', NULL, 0, ?);
                            """,
                            (str(uuid4()), session_id, proposal_id, host_participant_id, rechecked.grand_total_paise, ts),
                        )

                    host_name_row = next((r["display_name"] for r in part_rows if str(r["id"]) == host_participant_id), "Host")
                    for pid in participant_ids:
                        if pid == host_participant_id:
                            continue
                        share = rechecked.allocations[pid].total_paise
                        if share <= 0:
                            continue
                        txn_ref = f"iou_{session_id.replace('-', '')[:8]}_{pid.replace('-', '')[:6]}"
                        uri = build_upi_intent_uri(
                            payee_vpa=self.host_upi_vpa,
                            payee_name=str(host_name_row),
                            amount_paise=share,
                            transaction_ref=txn_ref,
                            note="SpiceRoute Table Share Settlement",
                        )
                        qr = build_upi_qr_payload(
                            payee_vpa=self.host_upi_vpa,
                            payee_name=str(host_name_row),
                            amount_paise=share,
                            transaction_ref=txn_ref,
                            note="SpiceRoute Table Share Settlement",
                        )
                        conn.execute(
                            """
                            INSERT INTO host_settlements (
                                id, session_id, proposal_id, participant_id, host_participant_id,
                                amount_paise, status, informational_only, upi_intent_uri, upi_qr_payload, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, 'owes_host', 1, ?, ?, ?)
                            ON CONFLICT(session_id, proposal_id, participant_id) DO UPDATE SET
                                amount_paise = excluded.amount_paise,
                                upi_intent_uri = excluded.upi_intent_uri,
                                upi_qr_payload = excluded.upi_qr_payload,
                                updated_at = excluded.updated_at;
                            """,
                            (str(uuid4()), session_id, proposal_id, pid, host_participant_id, share, uri, qr, ts),
                        )

                new_ver = self._bump_version(
                    conn,
                    session_id,
                    extra_set_sql="status = 'paying', payment_mode = ?",
                    extra_params=(mode,),
                )
                events_to_publish.append(
                    self._append_event(
                        conn,
                        session_id=session_id,
                        session_version=new_ver,
                        event_type="payment.window_started",
                        participant_id=host_participant_id,
                        payload={
                            "payment_mode": mode,
                            "window_seconds": win_sec,
                            "expires_at": expires_at,
                            "frozen_total_paise": rechecked.grand_total_paise,
                        },
                        now=ts,
                    )
                )

                # If carry-forward captures already fulfilled 100%, auto-place cleanly
                place_evt = self._maybe_place_kitchen_order_in_conn(conn, session_id=session_id, ts=ts)
                if place_evt is not None:
                    events_to_publish.append(place_evt)

                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            state = self._build_payment_state_in_conn(conn, session_id, now=ts)
        finally:
            conn.close()

        self._publish_events(session_id, events_to_publish)
        return state

    # ------------------------------------------------------------------
    # Step 2: Explicit Per-Participant Pay Initiation (No Auto-Charge)
    # ------------------------------------------------------------------
    def initiate_payment(
        self,
        session_id: str,
        *,
        participant_id: str,
        method: str = "upi_qr",
        idempotency_key: str | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        ts = int(time.time()) if now is None else int(now)
        norm_method = validate_payment_method(method)

        conn = get_sqlite_connection(self.db_path)
        events_to_publish: list[dict[str, Any]] = []
        created_order: dict[str, Any]
        try:
            self._begin_write(conn)
            try:
                sess = self._load_session_row(conn, session_id)
                if sess["status"] != "paying":
                    raise PaymentConflictError(
                        f"Session must be in 'paying' state to pay (currently '{sess['status']}').",
                        code="NOT_PAYING",
                    )

                pay_sess = conn.execute("SELECT * FROM payment_sessions WHERE session_id = ?;", (session_id,)).fetchone()
                if pay_sess is None:
                    raise PaymentConflictError("Payment window has not been initialized.", code="PAYMENT_NOT_STARTED")
                if ts >= int(pay_sess["expires_at"]):
                    state = self._build_payment_state_in_conn(conn, session_id, now=ts)
                    raise PaymentWindowExpiredError(state=state)

                proposal_id = str(pay_sess["proposal_id"])
                scoped_idem = derive_pay_idempotency_key(
                    session_id=session_id,
                    participant_id=participant_id,
                    proposal_id=proposal_id,
                    client_key=idempotency_key,
                )

                # Double-click idempotency check
                existing_pay = conn.execute(
                    """
                    SELECT p.*, pg.method, pg.upi_intent_uri, pg.upi_qr_payload
                    FROM payments p
                    LEFT JOIN payment_gateway_details pg ON pg.payment_id = p.id
                    WHERE p.idempotency_key = ?;
                    """,
                    (scoped_idem,),
                ).fetchone()
                if existing_pay is not None:
                    conn.execute("COMMIT;")
                    return {
                        "payment_id": str(existing_pay["id"]),
                        "gateway": str(existing_pay["gateway"]),
                        "gateway_order_id": str(existing_pay["gateway_order_id"]),
                        "amount_paise": int(existing_pay["amount_paise"]),
                        "status": str(existing_pay["status"]),
                        "method": str(existing_pay["method"] or norm_method),
                        "upi_intent_uri": existing_pay["upi_intent_uri"],
                        "upi_qr_payload": existing_pay["upi_qr_payload"],
                        "idempotent_replay": True,
                        "payment_state": self._build_payment_state_in_conn(conn, session_id, now=ts),
                    }

                ob = conn.execute(
                    """
                    SELECT * FROM payment_obligations
                    WHERE session_id = ? AND proposal_id = ? AND participant_id = ?
                      AND status != 'superseded'
                    ORDER BY CASE WHEN obligation_kind = 'host_coverage' THEN 0 ELSE 1 END ASC, rowid DESC
                    LIMIT 1;
                    """,
                    (session_id, proposal_id, participant_id),
                ).fetchone()
                if ob is None:
                    raise PaymentForbiddenError(
                        "No payable obligation exists for this caller in the current payment mode."
                    )
                if ob["status"] == "captured":
                    raise PaymentConflictError("Participant share is already captured.", code="ALREADY_PAID")

                # If caller already has a non-failed active gateway order and retries cleanly ONLY when failed or method changed,
                # return active order if still 'created'/'pending' to prevent parallel orphaned gateway orders on same share.
                if ob["status"] in ("pending",) and ob["active_payment_id"]:
                    active_p = conn.execute(
                        """
                        SELECT p.*, pg.method, pg.upi_intent_uri, pg.upi_qr_payload
                        FROM payments p
                        LEFT JOIN payment_gateway_details pg ON pg.payment_id = p.id
                        WHERE p.id = ?;
                        """,
                        (ob["active_payment_id"],),
                    ).fetchone()
                    if active_p is not None and active_p["status"] in ("created", "pending"):
                        conn.execute("COMMIT;")
                        return {
                            "payment_id": str(active_p["id"]),
                            "gateway": str(active_p["gateway"]),
                            "gateway_order_id": str(active_p["gateway_order_id"]),
                            "amount_paise": int(active_p["amount_paise"]),
                            "status": str(active_p["status"]),
                            "method": str(active_p["method"] or norm_method),
                            "upi_intent_uri": active_p["upi_intent_uri"],
                            "upi_qr_payload": active_p["upi_qr_payload"],
                            "idempotent_replay": True,
                            "payment_state": self._build_payment_state_in_conn(conn, session_id, now=ts),
                        }

                amount_paise = validate_strict_paise(int(ob["amount_paise"]))
                next_attempt = int(ob["attempt_count"]) + 1
                receipt = build_participant_receipt(session_id, next_attempt, participant_id)

                gw_order = self.provider.create_order(
                    GatewayOrderRequest(
                        session_id=session_id,
                        participant_id=participant_id,
                        amount_paise=amount_paise,
                        method=norm_method,
                        receipt=receipt,
                        idempotency_key=scoped_idem,
                        notes={"proposal_id": proposal_id, "obligation_id": str(ob["id"])},
                    )
                )

                pay_id = str(uuid4())
                conn.execute(
                    """
                    INSERT INTO payments (
                        id, session_id, participant_id, gateway, gateway_order_id,
                        amount_paise, status, idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?);
                    """,
                    (pay_id, session_id, participant_id, gw_order.gateway, gw_order.gateway_order_id, amount_paise, scoped_idem, ts),
                )
                conn.execute(
                    """
                    INSERT INTO payment_gateway_details (
                        payment_id, obligation_id, method, gateway_payment_id,
                        upi_intent_uri, upi_qr_payload, failure_reason, captured_at, updated_at
                    ) VALUES (?, ?, ?, NULL, ?, ?, NULL, NULL, ?);
                    """,
                    (pay_id, str(ob["id"]), norm_method, gw_order.upi_intent_uri, gw_order.upi_qr_payload, ts),
                )
                conn.execute(
                    """
                    UPDATE payment_obligations
                    SET status = 'pending', active_payment_id = ?, attempt_count = ?, updated_at = ?
                    WHERE id = ?;
                    """,
                    (pay_id, next_attempt, ts, str(ob["id"])),
                )

                new_ver = self._bump_version(conn, session_id)
                events_to_publish.append(
                    self._append_event(
                        conn,
                        session_id=session_id,
                        session_version=new_ver,
                        event_type="payment.initiated",
                        participant_id=participant_id,
                        payload={
                            "payment_id": pay_id,
                            "gateway_order_id": gw_order.gateway_order_id,
                            "amount_paise": amount_paise,
                            "method": norm_method,
                        },
                        now=ts,
                    )
                )
                created_order = {
                    "payment_id": pay_id,
                    "gateway": gw_order.gateway,
                    "gateway_order_id": gw_order.gateway_order_id,
                    "amount_paise": amount_paise,
                    "status": "pending",
                    "method": norm_method,
                    "upi_intent_uri": gw_order.upi_intent_uri,
                    "upi_qr_payload": gw_order.upi_qr_payload,
                    "checkout_options": gw_order.checkout_options,
                    "idempotent_replay": False,
                }
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            created_order["payment_state"] = self._build_payment_state_in_conn(conn, session_id, now=ts)
        finally:
            conn.close()

        self._publish_events(session_id, events_to_publish)
        return created_order

    # ------------------------------------------------------------------
    # Checkout Signature Verification + Capture + Kitchen Placement
    # ------------------------------------------------------------------
    def verify_checkout_payment(
        self,
        session_id: str,
        *,
        participant_id: str,
        gateway_order_id: str,
        gateway_payment_id: str,
        gateway_signature: str,
        now: int | None = None,
    ) -> dict[str, Any]:
        ts = int(time.time()) if now is None else int(now)

        if not self.provider.verify_checkout_signature(
            gateway_order_id=gateway_order_id,
            gateway_payment_id=gateway_payment_id,
            gateway_signature=gateway_signature,
        ):
            raise InvalidSignatureError("Checkout payment signature verification failed.")

        # Fetch canonical payment record from gateway to cross-check amount & captured status
        gw_payment = self.provider.fetch_payment(gateway_payment_id)
        if gw_payment.gateway_order_id != gateway_order_id:
            raise InvalidSignatureError("Gateway payment does not match gateway order ID.")
        if gw_payment.status != "captured":
            raise PaymentValidationError(
                f"Gateway payment '{gateway_payment_id}' is not captured (status='{gw_payment.status}').",
                code="PAYMENT_NOT_CAPTURED",
            )

        conn = get_sqlite_connection(self.db_path)
        events_to_publish: list[dict[str, Any]] = []
        try:
            self._begin_write(conn)
            try:
                pay = conn.execute(
                    """
                    SELECT p.*, pg.obligation_id
                    FROM payments p
                    JOIN payment_gateway_details pg ON pg.payment_id = p.id
                    WHERE p.session_id = ? AND p.gateway_order_id = ?;
                    """,
                    (session_id, gateway_order_id),
                ).fetchone()
                if pay is None:
                    raise PaymentNotFoundError(f"Payment order '{gateway_order_id}' not found in session '{session_id}'.")
                if str(pay["participant_id"]) != participant_id:
                    raise PaymentForbiddenError("Payment order belongs to another participant.")
                if int(gw_payment.amount_paise) != int(pay["amount_paise"]):
                    raise PaymentValidationError(
                        f"Captured amount mismatch: expected {pay['amount_paise']}p, got {gw_payment.amount_paise}p.",
                        code="AMOUNT_MISMATCH",
                    )

                cap_evts = self._apply_capture_in_conn(
                    conn,
                    session_id=session_id,
                    payment_row=pay,
                    gateway_payment_id=gateway_payment_id,
                    method=gw_payment.method,
                    ts=ts,
                )
                events_to_publish.extend(cap_evts)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            state = self._build_payment_state_in_conn(conn, session_id, now=ts)
        finally:
            conn.close()

        self._publish_events(session_id, events_to_publish)
        return state

    def _apply_capture_in_conn(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        payment_row: sqlite3.Row,
        gateway_payment_id: str,
        method: str | None,
        ts: int,
    ) -> list[dict[str, Any]]:
        """Transitions payment & obligation to captured safely (double-callback & late-cancel aware)."""
        events: list[dict[str, Any]] = []
        sess = self._load_session_row(conn, session_id)
        pay_id = str(payment_row["id"])
        ob_id = str(payment_row["obligation_id"])
        amt = int(payment_row["amount_paise"])
        pid = str(payment_row["participant_id"])

        # Double-callback / idempotency guard
        if str(payment_row["status"]) == "captured":
            place_evt = self._maybe_place_kitchen_order_in_conn(conn, session_id=session_id, ts=ts)
            if place_evt is not None:
                events.append(place_evt)
            return events

        # Late capture after cancellation/refund -> immediately refund captured payment & never place order!
        if str(sess["status"]) == "cancelled":
            conn.execute("UPDATE payments SET status = 'captured' WHERE id = ?;", (pay_id,))
            conn.execute(
                """
                UPDATE payment_gateway_details
                SET gateway_payment_id = COALESCE(gateway_payment_id, ?), captured_at = COALESCE(captured_at, ?), updated_at = ?
                WHERE payment_id = ?;
                """,
                (gateway_payment_id, ts, ts, pay_id),
            )
            refund_evt = self._issue_refund_for_payment_in_conn(
                conn,
                session_id=session_id,
                payment_id=pay_id,
                participant_id=pid,
                gateway_payment_id=gateway_payment_id,
                amount_paise=amt,
                reason="late_capture_after_cancel",
                ts=ts,
            )
            if refund_evt is not None:
                events.append(refund_evt)
            return events

        conn.execute("UPDATE payments SET status = 'captured' WHERE id = ?;", (pay_id,))
        conn.execute(
            """
            UPDATE payment_gateway_details
            SET gateway_payment_id = COALESCE(gateway_payment_id, ?),
                method = COALESCE(?, method),
                failure_reason = NULL,
                captured_at = COALESCE(captured_at, ?),
                updated_at = ?
            WHERE payment_id = ?;
            """,
            (gateway_payment_id, method, ts, ts, pay_id),
        )
        conn.execute(
            """
            UPDATE payment_obligations
            SET status = 'captured', active_payment_id = ?, updated_at = ?
            WHERE id = ? AND status != 'superseded';
            """,
            (pay_id, ts, ob_id),
        )
        new_ver = self._bump_version(conn, session_id)
        events.append(
            self._append_event(
                conn,
                session_id=session_id,
                session_version=new_ver,
                event_type="payment.captured",
                participant_id=pid,
                payload={
                    "payment_id": pay_id,
                    "gateway_payment_id": gateway_payment_id,
                    "amount_paise": amt,
                },
                now=ts,
            )
        )

        place_evt = self._maybe_place_kitchen_order_in_conn(conn, session_id=session_id, ts=ts)
        if place_evt is not None:
            events.append(place_evt)
        return events

    def _maybe_place_kitchen_order_in_conn(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        ts: int,
    ) -> dict[str, Any] | None:
        """
        Places the kitchen order atomically AT MOST ONCE inside the active BEGIN IMMEDIATE lock.
        Requires:
        - session.status == 'paying'
        - existing kitchen_orders row absent
        - ALL non-superseded obligations for active proposal captured AND exact paise sum match
        """
        sess = self._load_session_row(conn, session_id)
        if str(sess["status"]) != "paying":
            return None

        existing_ko = conn.execute("SELECT id FROM kitchen_orders WHERE session_id = ?;", (session_id,)).fetchone()
        if existing_ko is not None:
            return None

        pay_sess = conn.execute("SELECT * FROM payment_sessions WHERE session_id = ?;", (session_id,)).fetchone()
        if pay_sess is None:
            return None

        ob_rows = conn.execute(
            """
            SELECT status, amount_paise FROM payment_obligations
            WHERE session_id = ? AND proposal_id = ? AND status != 'superseded';
            """,
            (session_id, str(pay_sess["proposal_id"])),
        ).fetchall()
        if not ob_rows:
            return None

        if any(str(r["status"]) != "captured" for r in ob_rows):
            return None

        captured_sum = sum(int(r["amount_paise"]) for r in ob_rows)
        frozen_total = int(pay_sess["frozen_total_paise"])
        if captured_sum != frozen_total:
            return None

        # Transition table_sessions strictly from 'paying' -> 'placed' (guarded WHERE clause)
        cur = conn.execute(
            """
            UPDATE table_sessions
            SET status = 'placed', version = version + 1
            WHERE id = ? AND status = 'paying';
            """,
            (session_id,),
        )
        if cur.rowcount == 0:
            return None

        catalog = self._load_menu_catalog_map(conn)
        proposal_id = str(pay_sess["proposal_id"])
        line_rows = conn.execute(
            "SELECT * FROM proposal_lines WHERE proposal_id = ? ORDER BY rowid ASC;",
            (proposal_id,),
        ).fetchall()

        order_items: list[dict[str, Any]] = []
        max_prep = 15
        for lr in line_rows:
            mid = str(lr["menu_item_id"])
            info = catalog.get(mid, {})
            prep = int(info.get("prep_time_minutes", 20))
            max_prep = max(max_prep, prep)
            order_items.append({
                "dish_id": mid,
                "dish_name": str(info.get("dish_name", mid)),
                "quantity": int(lr["quantity"]),
                "unit_price": int(lr["unit_price_paise"]) / 100.0,
                "unit_price_paise": int(lr["unit_price_paise"]),
                "prep_time_minutes": prep,
                "category": str(info.get("category", "Main Course")),
            })

        order_type = "delivery" if sess["delivery_address_id"] else "dine_in"
        eta = max_prep + (DELIVERY_BUFFER_MINUTES if order_type == "delivery" else 0)
        order_id = f"ORD-{uuid4().hex[:8].upper()}"

        conn.execute(
            """
            INSERT INTO kitchen_orders (
                id, session_id, proposal_id, total_paise, order_type,
                kitchen_status, eta_minutes, placed_at, accepted_at, items_json, support_ticket_id
            ) VALUES (?, ?, ?, ?, ?, 'pending_acceptance', ?, ?, NULL, ?, NULL);
            """,
            (order_id, session_id, proposal_id, frozen_total, order_type, eta, ts, json.dumps(order_items)),
        )

        ver_row = conn.execute("SELECT version FROM table_sessions WHERE id = ?;", (session_id,)).fetchone()
        new_ver = int(ver_row["version"])
        return self._append_event(
            conn,
            session_id=session_id,
            session_version=new_ver,
            event_type="order.placed",
            participant_id=None,
            payload={
                "order_id": order_id,
                "total_paise": frozen_total,
                "eta_minutes": eta,
                "order_type": order_type,
                "kitchen_status": "pending_acceptance",
            },
            now=ts,
        )

    # ------------------------------------------------------------------
    # Raw Webhook Handling (Signature, Deduplication, Out-of-Order, Late)
    # ------------------------------------------------------------------
    def handle_webhook(
        self,
        *,
        raw_body: bytes,
        signature: str,
        event_id_header: str | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        ts = int(time.time()) if now is None else int(now)
        if not self.provider.verify_webhook_signature(raw_body=raw_body, signature=signature):
            raise InvalidSignatureError("Invalid webhook HMAC-SHA256 signature.")

        ev = self.provider.parse_webhook_event(raw_body=raw_body, event_id=event_id_header)
        raw_str = raw_body.decode("utf-8", errors="replace")

        conn = get_sqlite_connection(self.db_path)
        events_to_publish: list[dict[str, Any]] = []
        session_id_hit: str | None = None
        proc_status = "processed"
        duplicate = False

        try:
            self._begin_write(conn)
            try:
                existing_wh = conn.execute(
                    "SELECT event_id, processing_status FROM webhook_events WHERE event_id = ?;",
                    (ev.event_id,),
                ).fetchone()
                if existing_wh is not None:
                    duplicate = True
                    proc_status = str(existing_wh["processing_status"])
                    conn.execute("COMMIT;")
                    return {
                        "status": "duplicate_ignored",
                        "event_id": ev.event_id,
                        "duplicate": True,
                        "processing_status": proc_status,
                    }

                pay: sqlite3.Row | None = None
                if ev.gateway_order_id:
                    pay = conn.execute(
                        """
                        SELECT p.*, pg.obligation_id
                        FROM payments p
                        LEFT JOIN payment_gateway_details pg ON pg.payment_id = p.id
                        WHERE p.gateway_order_id = ?;
                        """,
                        (ev.gateway_order_id,),
                    ).fetchone()
                elif ev.gateway_payment_id:
                    pay = conn.execute(
                        """
                        SELECT p.*, pg.obligation_id
                        FROM payments p
                        JOIN payment_gateway_details pg ON pg.payment_id = p.id
                        WHERE pg.gateway_payment_id = ?;
                        """,
                        (ev.gateway_payment_id,),
                    ).fetchone()

                if pay is not None:
                    session_id_hit = str(pay["session_id"])
                    sess = self._load_session_row(conn, session_id_hit)
                    cur_status = str(pay["status"])

                    if ev.event_type in ("payment.captured", "order.paid"):
                        gw_pid = ev.gateway_payment_id or f"pay_wh_{ev.event_id[:12]}"
                        if ev.amount_paise is not None and int(ev.amount_paise) != int(pay["amount_paise"]):
                            raise PaymentValidationError(
                                f"Webhook amount mismatch for order {pay['gateway_order_id']}.",
                                code="AMOUNT_MISMATCH",
                            )
                        if cur_status in ("captured", "refunded", "partially_refunded"):
                            proc_status = "ignored_stale"
                        else:
                            cap_evts = self._apply_capture_in_conn(
                                conn,
                                session_id=session_id_hit,
                                payment_row=pay,
                                gateway_payment_id=gw_pid,
                                method=ev.method,
                                ts=ts,
                            )
                            events_to_publish.extend(cap_evts)
                            if str(sess["status"]) == "cancelled":
                                proc_status = "late_refunded"

                    elif ev.event_type == "payment.failed":
                        # Monotonic guard: NEVER downgrade already captured/refunded payment!
                        if _STATUS_RANK.get(cur_status, 0) >= _STATUS_RANK["captured"]:
                            proc_status = "ignored_stale"
                        else:
                            conn.execute("UPDATE payments SET status = 'failed' WHERE id = ?;", (str(pay["id"]),))
                            conn.execute(
                                """
                                UPDATE payment_gateway_details
                                SET gateway_payment_id = COALESCE(gateway_payment_id, ?),
                                    failure_reason = ?, updated_at = ?
                                WHERE payment_id = ?;
                                """,
                                (ev.gateway_payment_id, ev.failure_reason or "PAYMENT_FAILED", ts, str(pay["id"])),
                            )
                            if pay["obligation_id"]:
                                conn.execute(
                                    """
                                    UPDATE payment_obligations
                                    SET status = 'failed', updated_at = ?
                                    WHERE id = ? AND status NOT IN ('captured', 'superseded');
                                    """,
                                    (ts, str(pay["obligation_id"])),
                                )
                            new_ver = self._bump_version(conn, session_id_hit)
                            events_to_publish.append(
                                self._append_event(
                                    conn,
                                    session_id=session_id_hit,
                                    session_version=new_ver,
                                    event_type="payment.failed",
                                    participant_id=str(pay["participant_id"]),
                                    payload={
                                        "payment_id": str(pay["id"]),
                                        "reason": ev.failure_reason or "PAYMENT_FAILED",
                                    },
                                    now=ts,
                                )
                            )

                    elif ev.event_type in ("refund.created", "refund.processed", "refund.failed") and ev.gateway_refund_id:
                        existing_rf = conn.execute(
                            "SELECT id, status FROM payment_refunds WHERE gateway_refund_id = ?;",
                            (ev.gateway_refund_id,),
                        ).fetchone()
                        target_st = (
                            "processed"
                            if ev.event_type == "refund.processed"
                            else ("failed" if ev.event_type == "refund.failed" else "created")
                        )
                        if existing_rf is not None:
                            # Monotonic: never downgrade 'processed' to 'created'
                            if str(existing_rf["status"]) == "processed" and target_st == "created":
                                proc_status = "ignored_stale"
                            else:
                                conn.execute(
                                    "UPDATE payment_refunds SET status = ?, updated_at = ? WHERE id = ?;",
                                    (target_st, ts, str(existing_rf["id"])),
                                )

                conn.execute(
                    """
                    INSERT INTO webhook_events (
                        event_id, gateway, event_type, gateway_order_id, gateway_payment_id,
                        raw_body, signature_verified, processing_status, received_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?);
                    """,
                    (
                        ev.event_id,
                        self.provider.gateway_name,
                        ev.event_type,
                        ev.gateway_order_id,
                        ev.gateway_payment_id,
                        raw_str,
                        proc_status,
                        ts,
                    ),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        finally:
            conn.close()

        if session_id_hit and events_to_publish:
            self._publish_events(session_id_hit, events_to_publish)

        return {
            "status": "ok",
            "event_id": ev.event_id,
            "duplicate": duplicate,
            "processing_status": proc_status,
            "session_id": session_id_hit,
        }

    # ------------------------------------------------------------------
    # Refund Helper & Cancel Flows (Before vs After Kitchen Acceptance)
    # ------------------------------------------------------------------
    def _issue_refund_for_payment_in_conn(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        payment_id: str,
        participant_id: str,
        gateway_payment_id: str,
        amount_paise: int,
        reason: str,
        ts: int,
    ) -> dict[str, Any] | None:
        """Issues an idempotent gateway refund for a captured payment and updates the refund ledger."""
        already_refunded = conn.execute(
            """
            SELECT COALESCE(SUM(amount_paise), 0) AS total
            FROM payment_refunds
            WHERE payment_id = ? AND status != 'failed';
            """,
            (payment_id,),
        ).fetchone()
        ref_sum = int(already_refunded["total"]) if already_refunded else 0
        remaining_refundable = max(0, amount_paise - ref_sum)
        if remaining_refundable <= 0:
            return None

        receipt = f"rf_{session_id.replace('-', '')[:8]}_{payment_id.replace('-', '')[:12]}"[:40]
        gw_ref = self.provider.create_refund(
            gateway_payment_id=gateway_payment_id,
            amount_paise=remaining_refundable,
            receipt=receipt,
            notes={"session_id": session_id, "participant_id": participant_id, "reason": reason},
        )
        rf_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO payment_refunds (
                id, session_id, payment_id, participant_id, gateway_refund_id,
                amount_paise, reason, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                rf_id,
                session_id,
                payment_id,
                participant_id,
                gw_ref.gateway_refund_id,
                gw_ref.amount_paise,
                reason,
                gw_ref.status,
                ts,
                ts,
            ),
        )
        new_pay_status = "refunded" if (ref_sum + gw_ref.amount_paise) >= amount_paise else "partially_refunded"
        conn.execute("UPDATE payments SET status = ? WHERE id = ?;", (new_pay_status, payment_id))
        conn.execute(
            "UPDATE payment_obligations SET status = 'refunded', updated_at = ? WHERE active_payment_id = ? AND status = 'captured';",
            (ts, payment_id),
        )
        new_ver = self._bump_version(conn, session_id)
        return self._append_event(
            conn,
            session_id=session_id,
            session_version=new_ver,
            event_type="payment.refunded" if reason != "late_capture_after_cancel" else "payment.late_capture_refunded",
            participant_id=participant_id,
            payload={
                "refund_id": rf_id,
                "gateway_refund_id": gw_ref.gateway_refund_id,
                "payment_id": payment_id,
                "amount_paise": gw_ref.amount_paise,
                "reason": reason,
            },
            now=ts,
        )

    def _refund_all_captured_in_conn(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        reason: str,
        ts: int,
    ) -> list[dict[str, Any]]:
        captured_rows = conn.execute(
            """
            SELECT p.id, p.participant_id, p.amount_paise, pg.gateway_payment_id
            FROM payments p
            JOIN payment_gateway_details pg ON pg.payment_id = p.id
            WHERE p.session_id = ? AND p.status = 'captured'
            ORDER BY p.created_at ASC, p.rowid ASC;
            """,
            (session_id,),
        ).fetchall()
        evts: list[dict[str, Any]] = []
        for row in captured_rows:
            gw_pid = row["gateway_payment_id"] or f"pay_missing_{str(row['id'])[:8]}"
            evt = self._issue_refund_for_payment_in_conn(
                conn,
                session_id=session_id,
                payment_id=str(row["id"]),
                participant_id=str(row["participant_id"]),
                gateway_payment_id=str(gw_pid),
                amount_paise=int(row["amount_paise"]),
                reason=reason,
                ts=ts,
            )
            if evt is not None:
                evts.append(evt)
        return evts

    def cancel_or_request_refund(
        self,
        session_id: str,
        *,
        actor_participant_id: str,
        reason: str = "host_cancelled",
        now: int | None = None,
    ) -> dict[str, Any]:
        ts = int(time.time()) if now is None else int(now)
        conn = get_sqlite_connection(self.db_path)
        events_to_publish: list[dict[str, Any]] = []
        refund_mode = "automatic"
        support_ticket_id: str | None = None

        try:
            self._begin_write(conn)
            try:
                sess = self._load_session_row(conn, session_id)
                if sess["host_participant_id"] != actor_participant_id:
                    raise PaymentForbiddenError("Only the host can cancel and refund the group order.")
                if sess["status"] in ("cancelled", "expired"):
                    conn.execute("COMMIT;")
                    return {
                        "refund_mode": "already_closed",
                        "support_ticket_id": None,
                        "payment_state": self._build_payment_state_in_conn(conn, session_id, now=ts),
                    }

                ko = conn.execute("SELECT * FROM kitchen_orders WHERE session_id = ?;", (session_id,)).fetchone()
                if ko is not None and str(ko["kitchen_status"]) in ("accepted", "cooking", "served"):
                    # After kitchen acceptance -> existing support path, DO NOT auto-refund on gateway
                    refund_mode = "support_required"
                    support_ticket_id = str(ko["support_ticket_id"] or f"SUP-{uuid4().hex[:8].upper()}")
                    conn.execute(
                        "UPDATE kitchen_orders SET support_ticket_id = ? WHERE id = ?;",
                        (support_ticket_id, str(ko["id"])),
                    )
                    new_ver = self._bump_version(conn, session_id)
                    events_to_publish.append(
                        self._append_event(
                            conn,
                            session_id=session_id,
                            session_version=new_ver,
                            event_type="support.refund_requested",
                            participant_id=actor_participant_id,
                            payload={
                                "order_id": str(ko["id"]),
                                "kitchen_status": str(ko["kitchen_status"]),
                                "support_ticket_id": support_ticket_id,
                                "contact": f"SpiceRoute Customer Support ({RESTAURANT_LOCATION}, +91-9876543210)",
                            },
                            now=ts,
                        )
                    )
                else:
                    # Before kitchen acceptance (or during locked/paying): cancel + auto-refund all captured payments
                    refund_evts = self._refund_all_captured_in_conn(
                        conn, session_id=session_id, reason=reason, ts=ts
                    )
                    events_to_publish.extend(refund_evts)
                    if ko is not None:
                        conn.execute("UPDATE kitchen_orders SET kitchen_status = 'cancelled' WHERE id = ?;", (str(ko["id"]),))
                    conn.execute(
                        "UPDATE payment_obligations SET status = 'superseded', updated_at = ? WHERE session_id = ? AND status IN ('unpaid', 'pending', 'failed');",
                        (ts, session_id),
                    )
                    new_ver = self._bump_version(conn, session_id, extra_set_sql="status = 'cancelled'")
                    events_to_publish.append(
                        self._append_event(
                            conn,
                            session_id=session_id,
                            session_version=new_ver,
                            event_type="session.cancelled",
                            participant_id=actor_participant_id,
                            payload={"reason": reason, "refund_count": len(refund_evts)},
                            now=ts,
                        )
                    )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            state = self._build_payment_state_in_conn(conn, session_id, now=ts)
        finally:
            conn.close()

        self._publish_events(session_id, events_to_publish)
        return {
            "refund_mode": refund_mode,
            "support_ticket_id": support_ticket_id,
            "payment_state": state,
        }

    # ------------------------------------------------------------------
    # Step 6: Expired Window Host Choices (cover_unpaid, drop_unpaid_re_split, cancel_and_refund)
    # ------------------------------------------------------------------
    def resolve_expired_window(
        self,
        session_id: str,
        *,
        host_participant_id: str,
        choice: str,
        extend_window_seconds: int = DEFAULT_PAYMENT_WINDOW_SECONDS,
        now: int | None = None,
    ) -> dict[str, Any]:
        ts = int(time.time()) if now is None else int(now)
        ext_sec = validate_payment_window_seconds(extend_window_seconds)
        if choice not in ("cover_unpaid", "drop_unpaid_re_split", "cancel_and_refund"):
            raise PaymentValidationError(f"Unsupported host resolution choice '{choice}'.")

        if choice == "cancel_and_refund":
            conn = get_sqlite_connection(self.db_path)
            events_to_publish: list[dict[str, Any]] = []
            try:
                self._begin_write(conn)
                try:
                    sess = self._load_session_row(conn, session_id)
                    if sess["host_participant_id"] != host_participant_id:
                        raise PaymentForbiddenError("Only the host can resolve an expired payment window.")
                    if sess["status"] != "paying":
                        raise PaymentConflictError("Session is not in 'paying' status.")
                    pay_sess = conn.execute("SELECT * FROM payment_sessions WHERE session_id = ?;", (session_id,)).fetchone()
                    if pay_sess is None or ts < int(pay_sess["expires_at"]):
                        raise PaymentConflictError("Payment window has not expired yet.", code="WINDOW_NOT_EXPIRED")

                    conn.execute(
                        "UPDATE payment_sessions SET host_resolution = 'cancel_and_refund', updated_at = ? WHERE session_id = ?;",
                        (ts, session_id),
                    )
                    events_to_publish.extend(
                        self._refund_all_captured_in_conn(
                            conn, session_id=session_id, reason="expired_window_cancelled", ts=ts
                        )
                    )
                    conn.execute(
                        "UPDATE payment_obligations SET status = 'superseded', updated_at = ? WHERE session_id = ? AND status IN ('unpaid', 'pending', 'failed');",
                        (ts, session_id),
                    )
                    new_ver = self._bump_version(conn, session_id, extra_set_sql="status = 'cancelled'")
                    events_to_publish.append(
                        self._append_event(
                            conn,
                            session_id=session_id,
                            session_version=new_ver,
                            event_type="session.cancelled",
                            participant_id=host_participant_id,
                            payload={"reason": "expired_window_cancelled"},
                            now=ts,
                        )
                    )
                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise
                state = self._build_payment_state_in_conn(conn, session_id, now=ts)
            finally:
                conn.close()
            self._publish_events(session_id, events_to_publish)
            return state

        conn = get_sqlite_connection(self.db_path)
        events_to_publish = []
        try:
            self._begin_write(conn)
            try:
                sess = self._load_session_row(conn, session_id)
                if sess["host_participant_id"] != host_participant_id:
                    raise PaymentForbiddenError("Only the host can resolve an expired payment window.")
                if sess["status"] != "paying":
                    raise PaymentConflictError("Session is not in 'paying' status.")

                pay_sess = conn.execute("SELECT * FROM payment_sessions WHERE session_id = ?;", (session_id,)).fetchone()
                if pay_sess is None:
                    raise PaymentConflictError("No active payment window record.")
                if ts < int(pay_sess["expires_at"]):
                    raise PaymentConflictError("Payment window has not expired yet.", code="WINDOW_NOT_EXPIRED")

                proposal_id = str(pay_sess["proposal_id"])
                obs = conn.execute(
                    """
                    SELECT * FROM payment_obligations
                    WHERE session_id = ? AND proposal_id = ? AND status != 'superseded'
                    ORDER BY rowid ASC;
                    """,
                    (session_id, proposal_id),
                ).fetchall()
                unpaid_obs = [ob for ob in obs if str(ob["status"]) != "captured"]
                if not unpaid_obs:
                    raise PaymentConflictError("All obligations are already captured.", code="NO_UNPAID_OBLIGATIONS")

                if choice == "cover_unpaid":
                    diff_paise = sum(int(ob["amount_paise"]) for ob in unpaid_obs)
                    if diff_paise <= 0:
                        raise PaymentConflictError("Unpaid difference must be > 0.")

                    for ob in unpaid_obs:
                        conn.execute(
                            "UPDATE payment_obligations SET status = 'superseded', updated_at = ? WHERE id = ?;",
                            (ts, str(ob["id"])),
                        )
                    # Check if host already has an unpaid host_coverage obligation, merge cleanly
                    conn.execute(
                        """
                        INSERT INTO payment_obligations (
                            id, session_id, proposal_id, participant_id, obligation_kind,
                            amount_paise, status, active_payment_id, attempt_count, updated_at
                        ) VALUES (?, ?, ?, ?, 'host_coverage', ?, 'unpaid', NULL, 0, ?);
                        """,
                        (str(uuid4()), session_id, proposal_id, host_participant_id, diff_paise, ts),
                    )
                    new_exp = ts + ext_sec
                    conn.execute(
                        """
                        UPDATE payment_sessions
                        SET host_resolution = 'cover_unpaid', expires_at = ?, updated_at = ?
                        WHERE session_id = ?;
                        """,
                        (new_exp, ts, session_id),
                    )
                    new_ver = self._bump_version(conn, session_id)
                    events_to_publish.append(
                        self._append_event(
                            conn,
                            session_id=session_id,
                            session_version=new_ver,
                            event_type="payment.host_covering_unpaid",
                            participant_id=host_participant_id,
                            payload={"difference_paise": diff_paise, "new_expires_at": new_exp},
                            now=ts,
                        )
                    )

                elif choice == "drop_unpaid_re_split":
                    unpaid_pids = {str(ob["participant_id"]) for ob in unpaid_obs}
                    captured_obs = [ob for ob in obs if str(ob["status"]) == "captured"]
                    paid_pids = [str(ob["participant_id"]) for ob in captured_obs]
                    if not paid_pids:
                        raise PaymentConflictError(
                            "Zero paid participants remain after dropping unpaid diners; choose cancel_and_refund instead.",
                            code="NO_PAID_DINERS_REMAINING",
                        )

                    # Re-build proposal lines excluding unpaid personal items and removing unpaid diners from shared items
                    catalog = self._load_menu_catalog_map(conn)
                    old_prop = conn.execute("SELECT * FROM proposals WHERE id = ?;", (proposal_id,)).fetchone()
                    old_lines = conn.execute(
                        "SELECT * FROM proposal_lines WHERE proposal_id = ? ORDER BY rowid ASC;",
                        (proposal_id,),
                    ).fetchall()

                    new_lines_data: list[dict[str, Any]] = []
                    new_pricing_inputs: list[PricingLineInput] = []
                    for lr in old_lines:
                        consumers = [str(c) for c in json.loads(lr["consumer_participant_ids"] or "[]")]
                        if str(lr["kind"]) == "PERSONAL":
                            kept = [c for c in consumers if c not in unpaid_pids]
                            if not kept:
                                continue
                            new_kind = "PERSONAL"
                        else:
                            kept = [c for c in consumers if c not in unpaid_pids]
                            if not kept:
                                continue
                            new_kind = "SHARED" if len(kept) >= 2 else "PERSONAL"

                        mid = str(lr["menu_item_id"])
                        info = catalog.get(mid, {})
                        unit_p = int(info.get("price_paise", int(lr["unit_price_paise"])))
                        bps = int(info.get("tax_rate_bps", DEFAULT_GST_BPS))
                        pkg = int(info.get("packaging_paise", 0))
                        new_lines_data.append({
                            "menu_item_id": mid,
                            "quantity": int(lr["quantity"]),
                            "kind": new_kind,
                            "consumer_participant_ids": kept,
                            "unit_price_paise": unit_p,
                        })
                        new_pricing_inputs.append(
                            PricingLineInput(
                                menu_item_id=mid,
                                dish_name=str(info.get("dish_name", mid)),
                                quantity=int(lr["quantity"]),
                                kind=new_kind,  # type: ignore[arg-type]
                                consumer_participant_ids=kept,
                                unit_price_paise=unit_p,
                                tax_rate_bps=bps,
                                packaging_paise=pkg,
                            )
                        )

                    if not new_pricing_inputs:
                        raise PaymentConflictError(
                            "No food items remain after removing unpaid items; use cancel_and_refund.",
                            code="EMPTY_REMAINING_ORDER",
                        )

                    delivery_fee = self._derive_delivery_fee_paise(old_prop) if old_prop else 0
                    ordered_paid_pids = sorted(set(paid_pids))
                    repriced = self.pricing_engine.calculate_proposal_pricing(
                        new_pricing_inputs,
                        ordered_paid_pids,
                        delivery_fee_paise=delivery_fee,
                        default_tax_rate_bps=DEFAULT_GST_BPS,
                    )

                    # Mark unpaid participants removed/left for payment & create revised proposal requiring fresh lock
                    for up_pid in unpaid_pids:
                        conn.execute(
                            "UPDATE participants SET status = 'left' WHERE id = ? AND session_id = ?;",
                            (up_pid, session_id),
                        )

                    # Check any confirmed HARD budget_max_paise constraints for remaining participants cleanly
                    budget_rows = conn.execute(
                        """
                        SELECT participant_id, value
                        FROM preferences
                        WHERE participant_id IN ({placeholders})
                          AND kind = 'budget_max_paise'
                          AND strength = 'HARD'
                          AND confirmed_by_user = 1;
                        """.format(placeholders=",".join("?" for _ in ordered_paid_pids)),
                        tuple(ordered_paid_pids),
                    ).fetchall()
                    budget_violations: list[dict[str, Any]] = []
                    affected_budget_pids: list[str] = []
                    for br in budget_rows:
                        bpid = str(br["participant_id"])
                        val = json.loads(br["value"] or "{}")
                        cap = int(val.get("max_paise", 0))
                        if cap > 0 and repriced.allocations[bpid].total_paise > cap:
                            affected_budget_pids.append(bpid)
                            budget_violations.append({
                                "participant_id": bpid,
                                "reason": "RESPLIT_EXCEEDS_BUDGET",
                                "budget_max_paise": cap,
                                "required_paise": repriced.allocations[bpid].total_paise,
                            })

                    # Supersede old proposal and persist new proposal snapshot
                    new_prop_id = str(uuid4())
                    conn.execute("UPDATE proposals SET status = 'superseded' WHERE id = ?;", (proposal_id,))
                    old_expl = json.loads(old_prop["explanation_json"] or "{}") if old_prop else {}
                    old_ver_num = int(old_expl.get("proposal_version", 1))
                    new_expl = {
                        **old_expl,
                        "proposal_id": new_prop_id,
                        "proposal_version": old_ver_num + 1,
                        "status": "OK",
                        "dropped_unpaid_participant_ids": sorted(unpaid_pids),
                        "lines": new_lines_data,
                        "allocations": {
                            pid: repriced.allocations[pid].model_dump() for pid in ordered_paid_pids
                        },
                        "safety_summary": {
                            "all_hard_constraints_met": len(budget_violations) == 0,
                            "verified_by_deterministic_engine": True,
                            "participants_checked": len(ordered_paid_pids),
                            "violations": budget_violations,
                            "affected_participant_ids": sorted(set(affected_budget_pids)),
                            "affected_participant_names": [],
                        },
                        "pricing": {
                            "pricing_version": repriced.pricing_version,
                            "subtotal_paise": repriced.subtotal_paise,
                            "tax_paise": repriced.tax_paise,
                            "packaging_paise": repriced.packaging_paise,
                            "delivery_fee_paise": repriced.delivery_fee_paise,
                            "discount_paise": repriced.discount_paise,
                            "grand_total_paise": repriced.grand_total_paise,
                        },
                    }
                    new_hash = hashlib.sha256(
                        json.dumps({"lines": new_lines_data, "paid": ordered_paid_pids}, sort_keys=True).encode()
                    ).hexdigest()
                    # Check whether migration 005 columns exist gracefully
                    prop_cols = {r[1] for r in conn.execute("PRAGMA table_info(proposals);").fetchall()}
                    sess_ver_next = int(sess["version"]) + 1
                    if "proposal_version" in prop_cols:
                        conn.execute(
                            """
                            INSERT INTO proposals (
                                id, session_id, session_version, proposal_version, engine_version,
                                status, constraints_hash, explanation_json, diff_json, created_at
                            ) VALUES (?, ?, ?, ?, '1.0.0', 'active', ?, ?, '{}', ?);
                            """,
                            (
                                new_prop_id,
                                session_id,
                                sess_ver_next,
                                old_ver_num + 1,
                                new_hash,
                                json.dumps(new_expl),
                                ts,
                            ),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO proposals (
                                id, session_id, session_version, engine_version,
                                status, constraints_hash, explanation_json, created_at
                            ) VALUES (?, ?, ?, '1.0.0', 'active', ?, ?, ?);
                            """,
                            (new_prop_id, session_id, sess_ver_next, new_hash, json.dumps(new_expl), ts),
                        )

                    for ln in new_lines_data:
                        conn.execute(
                            """
                            INSERT INTO proposal_lines (
                                id, proposal_id, menu_item_id, quantity, kind, consumer_participant_ids, unit_price_paise
                            ) VALUES (?, ?, ?, ?, ?, ?, ?);
                            """,
                            (
                                str(uuid4()),
                                new_prop_id,
                                ln["menu_item_id"],
                                ln["quantity"],
                                ln["kind"],
                                json.dumps(ln["consumer_participant_ids"]),
                                ln["unit_price_paise"],
                            ),
                        )

                    for pid in ordered_paid_pids:
                        al = repriced.allocations[pid]
                        conn.execute(
                            """
                            INSERT INTO allocations (
                                id, proposal_id, participant_id, items_paise, tax_paise,
                                delivery_share_paise, packaging_share_paise, discount_share_paise,
                                rounding_paise, total_paise
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                            """,
                            (
                                str(uuid4()),
                                new_prop_id,
                                pid,
                                al.items_paise,
                                al.tax_paise,
                                al.delivery_share_paise,
                                al.packaging_share_paise,
                                al.discount_share_paise,
                                al.rounding_paise,
                                al.total_paise,
                            ),
                        )

                    # Reconcile prior captured payments vs new amounts:
                    # Refund captured payments whose share changed (requiring re-pay of changed amount);
                    # carry unchanged shares cleanly onto the new proposal obligation as captured.
                    for cap_ob in captured_obs:
                        cap_pid = str(cap_ob["participant_id"])
                        old_amt = int(cap_ob["amount_paise"])
                        new_amt = repriced.allocations[cap_pid].total_paise
                        pay_id = cap_ob["active_payment_id"]
                        conn.execute(
                            "UPDATE payment_obligations SET status = 'superseded', updated_at = ? WHERE id = ?;",
                            (ts, str(cap_ob["id"])),
                        )
                        if new_amt == old_amt and pay_id:
                            conn.execute(
                                """
                                INSERT INTO payment_obligations (
                                    id, session_id, proposal_id, participant_id, obligation_kind,
                                    amount_paise, status, active_payment_id, attempt_count, updated_at
                                ) VALUES (?, ?, ?, ?, 'share', ?, 'captured', ?, 1, ?);
                                """,
                                (str(uuid4()), session_id, new_prop_id, cap_pid, new_amt, str(pay_id), ts),
                            )
                        else:
                            if pay_id:
                                p_row = conn.execute(
                                    """
                                    SELECT p.id, p.amount_paise, pg.gateway_payment_id
                                    FROM payments p
                                    JOIN payment_gateway_details pg ON pg.payment_id = p.id
                                    WHERE p.id = ?;
                                    """,
                                    (str(pay_id),),
                                ).fetchone()
                                if p_row is not None:
                                    rf_evt = self._issue_refund_for_payment_in_conn(
                                        conn,
                                        session_id=session_id,
                                        payment_id=str(p_row["id"]),
                                        participant_id=cap_pid,
                                        gateway_payment_id=str(p_row["gateway_payment_id"] or f"pay_{str(p_row['id'])[:8]}"),
                                        amount_paise=int(p_row["amount_paise"]),
                                        reason="resplit_amount_changed",
                                        ts=ts,
                                    )
                                    if rf_evt is not None:
                                        events_to_publish.append(rf_evt)

                    for ob in unpaid_obs:
                        conn.execute(
                            "UPDATE payment_obligations SET status = 'superseded', updated_at = ? WHERE id = ?;",
                            (ts, str(ob["id"])),
                        )

                    conn.execute(
                        "UPDATE payment_sessions SET host_resolution = 'drop_unpaid_re_split', updated_at = ? WHERE session_id = ?;",
                        (ts, session_id),
                    )
                    # Return session to 'proposing' -> requires fresh host lock + payment start for changed amounts!
                    new_ver = self._bump_version(conn, session_id, extra_set_sql="status = 'proposing'")
                    events_to_publish.append(
                        self._append_event(
                            conn,
                            session_id=session_id,
                            session_version=new_ver,
                            event_type="payment.resplit_requires_relock",
                            participant_id=host_participant_id,
                            payload={
                                "new_proposal_id": new_prop_id,
                                "dropped_participant_ids": sorted(unpaid_pids),
                                "new_total_paise": repriced.grand_total_paise,
                            },
                            now=ts,
                        )
                    )

                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            state = self._build_payment_state_in_conn(conn, session_id, now=ts)
        finally:
            conn.close()

        self._publish_events(session_id, events_to_publish)
        return state

    # ------------------------------------------------------------------
    # Host Settlement Marking (Informational Only) & Kitchen Acceptance
    # ------------------------------------------------------------------
    def mark_host_settlement_paid(
        self,
        session_id: str,
        *,
        participant_id: str,
        now: int | None = None,
    ) -> dict[str, Any]:
        ts = int(time.time()) if now is None else int(now)
        conn = get_sqlite_connection(self.db_path)
        events: list[dict[str, Any]] = []
        try:
            self._begin_write(conn)
            try:
                _ = self._load_session_row(conn, session_id)
                cur = conn.execute(
                    """
                    UPDATE host_settlements
                    SET status = 'marked_settled', updated_at = ?
                    WHERE session_id = ? AND participant_id = ?;
                    """,
                    (ts, session_id, participant_id),
                )
                if cur.rowcount == 0:
                    raise PaymentNotFoundError("No host settlement entry found for participant.")
                new_ver = self._bump_version(conn, session_id)
                events.append(
                    self._append_event(
                        conn,
                        session_id=session_id,
                        session_version=new_ver,
                        event_type="settlement.marked_informational",
                        participant_id=participant_id,
                        payload={"informational_only": True},
                        now=ts,
                    )
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            state = self._build_payment_state_in_conn(conn, session_id, now=ts)
        finally:
            conn.close()
        self._publish_events(session_id, events)
        return state

    def accept_kitchen_order(
        self,
        session_id: str,
        *,
        now: int | None = None,
    ) -> dict[str, Any]:
        """Transitions a placed order from pending_acceptance -> accepted (boundary for auto-refund vs support path)."""
        ts = int(time.time()) if now is None else int(now)
        conn = get_sqlite_connection(self.db_path)
        events: list[dict[str, Any]] = []
        try:
            self._begin_write(conn)
            try:
                ko = conn.execute("SELECT * FROM kitchen_orders WHERE session_id = ?;", (session_id,)).fetchone()
                if ko is None:
                    raise PaymentNotFoundError(f"No placed kitchen order for session '{session_id}'.")
                if str(ko["kitchen_status"]) == "cancelled":
                    raise PaymentConflictError("Kitchen order is already cancelled.")
                if str(ko["kitchen_status"]) == "pending_acceptance":
                    conn.execute(
                        "UPDATE kitchen_orders SET kitchen_status = 'accepted', accepted_at = ? WHERE id = ?;",
                        (ts, str(ko["id"])),
                    )
                    new_ver = self._bump_version(conn, session_id)
                    events.append(
                        self._append_event(
                            conn,
                            session_id=session_id,
                            session_version=new_ver,
                            event_type="kitchen.order_accepted",
                            participant_id=None,
                            payload={"order_id": str(ko["id"]), "accepted_at": ts},
                            now=ts,
                        )
                    )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            state = self._build_payment_state_in_conn(conn, session_id, now=ts)
        finally:
            conn.close()
        self._publish_events(session_id, events)
        return state

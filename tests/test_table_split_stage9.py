"""
tests.test_table_split_stage9 — Comprehensive verification for Stage 9 Per-Person Payment Orchestration.

Covers:
- Reversible Migration 006 schema & constraints
- Razorpay httpx REST adapter (Orders, Fetch, Refunds) & NPCI Feb 2026 UPI Collect ban
- Each-pays-own lifecycle, Stage 7 price re-check freeze, explicit Pay gating, single order placement
- Payment failure & retry isolation, idempotency keys, double callbacks
- Expired window Host 3-choice recovery: (a) cover_unpaid, (b) drop_unpaid_re_split, (c) cancel_and_refund
- Webhook security: duplicate event deduplication, out-of-order monotonicity, tampered HMAC, late capture after cancel
- Scheduled reconciliation polling stuck pending payments
- Pre-acceptance auto-refund vs post-acceptance support-path handoff
- Host-pays-all mode with informational UPI Intent/QR settlements
- True multi-threaded concurrency stress ensuring exactly one kitchen order on simultaneous all-paid triggers
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from api import app
from table_split.api_payments import get_payment_orchestrator, get_payment_provider
from table_split.api_sessions import get_rate_limiter, get_session_service, get_sse_hub
from table_split.db.dietary_repo import DEFAULT_MENU_JSON_PATH
from table_split.db.engine import get_sqlite_connection
from table_split.db.migrations.runner import migrate_down, migrate_up
from table_split.gateways.razorpay_adapter import RazorpayPaymentProvider
from table_split.gateways.simulated import SimulatedRazorpayProvider
from table_split.payment import (
    GatewayOrderRequest,
    InvalidSignatureError,
    PriceDriftError,
    UnsupportedPaymentMethodError,
    validate_payment_method,
)
from table_split.pricing import DEFAULT_GST_BPS, PricingLineInput, StubPricingEngine
from table_split.rate_limit import RateLimitRule, SlidingWindowRateLimiter
from table_split.services.payment_reconciler import reconcile_pending_payments
from table_split.services.payment_service import (
    PaymentConflictError,
    PaymentForbiddenError,
    PaymentOrchestrator,
)
from table_split.services.session_service import SessionService
from table_split.sse import SSEHub


def _seed_locked_session(
    db_path: Path,
    *,
    two_diners_only: bool = False,
    delivery_address_id: str | None = None,
) -> dict[str, Any]:
    """Creates a locked session with deterministic Stage 7 allocations ready for payment start."""
    migrate_up(db_path)
    hub = SSEHub()
    svc = SessionService(db_path=db_path, sse_hub=hub)
    created = svc.create_session(
        display_name="Rohan (Host)",
        delivery_address_id=delivery_address_id,
    )
    sid = created["session_id"]
    jtok = created["join_token"]
    hpid = created["host_participant_id"]
    htok = created["host_token"]

    g1 = svc.join_session(sid, display_name="Ananya", join_token=jtok)
    g1_pid, g1_tok = g1["participant_id"], g1["guest_token"]

    participants = [hpid, g1_pid]
    g2_pid, g2_tok = None, None
    if not two_diners_only:
        g2 = svc.join_session(sid, display_name="Amit", join_token=jtok)
        g2_pid, g2_tok = g2["participant_id"], g2["guest_token"]
        participants.append(g2_pid)

    # Create deterministic proposal lines using committed data/menu.json prices:
    # D002 Paneer Tikka Masala (240 INR = 24000p) shared across Rohan & (Amit or Ananya)
    # D004 / Veg dish or D008 Samosa (120 INR = 12000p) personal for Ananya
    # If 3 diners: Amit also gets D013 Masala Chai (80 INR = 8000p) personal
    from table_split.db.dietary_repo import _load_menu_json

    menu_map = {str(r["id"]): r for r in _load_menu_json(DEFAULT_MENU_JSON_PATH)}
    d002_p = int(Decimal(str(menu_map["D002"]["price"])) * 100)
    d008_p = int(Decimal(str(menu_map["D008"]["price"])) * 100)
    d013_p = int(Decimal(str(menu_map["D013"]["price"])) * 100)

    if two_diners_only:
        lines = [
            PricingLineInput(
                menu_item_id="D002",
                dish_name="Paneer Tikka Masala",
                quantity=1,
                kind="SHARED",
                consumer_participant_ids=[hpid, g1_pid],
                unit_price_paise=d002_p,
                tax_rate_bps=DEFAULT_GST_BPS,
            ),
            PricingLineInput(
                menu_item_id="D008",
                dish_name="Samosa",
                quantity=1,
                kind="PERSONAL",
                consumer_participant_ids=[g1_pid],
                unit_price_paise=d008_p,
                tax_rate_bps=DEFAULT_GST_BPS,
            ),
        ]
    else:
        assert g2_pid is not None
        lines = [
            PricingLineInput(
                menu_item_id="D002",
                dish_name="Paneer Tikka Masala",
                quantity=1,
                kind="SHARED",
                consumer_participant_ids=[hpid, g2_pid],
                unit_price_paise=d002_p,
                tax_rate_bps=DEFAULT_GST_BPS,
            ),
            PricingLineInput(
                menu_item_id="D008",
                dish_name="Samosa",
                quantity=1,
                kind="PERSONAL",
                consumer_participant_ids=[g1_pid],
                unit_price_paise=d008_p,
                tax_rate_bps=DEFAULT_GST_BPS,
            ),
            PricingLineInput(
                menu_item_id="D013",
                dish_name="Masala Chai",
                quantity=1,
                kind="PERSONAL",
                consumer_participant_ids=[g2_pid],
                unit_price_paise=d013_p,
                tax_rate_bps=DEFAULT_GST_BPS,
            ),
        ]

    pricing = StubPricingEngine().calculate_proposal_pricing(
        lines,
        sorted(participants),
        delivery_fee_paise=3000 if delivery_address_id else 0,
    )

    conn = get_sqlite_connection(db_path)
    prop_id = str(uuid4())
    now = 1_760_000_000
    with conn:
        ver_row = conn.execute("SELECT version FROM table_sessions WHERE id = ?;", (sid,)).fetchone()
        s_ver = int(ver_row["version"])
        prop_cols = {r[1] for r in conn.execute("PRAGMA table_info(proposals);").fetchall()}
        expl_json = json.dumps({
            "proposal_id": prop_id,
            "proposal_version": 1,
            "status": "OK",
            "pricing": {
                "pricing_version": pricing.pricing_version,
                "subtotal_paise": pricing.subtotal_paise,
                "tax_paise": pricing.tax_paise,
                "packaging_paise": pricing.packaging_paise,
                "delivery_fee_paise": pricing.delivery_fee_paise,
                "discount_paise": pricing.discount_paise,
                "grand_total_paise": pricing.grand_total_paise,
            },
        })
        if "proposal_version" in prop_cols:
            conn.execute(
                """
                INSERT INTO proposals (
                    id, session_id, session_version, proposal_version, engine_version,
                    status, constraints_hash, explanation_json, diff_json, created_at
                ) VALUES (?, ?, ?, 1, '1.0.0', 'active', 'hash_s9', ?, '{}', ?);
                """,
                (prop_id, sid, s_ver, expl_json, now),
            )
        else:
            conn.execute(
                """
                INSERT INTO proposals (
                    id, session_id, session_version, engine_version,
                    status, constraints_hash, explanation_json, created_at
                ) VALUES (?, ?, ?, '1.0.0', 'active', 'hash_s9', ?, ?);
                """,
                (prop_id, sid, s_ver, expl_json, now),
            )

        for ln in lines:
            conn.execute(
                """
                INSERT INTO proposal_lines (
                    id, proposal_id, menu_item_id, quantity, kind, consumer_participant_ids, unit_price_paise
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    str(uuid4()),
                    prop_id,
                    ln.menu_item_id,
                    ln.quantity,
                    ln.kind,
                    json.dumps(ln.consumer_participant_ids),
                    ln.unit_price_paise,
                ),
            )
        for pid, alloc in pricing.allocations.items():
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
                    prop_id,
                    pid,
                    alloc.items_paise,
                    alloc.tax_paise,
                    alloc.delivery_share_paise,
                    alloc.packaging_share_paise,
                    alloc.discount_share_paise,
                    alloc.rounding_paise,
                    alloc.total_paise,
                ),
            )
        conn.execute(
            "UPDATE table_sessions SET status = 'locked', version = version + 1 WHERE id = ?;",
            (sid,),
        )
    conn.close()

    return {
        "session_id": sid,
        "proposal_id": prop_id,
        "host": {"id": hpid, "token": htok},
        "guest1": {"id": g1_pid, "token": g1_tok},
        "guest2": {"id": g2_pid, "token": g2_tok} if g2_pid else None,
        "pricing": pricing,
        "grand_total_paise": pricing.grand_total_paise,
    }


def test_migration_006_up_and_down(tmp_path: Path) -> None:
    db_file = tmp_path / "mig006.db"
    applied = migrate_up(db_file)
    assert "006" in applied

    conn = get_sqlite_connection(db_file)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()}
    conn.close()
    assert {
        "payment_sessions",
        "payment_obligations",
        "payment_gateway_details",
        "webhook_events",
        "payment_refunds",
        "kitchen_orders",
        "host_settlements",
        "payment_reconciliations",
    }.issubset(tables)

    steps = len(applied) - applied.index("006")
    rolled = migrate_down(db_file, steps=steps)
    assert "006" in rolled

    conn2 = get_sqlite_connection(db_file)
    tables_down = {r[0] for r in conn2.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()}
    conn2.close()
    assert "payment_obligations" not in tables_down
    assert "kitchen_orders" not in tables_down


def test_razorpay_docs_compliance_npci_collect_ban_and_httpx_adapter() -> None:
    # UPI Collect deprecated by NPCI effective Feb 28, 2026 -> rejected strictly
    for banned in ("upi_collect", "collect", "VPA_COLLECT"):
        with pytest.raises(UnsupportedPaymentMethodError):
            validate_payment_method(banned)

    for allowed in ("upi_intent", "upi_qr", "card", "netbanking"):
        assert validate_payment_method(allowed) == allowed

    seen_requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        seen_requests.append((request.method, str(request.url), body))
        if request.method == "POST" and request.url.path.endswith("/v1/orders") and body is not None:
            return httpx.Response(200, json={"id": "order_rzp_123", "amount": body["amount"], "currency": "INR", "receipt": body["receipt"], "status": "created"})
        if request.method == "GET" and request.url.path.endswith("/v1/orders/order_rzp_123/payments"):
            return httpx.Response(200, json={"items": [{"id": "pay_rzp_456", "order_id": "order_rzp_123", "amount": 18000, "currency": "INR", "status": "captured", "method": "card"}]})
        if request.method == "POST" and request.url.path.endswith("/v1/payments/pay_rzp_456/refund") and body is not None:
            return httpx.Response(200, json={"id": "rfnd_rzp_789", "payment_id": "pay_rzp_456", "amount": body["amount"], "status": "processed", "receipt": body["receipt"]})
        return httpx.Response(404, json={"error": "not_found"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = RazorpayPaymentProvider(
        key_id="rzp_test_key",
        key_secret="test_secret_123",
        webhook_secret="wh_secret_123",
        http_client=client,
    )

    with pytest.raises(UnsupportedPaymentMethodError):
        validate_payment_method("upi_collect")

    order_res = adapter.create_order(
        GatewayOrderRequest(
            session_id="sess-1",
            participant_id="part-1",
            amount_paise=18000,
            method="upi_intent",
            receipt="sr_sess1_part1_1",
            idempotency_key="idem_1",
        )
    )
    assert order_res.gateway_order_id == "order_rzp_123"
    assert order_res.upi_intent_uri is not None and order_res.upi_intent_uri.startswith("upi://pay?")
    assert "180.00" in order_res.upi_intent_uri
    assert order_res.checkout_options["config"]["display"]["blocks"]["upi"]["instruments"][0]["flows"] == ["intent"]

    fetched = adapter.fetch_order_payments("order_rzp_123")
    assert len(fetched) == 1 and fetched[0].status == "captured" and fetched[0].method == "card"

    ref = adapter.create_refund(gateway_payment_id="pay_rzp_456", amount_paise=18000, receipt="rf_1")
    assert ref.gateway_refund_id == "rfnd_rzp_789" and ref.amount_paise == 18000


def test_stage7_price_recheck_blocks_stale_freeze(tmp_path: Path) -> None:
    db = tmp_path / "drift.db"
    setup = _seed_locked_session(db)
    orch = PaymentOrchestrator(db_path=db, provider=SimulatedRazorpayProvider())

    # Simulate menu price hike on D002 after proposal locked
    conn = get_sqlite_connection(db)
    with conn:
        conn.execute(
            """
            INSERT INTO menu_item_extensions (item_id, price_paise, tax_rate_bps, packaging_paise)
            VALUES ('D002', 45000, 500, 0)
            ON CONFLICT(item_id) DO UPDATE SET price_paise = 45000;
            """
        )
    conn.close()

    with pytest.raises(PriceDriftError):
        orch.start_payments(setup["session_id"], host_participant_id=setup["host"]["id"])


def test_each_pays_own_all_pay_single_order_double_click_and_sum_conservation(tmp_path: Path) -> None:
    db = tmp_path / "all_pay.db"
    setup = _seed_locked_session(db, delivery_address_id="addr-indiranagar")
    provider = SimulatedRazorpayProvider()
    orch = PaymentOrchestrator(db_path=db, provider=provider)
    sid = setup["session_id"]

    # Window > 30 min rejected cleanly
    with pytest.raises(Exception, match="window_seconds"):
        orch.start_payments(sid, host_participant_id=setup["host"]["id"], window_seconds=2400)

    state = orch.start_payments(sid, host_participant_id=setup["host"]["id"], window_seconds=900)
    assert state["session_status"] == "paying"
    # Requirement 10: Nobody has a payment / charge until calling initiate_payment
    assert state["payments"] == []
    assert state["captured_total_paise"] == 0
    assert state["frozen_total_paise"] == setup["grand_total_paise"]

    # Each participant presses Pay (double-click safe + double-callback safe)
    payers = [
        (setup["host"]["id"], "upi_qr"),
        (setup["guest1"]["id"], "upi_intent"),
        (setup["guest2"]["id"], "netbanking"),
    ]
    for idx, (pid, method) in enumerate(payers):
        idem = f"btn_click_{pid}"
        first_call = orch.initiate_payment(sid, participant_id=pid, method=method, idempotency_key=idem)
        second_call = orch.initiate_payment(sid, participant_id=pid, method=method, idempotency_key=idem)
        assert first_call["gateway_order_id"] == second_call["gateway_order_id"]
        assert second_call["idempotent_replay"] is True

        gw_pid, gw_sig = provider.simulate_payment_capture(first_call["gateway_order_id"], method=method)
        verify_state = orch.verify_checkout_payment(
            sid,
            participant_id=pid,
            gateway_order_id=first_call["gateway_order_id"],
            gateway_payment_id=gw_pid,
            gateway_signature=gw_sig,
        )
        # Double-callback safety
        verify_again = orch.verify_checkout_payment(
            sid,
            participant_id=pid,
            gateway_order_id=first_call["gateway_order_id"],
            gateway_payment_id=gw_pid,
            gateway_signature=gw_sig,
        )
        if idx < len(payers) - 1:
            assert verify_state["session_status"] == "paying"
            assert verify_state["kitchen_order"] is None
        else:
            assert verify_state["session_status"] == "placed"
            assert verify_again["session_status"] == "placed"
            assert verify_state["kitchen_order"]["order_id"] == verify_again["kitchen_order"]["order_id"]

    final_state = orch.get_payment_state(sid)
    assert final_state["session_status"] == "placed"
    assert final_state["captured_total_paise"] == final_state["frozen_total_paise"] == setup["grand_total_paise"]
    assert len({p["gateway_order_id"] for p in final_state["payments"]}) == 3
    assert final_state["kitchen_order"]["kitchen_status"] == "pending_acceptance"


def test_one_fails_then_retries_without_affecting_other_captured_payers(tmp_path: Path) -> None:
    db = tmp_path / "fail_retry.db"
    setup = _seed_locked_session(db, two_diners_only=True)
    provider = SimulatedRazorpayProvider()
    orch = PaymentOrchestrator(db_path=db, provider=provider)
    sid = setup["session_id"]

    orch.start_payments(sid, host_participant_id=setup["host"]["id"], now=1000)

    # Host pays cleanly
    h_init = orch.initiate_payment(sid, participant_id=setup["host"]["id"], method="card", idempotency_key="h1", now=1010)
    h_pid, h_sig = provider.simulate_payment_capture(h_init["gateway_order_id"])
    st1 = orch.verify_checkout_payment(
        sid, participant_id=setup["host"]["id"], gateway_order_id=h_init["gateway_order_id"], gateway_payment_id=h_pid, gateway_signature=h_sig, now=1020
    )
    assert st1["captured_total_paise"] == h_init["amount_paise"]

    # Guest1 payment fails on gateway + webhook arrives
    g_init = orch.initiate_payment(sid, participant_id=setup["guest1"]["id"], method="upi_qr", idempotency_key="g1_try1", now=1030)
    f_pid = provider.simulate_payment_failure(g_init["gateway_order_id"], reason="INSUFFICIENT_FUNDS")
    raw_wh, hdrs = provider.build_signed_webhook(
        event_type="payment.failed",
        gateway_order_id=g_init["gateway_order_id"],
        gateway_payment_id=f_pid,
        amount_paise=g_init["amount_paise"],
        failure_reason="INSUFFICIENT_FUNDS",
    )
    orch.handle_webhook(raw_body=raw_wh, signature=hdrs["X-Razorpay-Signature"], event_id_header=hdrs["X-Razorpay-Event-Id"], now=1040)

    mid = orch.get_payment_state(sid, now=1045)
    assert mid["session_status"] == "paying"
    host_ob = next(ob for ob in mid["obligations"] if ob["participant_id"] == setup["host"]["id"])
    g1_ob = next(ob for ob in mid["obligations"] if ob["participant_id"] == setup["guest1"]["id"])
    assert host_ob["status"] == "captured" and host_ob["display_status"] == "Paid"
    assert g1_ob["status"] == "failed" and g1_ob["display_status"] == "Failed"

    # Guest1 retries inside window -> succeeds -> order placed cleanly
    g_retry = orch.initiate_payment(sid, participant_id=setup["guest1"]["id"], method="upi_intent", idempotency_key="g1_try2", now=1060)
    assert g_retry["gateway_order_id"] != g_init["gateway_order_id"]
    g_pid, g_sig = provider.simulate_payment_capture(g_retry["gateway_order_id"])
    done = orch.verify_checkout_payment(
        sid, participant_id=setup["guest1"]["id"], gateway_order_id=g_retry["gateway_order_id"], gateway_payment_id=g_pid, gateway_signature=g_sig, now=1070
    )
    assert done["session_status"] == "placed"
    assert done["captured_total_paise"] == setup["grand_total_paise"]


def test_expired_window_choice_a_cover_unpaid(tmp_path: Path) -> None:
    db = tmp_path / "exp_a.db"
    setup = _seed_locked_session(db)
    provider = SimulatedRazorpayProvider()
    orch = PaymentOrchestrator(db_path=db, provider=provider)
    sid = setup["session_id"]

    t0 = 1_760_000_000
    orch.start_payments(sid, host_participant_id=setup["host"]["id"], window_seconds=900, now=t0)
    # Host pays share, Guests do not pay
    h_init = orch.initiate_payment(sid, participant_id=setup["host"]["id"], idempotency_key="h_cover1", now=t0 + 10)
    h_pid, h_sig = provider.simulate_payment_capture(h_init["gateway_order_id"])
    orch.verify_checkout_payment(
        sid, participant_id=setup["host"]["id"], gateway_order_id=h_init["gateway_order_id"], gateway_payment_id=h_pid, gateway_signature=h_sig, now=t0 + 20
    )

    # Past window expiry -> cannot pay expired share, must resolve via host choice
    exp_t = t0 + 950
    with pytest.raises(PaymentConflictError):
        orch.initiate_payment(sid, participant_id=setup["guest1"]["id"], idempotency_key="late_try", now=exp_t)

    # Host picks (a) cover_unpaid
    res_a = orch.resolve_expired_window(
        sid, host_participant_id=setup["host"]["id"], choice="cover_unpaid", extend_window_seconds=600, now=exp_t + 5
    )
    assert res_a["session_status"] == "paying"
    cov_ob = next(ob for ob in res_a["obligations"] if ob["obligation_kind"] == "host_coverage")
    assert cov_ob["amount_paise"] == setup["grand_total_paise"] - h_init["amount_paise"]
    assert res_a["kitchen_order"] is None  # Still not placed until host explicitly clicks Pay!

    # Host pays coverage difference -> order placed
    cov_init = orch.initiate_payment(sid, participant_id=setup["host"]["id"], idempotency_key="h_cov_pay", now=exp_t + 10)
    c_pid, c_sig = provider.simulate_payment_capture(cov_init["gateway_order_id"])
    final_st = orch.verify_checkout_payment(
        sid, participant_id=setup["host"]["id"], gateway_order_id=cov_init["gateway_order_id"], gateway_payment_id=c_pid, gateway_signature=c_sig, now=exp_t + 20
    )
    assert final_st["session_status"] == "placed"
    assert final_st["captured_total_paise"] == setup["grand_total_paise"]


def test_expired_window_choice_b_drop_unpaid_resplit_relock_and_repay(tmp_path: Path) -> None:
    db = tmp_path / "exp_b.db"
    setup = _seed_locked_session(db)
    provider = SimulatedRazorpayProvider()
    orch = PaymentOrchestrator(db_path=db, provider=provider)
    svc = SessionService(db_path=db)
    sid = setup["session_id"]

    t0 = 1_760_000_000
    orch.start_payments(sid, host_participant_id=setup["host"]["id"], window_seconds=900, now=t0)

    # Rohan (shared pizza with Amit) and Ananya (personal Samosa) BOTH pay; Amit never pays!
    h_init = orch.initiate_payment(sid, participant_id=setup["host"]["id"], idempotency_key="h_b1", now=t0 + 10)
    hp, hs = provider.simulate_payment_capture(h_init["gateway_order_id"])
    orch.verify_checkout_payment(sid, participant_id=setup["host"]["id"], gateway_order_id=h_init["gateway_order_id"], gateway_payment_id=hp, gateway_signature=hs, now=t0 + 20)

    g1_init = orch.initiate_payment(sid, participant_id=setup["guest1"]["id"], idempotency_key="g1_b1", now=t0 + 30)
    g1p, g1s = provider.simulate_payment_capture(g1_init["gateway_order_id"])
    orch.verify_checkout_payment(sid, participant_id=setup["guest1"]["id"], gateway_order_id=g1_init["gateway_order_id"], gateway_payment_id=g1p, gateway_signature=g1s, now=t0 + 40)

    # Past deadline -> Host chooses (b) drop_unpaid_re_split
    exp_t = t0 + 950
    res_b = orch.resolve_expired_window(
        sid, host_participant_id=setup["host"]["id"], choice="drop_unpaid_re_split", now=exp_t
    )
    assert res_b["session_status"] == "proposing"  # Requires fresh lock!
    # Rohan's old payment refunded because his shared pizza became personal (price changed 12600p -> 25200p)
    assert any(r["participant_id"] == setup["host"]["id"] and r["amount_paise"] == h_init["amount_paise"] for r in res_b["refunds"])
    # Ananya's share stayed exact (Samosa personal) -> NOT refunded!
    assert not any(r["participant_id"] == setup["guest1"]["id"] for r in res_b["refunds"])

    # Cannot skip fresh lock!
    with pytest.raises(PaymentConflictError):
        orch.start_payments(sid, host_participant_id=setup["host"]["id"], now=exp_t + 5)

    # Host locks new split cleanly -> starts paying -> ONLY Rohan pays new share -> order placed!
    sess_after = svc.get_session_state(sid, viewer_participant_id=setup["host"]["id"])
    svc.lock_session(sid, host_participant_id=setup["host"]["id"], expected_version=sess_after["version"], now=exp_t + 10)
    re_pay_st = orch.start_payments(sid, host_participant_id=setup["host"]["id"], now=exp_t + 15)

    g1_ob_new = next(ob for ob in re_pay_st["obligations"] if ob["participant_id"] == setup["guest1"]["id"])
    h_ob_new = next(ob for ob in re_pay_st["obligations"] if ob["participant_id"] == setup["host"]["id"])
    assert g1_ob_new["status"] == "captured"
    assert h_ob_new["status"] == "unpaid"

    h2_init = orch.initiate_payment(sid, participant_id=setup["host"]["id"], idempotency_key="h_b2", now=exp_t + 20)
    h2p, h2s = provider.simulate_payment_capture(h2_init["gateway_order_id"])
    done = orch.verify_checkout_payment(sid, participant_id=setup["host"]["id"], gateway_order_id=h2_init["gateway_order_id"], gateway_payment_id=h2p, gateway_signature=h2s, now=exp_t + 25)
    assert done["session_status"] == "placed"
    # D013 Masala Chai (Amit personal) removed, D002 (240+12=252) + D008 (120+6=126) = 37800p exact!
    assert done["kitchen_order"]["total_paise"] == done["frozen_total_paise"] == done["captured_total_paise"]


def test_expired_window_choice_c_cancel_and_refund_partial_capture(tmp_path: Path) -> None:
    db = tmp_path / "exp_c.db"
    setup = _seed_locked_session(db)
    provider = SimulatedRazorpayProvider()
    orch = PaymentOrchestrator(db_path=db, provider=provider)
    sid = setup["session_id"]

    t0 = 1_760_000_000
    orch.start_payments(sid, host_participant_id=setup["host"]["id"], window_seconds=900, now=t0)

    # Rohan & Ananya pay; Amit fails/unpaid
    for pid in (setup["host"]["id"], setup["guest1"]["id"]):
        init = orch.initiate_payment(sid, participant_id=pid, idempotency_key=f"p_{pid}", now=t0 + 10)
        gw_pid, sig = provider.simulate_payment_capture(init["gateway_order_id"])
        orch.verify_checkout_payment(sid, participant_id=pid, gateway_order_id=init["gateway_order_id"], gateway_payment_id=gw_pid, gateway_signature=sig, now=t0 + 20)

    res_c = orch.resolve_expired_window(
        sid, host_participant_id=setup["host"]["id"], choice="cancel_and_refund", now=t0 + 950
    )
    assert res_c["session_status"] == "cancelled"
    assert res_c["kitchen_order"] is None
    assert len(res_c["refunds"]) == 2
    assert res_c["refunded_total_paise"] == sum(r["amount_paise"] for r in res_c["refunds"])
    assert len(provider.refund_calls) == 2


def test_webhooks_duplicate_out_of_order_tampered_and_late_after_cancel(tmp_path: Path) -> None:
    db = tmp_path / "webhooks.db"
    setup = _seed_locked_session(db, two_diners_only=True)
    provider = SimulatedRazorpayProvider()
    orch = PaymentOrchestrator(db_path=db, provider=provider)
    sid = setup["session_id"]

    orch.start_payments(sid, host_participant_id=setup["host"]["id"], now=1000)
    h_init = orch.initiate_payment(sid, participant_id=setup["host"]["id"], idempotency_key="wh_h", now=1010)
    g_init = orch.initiate_payment(sid, participant_id=setup["guest1"]["id"], idempotency_key="wh_g", now=1015)

    # 1. Tampered webhook signature rejected without DB write
    bad_body, bad_hdrs = provider.build_signed_webhook(
        event_type="payment.captured",
        gateway_order_id=h_init["gateway_order_id"],
        gateway_payment_id="pay_tamper",
        amount_paise=h_init["amount_paise"],
    )
    with pytest.raises(InvalidSignatureError):
        orch.handle_webhook(raw_body=bad_body, signature="bad_sig_000", event_id_header=bad_hdrs["X-Razorpay-Event-Id"], now=1020)
    # Tampering body bytes also fails signature!
    with pytest.raises(InvalidSignatureError):
        orch.handle_webhook(raw_body=bad_body + b" ", signature=bad_hdrs["X-Razorpay-Signature"], event_id_header=bad_hdrs["X-Razorpay-Event-Id"], now=1020)

    # 2. Valid capture webhook + Duplicate webhook (same event_id ignored cleanly!)
    cap_pid = "pay_wh_host_001"
    raw_cap, cap_hdrs = provider.build_signed_webhook(
        event_type="payment.captured",
        gateway_order_id=h_init["gateway_order_id"],
        gateway_payment_id=cap_pid,
        amount_paise=h_init["amount_paise"],
        event_id="evt_unique_host_cap",
    )
    w1 = orch.handle_webhook(raw_body=raw_cap, signature=cap_hdrs["X-Razorpay-Signature"], event_id_header=cap_hdrs["X-Razorpay-Event-Id"], now=1025)
    w2 = orch.handle_webhook(raw_body=raw_cap, signature=cap_hdrs["X-Razorpay-Signature"], event_id_header=cap_hdrs["X-Razorpay-Event-Id"], now=1026)
    assert w1["duplicate"] is False and w1["processing_status"] == "processed"
    assert w2["duplicate"] is True

    # 3. Out-of-order delayed payment.failed arriving AFTER payment.captured MUST NOT downgrade capture!
    raw_fail, fail_hdrs = provider.build_signed_webhook(
        event_type="payment.failed",
        gateway_order_id=h_init["gateway_order_id"],
        gateway_payment_id=cap_pid,
        amount_paise=h_init["amount_paise"],
        failure_reason="STALE_TIMEOUT",
        event_id="evt_stale_fail",
    )
    wf = orch.handle_webhook(raw_body=raw_fail, signature=fail_hdrs["X-Razorpay-Signature"], event_id_header=fail_hdrs["X-Razorpay-Event-Id"], now=1030)
    assert wf["processing_status"] == "ignored_stale"
    h_ob = next(ob for ob in orch.get_payment_state(sid)["obligations"] if ob["participant_id"] == setup["host"]["id"])
    assert h_ob["status"] == "captured"

    # 4. Host cancels session -> host capture refunded; then LATE capture webhook arrives for Guest1!
    orch.cancel_or_request_refund(sid, actor_participant_id=setup["host"]["id"], now=1040)
    assert orch.get_payment_state(sid)["session_status"] == "cancelled"

    raw_late, late_hdrs = provider.build_signed_webhook(
        event_type="payment.captured",
        gateway_order_id=g_init["gateway_order_id"],
        gateway_payment_id="pay_late_guest1",
        amount_paise=g_init["amount_paise"],
        event_id="evt_late_capture",
    )
    wl = orch.handle_webhook(raw_body=raw_late, signature=late_hdrs["X-Razorpay-Signature"], event_id_header=late_hdrs["X-Razorpay-Event-Id"], now=1050)
    assert wl["processing_status"] == "late_refunded"

    st_after = orch.get_payment_state(sid)
    assert st_after["session_status"] == "cancelled"
    assert st_after["kitchen_order"] is None
    assert len(st_after["refunds"]) == 2
    assert any(r["reason"] == "late_capture_after_cancel" and r["participant_id"] == setup["guest1"]["id"] for r in st_after["refunds"])


def test_scheduled_reconciler_recovers_stuck_pending_payments(tmp_path: Path) -> None:
    db = tmp_path / "recon.db"
    setup = _seed_locked_session(db, two_diners_only=True)
    provider = SimulatedRazorpayProvider()
    orch = PaymentOrchestrator(db_path=db, provider=provider)
    sid = setup["session_id"]

    orch.start_payments(sid, host_participant_id=setup["host"]["id"], now=1000)
    h_init = orch.initiate_payment(sid, participant_id=setup["host"]["id"], idempotency_key="rec_h", now=1010)
    g_init = orch.initiate_payment(sid, participant_id=setup["guest1"]["id"], idempotency_key="rec_g", now=1010)

    # Gateway actually captured both payments without delivering checkout callback or webhook!
    provider.simulate_payment_pending(h_init["gateway_order_id"], settle_on_gateway_later=True)
    provider.simulate_payment_pending(g_init["gateway_order_id"], settle_on_gateway_later=True)

    assert orch.get_payment_state(sid)["session_status"] == "paying"

    report = reconcile_pending_payments(db_path=db, provider=provider, stale_after_seconds=30, now=1050)
    assert report["checked_count"] == 2
    assert report["captured_count"] == 2

    st = orch.get_payment_state(sid, now=1050)
    assert st["session_status"] == "placed"
    assert st["kitchen_order"] is not None
    assert st["captured_total_paise"] == setup["grand_total_paise"]


def test_refund_before_vs_after_kitchen_acceptance_and_host_pays_all(tmp_path: Path) -> None:
    # A) host_pays_all + informational settlements + pre-acceptance refund
    db1 = tmp_path / "host_all.db"
    setup1 = _seed_locked_session(db1)
    prov1 = SimulatedRazorpayProvider()
    orch1 = PaymentOrchestrator(db_path=db1, provider=prov1)
    sid1 = setup1["session_id"]

    st = orch1.start_payments(sid1, host_participant_id=setup1["host"]["id"], payment_mode="host_pays_all", now=1000)
    assert len(st["obligations"]) == 1
    assert st["obligations"][0]["participant_id"] == setup1["host"]["id"]
    assert st["obligations"][0]["amount_paise"] == setup1["grand_total_paise"]
    assert len(st["host_settlements"]) == 2
    assert all(s["informational_only"] is True and s["upi_intent_uri"].startswith("upi://pay?") for s in st["host_settlements"])

    # Guest cannot initiate gateway order in host_pays_all mode
    with pytest.raises(PaymentForbiddenError):
        orch1.initiate_payment(sid1, participant_id=setup1["guest1"]["id"], now=1005)

    # Host pays entire bill -> kitchen order placed immediately even before guests settle IOUs
    h_init = orch1.initiate_payment(sid1, participant_id=setup1["host"]["id"], method="card", now=1010)
    hp, hs = prov1.simulate_payment_capture(h_init["gateway_order_id"])
    placed = orch1.verify_checkout_payment(
        sid1, participant_id=setup1["host"]["id"], gateway_order_id=h_init["gateway_order_id"], gateway_payment_id=hp, gateway_signature=hs, now=1020
    )
    assert placed["session_status"] == "placed"
    assert placed["kitchen_order"]["kitchen_status"] == "pending_acceptance"

    # Peer marks informational settlement -> never alters captured state
    settled_view = orch1.mark_host_settlement_paid(sid1, participant_id=setup1["guest1"]["id"], now=1030)
    assert any(s["participant_id"] == setup1["guest1"]["id"] and s["status"] == "marked_settled" for s in settled_view["host_settlements"])

    # Cancel BEFORE kitchen acceptance -> auto-refund!
    pre_cancel = orch1.cancel_or_request_refund(sid1, actor_participant_id=setup1["host"]["id"], now=1040)
    assert pre_cancel["refund_mode"] == "automatic"
    assert pre_cancel["payment_state"]["session_status"] == "cancelled"
    assert pre_cancel["payment_state"]["refunded_total_paise"] == setup1["grand_total_paise"]

    # B) After kitchen acceptance -> routes to support ticket path cleanly (zero gateway refund)
    db2 = tmp_path / "post_acc.db"
    setup2 = _seed_locked_session(db2, two_diners_only=True)
    prov2 = SimulatedRazorpayProvider()
    orch2 = PaymentOrchestrator(db_path=db2, provider=prov2)
    sid2 = setup2["session_id"]

    orch2.start_payments(sid2, host_participant_id=setup2["host"]["id"], payment_mode="host_pays_all", now=2000)
    hi2 = orch2.initiate_payment(sid2, participant_id=setup2["host"]["id"], now=2010)
    hp2, hs2 = prov2.simulate_payment_capture(hi2["gateway_order_id"])
    orch2.verify_checkout_payment(sid2, participant_id=setup2["host"]["id"], gateway_order_id=hi2["gateway_order_id"], gateway_payment_id=hp2, gateway_signature=hs2, now=2020)

    orch2.accept_kitchen_order(sid2, now=2030)
    post_cancel = orch2.cancel_or_request_refund(sid2, actor_participant_id=setup2["host"]["id"], now=2040)
    assert post_cancel["refund_mode"] == "support_required"
    assert post_cancel["support_ticket_id"] is not None and post_cancel["support_ticket_id"].startswith("SUP-")
    assert prov2.refund_calls == []
    assert post_cancel["payment_state"]["session_status"] == "placed"


def test_concurrency_simultaneous_all_paid_triggers_place_exactly_one_order(tmp_path: Path) -> None:
    db = tmp_path / "concurrent_place.db"
    setup = _seed_locked_session(db, two_diners_only=True)
    provider = SimulatedRazorpayProvider()
    orch = PaymentOrchestrator(db_path=db, provider=provider)
    sid = setup["session_id"]

    orch.start_payments(sid, host_participant_id=setup["host"]["id"], now=3000)
    h_init = orch.initiate_payment(sid, participant_id=setup["host"]["id"], idempotency_key="conc_h", now=3005)
    g_init = orch.initiate_payment(sid, participant_id=setup["guest1"]["id"], idempotency_key="conc_g", now=3006)

    hp, hs = provider.simulate_payment_capture(h_init["gateway_order_id"])
    gp, gs = provider.simulate_payment_capture(g_init["gateway_order_id"])

    # Prepare simultaneous capture callbacks + webhook duplicates racing concurrently
    wh_raw, wh_hdrs = provider.build_signed_webhook(
        event_type="order.paid",
        gateway_order_id=g_init["gateway_order_id"],
        gateway_payment_id=gp,
        amount_paise=g_init["amount_paise"],
    )

    barrier = threading.Barrier(3)

    def _finish_host() -> str:
        barrier.wait()
        res = orch.verify_checkout_payment(
            sid, participant_id=setup["host"]["id"], gateway_order_id=h_init["gateway_order_id"], gateway_payment_id=hp, gateway_signature=hs, now=3010
        )
        return res["session_status"]

    def _finish_guest() -> str:
        barrier.wait()
        res = orch.verify_checkout_payment(
            sid, participant_id=setup["guest1"]["id"], gateway_order_id=g_init["gateway_order_id"], gateway_payment_id=gp, gateway_signature=gs, now=3010
        )
        return res["session_status"]

    def _finish_webhook() -> str:
        barrier.wait()
        res = orch.handle_webhook(raw_body=wh_raw, signature=wh_hdrs["X-Razorpay-Signature"], event_id_header=wh_hdrs["X-Razorpay-Event-Id"], now=3010)
        return res["status"]

    with ThreadPoolExecutor(max_workers=3) as pool:
        f1 = pool.submit(_finish_host)
        f2 = pool.submit(_finish_guest)
        f3 = pool.submit(_finish_webhook)
        _, _, _ = f1.result(), f2.result(), f3.result()

    conn = get_sqlite_connection(db)
    ko_count = conn.execute("SELECT COUNT(*) FROM kitchen_orders WHERE session_id = ?;", (sid,)).fetchone()[0]
    evt_count = conn.execute(
        "SELECT COUNT(*) FROM session_events WHERE session_id = ? AND type = 'order.placed';", (sid,)
    ).fetchone()[0]
    conn.close()
    assert ko_count == 1
    assert evt_count == 1


def test_api_payments_end_to_end_fastapi_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_file = tmp_path / "stage9_api.db"
    monkeypatch.setenv("TABLE_SPLIT_DB_PATH", str(db_file))
    setup = _seed_locked_session(db_file, two_diners_only=True)
    sid = setup["session_id"]
    h_tok = setup["host"]["token"]
    g_tok = setup["guest1"]["token"]

    hub = SSEHub()
    provider = SimulatedRazorpayProvider()
    limiter = SlidingWindowRateLimiter(rules={"preference_participant": RateLimitRule(200, 60)})
    svc = SessionService(db_path=db_file, sse_hub=hub)
    orch = PaymentOrchestrator(db_path=db_file, provider=provider, sse_hub=hub)

    app.dependency_overrides[get_sse_hub] = lambda: hub
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    app.dependency_overrides[get_session_service] = lambda: svc
    app.dependency_overrides[get_payment_provider] = lambda: provider
    app.dependency_overrides[get_payment_orchestrator] = lambda: orch

    client = TestClient(app)
    try:
        # 1. Start paying
        start_res = client.post(
            f"/table-sessions/{sid}/payments/start",
            headers={"X-Guest-Token": h_tok},
            json={"payment_mode": "each_pays_own", "window_seconds": 900},
        )
        assert start_res.status_code == 200
        assert start_res.json()["session_status"] == "paying"

        # 2. Ban UPI Collect via API (422)
        bad_pay = client.post(
            f"/table-sessions/{sid}/payments/pay",
            headers={"X-Guest-Token": h_tok},
            json={"method": "upi_collect"},
        )
        assert bad_pay.status_code == 422

        # 3. Host & Guest initiate + verify via API
        p1 = client.post(
            f"/table-sessions/{sid}/payments/pay",
            headers={"X-Guest-Token": h_tok, "Idempotency-Key": "api_h1"},
            json={"method": "upi_qr"},
        )
        assert p1.status_code == 200
        p1_data = p1.json()
        pid1, sig1 = provider.simulate_payment_capture(p1_data["gateway_order_id"])

        v1 = client.post(
            f"/table-sessions/{sid}/payments/verify",
            headers={"X-Guest-Token": h_tok},
            json={"gateway_order_id": p1_data["gateway_order_id"], "gateway_payment_id": pid1, "gateway_signature": sig1},
        )
        assert v1.status_code == 200 and v1.json()["session_status"] == "paying"

        # Guest initiates, completes via raw webhook endpoint cleanly!
        p2 = client.post(
            f"/table-sessions/{sid}/payments/pay",
            headers={"X-Guest-Token": g_tok, "Idempotency-Key": "api_g1"},
            json={"method": "upi_intent"},
        )
        p2_data = p2.json()
        gw_pid2, _ = provider.simulate_payment_capture(p2_data["gateway_order_id"])
        raw_wh, wh_hdrs = provider.build_signed_webhook(
            event_type="order.paid",
            gateway_order_id=p2_data["gateway_order_id"],
            gateway_payment_id=gw_pid2,
            amount_paise=p2_data["amount_paise"],
        )

        # Tampered webhook fails 400
        bad_wh = client.post("/table-split/webhooks/payment", content=raw_wh, headers={**wh_hdrs, "X-Razorpay-Signature": "bad"})
        assert bad_wh.status_code == 400

        ok_wh = client.post("/table-split/webhooks/payment", content=raw_wh, headers=wh_hdrs)
        assert ok_wh.status_code == 200 and ok_wh.json()["processing_status"] == "processed"

        # 4. Shared tracking endpoint accessible by both host and guest
        trk_guest = client.get(f"/table-sessions/{sid}/tracking", headers={"X-Guest-Token": g_tok})
        assert trk_guest.status_code == 200
        assert trk_guest.json()["order_id"].startswith("ORD-")
        assert trk_guest.json()["total_paise"] == setup["grand_total_paise"]
    finally:
        app.dependency_overrides.clear()

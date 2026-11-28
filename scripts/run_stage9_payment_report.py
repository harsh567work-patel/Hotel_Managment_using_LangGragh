"""
scripts.run_stage9_payment_report — Executes end-to-end Stage 9 financial scenarios via TestClient
and emits a concise verification report at docs/table-split/PAYMENT_REPORT.md.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _seed_locked_report_session(db_path: Path, *, two_diners_only: bool = False) -> dict[str, Any]:
    """Seeds a locked Table Split session with deterministic Stage 7 allocations cleanly."""
    from table_split.db.dietary_repo import DEFAULT_MENU_JSON_PATH, _load_menu_json
    from table_split.db.engine import get_sqlite_connection
    from table_split.db.migrations.runner import migrate_up
    from table_split.pricing import DEFAULT_GST_BPS, PricingLineInput, StubPricingEngine
    from table_split.services.session_service import SessionService
    from table_split.sse import SSEHub

    migrate_up(db_path)
    svc = SessionService(db_path=db_path, sse_hub=SSEHub())
    created = svc.create_session(display_name="Rohan (Host)")
    sid, jtok = created["session_id"], created["join_token"]
    hpid, htok = created["host_participant_id"], created["host_token"]
    g1 = svc.join_session(sid, display_name="Ananya", join_token=jtok)
    g1_pid, g1_tok = g1["participant_id"], g1["guest_token"]

    pids = [hpid, g1_pid]
    g2_pid, g2_tok = None, None
    if not two_diners_only:
        g2 = svc.join_session(sid, display_name="Amit", join_token=jtok)
        g2_pid, g2_tok = g2["participant_id"], g2["guest_token"]
        pids.append(g2_pid)

    menu_map = {str(r["id"]): r for r in _load_menu_json(DEFAULT_MENU_JSON_PATH)}
    d002_p = int(Decimal(str(menu_map["D002"]["price"])) * 100)
    d008_p = int(Decimal(str(menu_map["D008"]["price"])) * 100)
    d013_p = int(Decimal(str(menu_map["D013"]["price"])) * 100)

    if two_diners_only:
        lines = [
            PricingLineInput(menu_item_id="D002", dish_name="Paneer Tikka Masala", quantity=1, kind="SHARED", consumer_participant_ids=[hpid, g1_pid], unit_price_paise=d002_p, tax_rate_bps=DEFAULT_GST_BPS),
            PricingLineInput(menu_item_id="D008", dish_name="Samosa", quantity=1, kind="PERSONAL", consumer_participant_ids=[g1_pid], unit_price_paise=d008_p, tax_rate_bps=DEFAULT_GST_BPS),
        ]
    else:
        assert g2_pid is not None
        lines = [
            PricingLineInput(menu_item_id="D002", dish_name="Paneer Tikka Masala", quantity=1, kind="SHARED", consumer_participant_ids=[hpid, g2_pid], unit_price_paise=d002_p, tax_rate_bps=DEFAULT_GST_BPS),
            PricingLineInput(menu_item_id="D008", dish_name="Samosa", quantity=1, kind="PERSONAL", consumer_participant_ids=[g1_pid], unit_price_paise=d008_p, tax_rate_bps=DEFAULT_GST_BPS),
            PricingLineInput(menu_item_id="D013", dish_name="Masala Chai", quantity=1, kind="PERSONAL", consumer_participant_ids=[g2_pid], unit_price_paise=d013_p, tax_rate_bps=DEFAULT_GST_BPS),
        ]

    pricing = StubPricingEngine().calculate_proposal_pricing(lines, sorted(pids))
    conn = get_sqlite_connection(db_path)
    prop_id = str(uuid4())
    with conn:
        s_ver = int(conn.execute("SELECT version FROM table_sessions WHERE id = ?;", (sid,)).fetchone()["version"])
        expl = json.dumps({"proposal_id": prop_id, "proposal_version": 1, "status": "OK", "pricing": {"pricing_version": pricing.pricing_version, "subtotal_paise": pricing.subtotal_paise, "tax_paise": pricing.tax_paise, "packaging_paise": pricing.packaging_paise, "delivery_fee_paise": 0, "discount_paise": 0, "grand_total_paise": pricing.grand_total_paise}})
        prop_cols = {r[1] for r in conn.execute("PRAGMA table_info(proposals);").fetchall()}
        if "proposal_version" in prop_cols:
            conn.execute("INSERT INTO proposals (id, session_id, session_version, proposal_version, engine_version, status, constraints_hash, explanation_json, diff_json, created_at) VALUES (?, ?, ?, 1, '1.0.0', 'active', 'h_rep', ?, '{}', 1760000000);", (prop_id, sid, s_ver, expl))
        else:
            conn.execute("INSERT INTO proposals (id, session_id, session_version, engine_version, status, constraints_hash, explanation_json, created_at) VALUES (?, ?, ?, '1.0.0', 'active', 'h_rep', ?, 1760000000);", (prop_id, sid, s_ver, expl))
        for ln in lines:
            conn.execute("INSERT INTO proposal_lines (id, proposal_id, menu_item_id, quantity, kind, consumer_participant_ids, unit_price_paise) VALUES (?, ?, ?, ?, ?, ?, ?);", (str(uuid4()), prop_id, ln.menu_item_id, ln.quantity, ln.kind, json.dumps(ln.consumer_participant_ids), ln.unit_price_paise))
        for pid, al in pricing.allocations.items():
            conn.execute("INSERT INTO allocations (id, proposal_id, participant_id, items_paise, tax_paise, delivery_share_paise, packaging_share_paise, discount_share_paise, rounding_paise, total_paise) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);", (str(uuid4()), prop_id, pid, al.items_paise, al.tax_paise, al.delivery_share_paise, al.packaging_share_paise, al.discount_share_paise, al.rounding_paise, al.total_paise))
        conn.execute("UPDATE table_sessions SET status = 'locked', version = version + 1 WHERE id = ?;", (sid,))
    conn.close()
    return {"session_id": sid, "host": {"id": hpid, "token": htok}, "guest1": {"id": g1_pid, "token": g1_tok}, "guest2": {"id": g2_pid, "token": g2_tok}, "grand_total_paise": pricing.grand_total_paise}


def main() -> None:
    from api import app
    from table_split.api_payments import get_payment_orchestrator, get_payment_provider
    from table_split.api_sessions import (
        get_rate_limiter,
        get_session_service,
        get_sse_hub,
    )
    from table_split.gateways.simulated import SimulatedRazorpayProvider
    from table_split.rate_limit import RateLimitRule, SlidingWindowRateLimiter
    from table_split.services.payment_reconciler import reconcile_pending_payments
    from table_split.services.payment_service import PaymentOrchestrator
    from table_split.services.session_service import SessionService
    from table_split.sse import SSEHub

    report_rows: list[tuple[str, str, str]] = []

    with tempfile.TemporaryDirectory(prefix="stage9_report_") as td:
        db_file = Path(td) / "report.db"
        os.environ["TABLE_SPLIT_DB_PATH"] = str(db_file)

        hub = SSEHub()
        provider = SimulatedRazorpayProvider()
        limiter = SlidingWindowRateLimiter(rules={"preference_participant": RateLimitRule(500, 60)})
        svc = SessionService(db_path=db_file, sse_hub=hub)
        orch = PaymentOrchestrator(db_path=db_file, provider=provider, sse_hub=hub)

        app.dependency_overrides[get_sse_hub] = lambda: hub
        app.dependency_overrides[get_rate_limiter] = lambda: limiter
        app.dependency_overrides[get_session_service] = lambda: svc
        app.dependency_overrides[get_payment_provider] = lambda: provider
        app.dependency_overrides[get_payment_orchestrator] = lambda: orch
        client = TestClient(app)

        try:
            # Scenario 1: Happy-path each_pays_own (3 diners, UPI QR / Intent / Card)
            s1 = _seed_locked_report_session(db_file)
            sid1, htok1 = s1["session_id"], s1["host"]["token"]
            client.post(f"/table-sessions/{sid1}/payments/start", headers={"X-Guest-Token": htok1}, json={"window_seconds": 900})
            for idx, (tok, meth) in enumerate([
                (s1["host"]["token"], "upi_qr"),
                (s1["guest1"]["token"], "upi_intent"),
                (s1["guest2"]["token"], "card"),
            ]):
                pay_r = client.post(
                    f"/table-sessions/{sid1}/payments/pay",
                    headers={"X-Guest-Token": tok, "Idempotency-Key": f"rep_s1_{idx}"},
                    json={"method": meth},
                ).json()
                gpid, sig = provider.simulate_payment_capture(pay_r["gateway_order_id"])
                ver_r = client.post(
                    f"/table-sessions/{sid1}/payments/verify",
                    headers={"X-Guest-Token": tok},
                    json={"gateway_order_id": pay_r["gateway_order_id"], "gateway_payment_id": gpid, "gateway_signature": sig},
                ).json()
            trk1 = client.get(f"/table-sessions/{sid1}/tracking", headers={"X-Guest-Token": htok1}).json()
            assert ver_r["captured_total_paise"] == s1["grand_total_paise"]
            report_rows.append(("1. 3-Diner Split Checkout (UPI QR/Intent/Card)", "PASS", f"Order `{trk1['order_id']}`, captured `{ver_r['captured_total_paise']}p == {s1['grand_total_paise']}p`"))

            # Scenario 2: NPCI Collect Rejection + Tampered Webhook Rejection
            s2 = _seed_locked_report_session(db_file, two_diners_only=True)
            sid2, htok2 = s2["session_id"], s2["host"]["token"]
            client.post(f"/table-sessions/{sid2}/payments/start", headers={"X-Guest-Token": htok2}, json={})
            collect_res = client.post(
                f"/table-sessions/{sid2}/payments/pay",
                headers={"X-Guest-Token": htok2},
                json={"method": "upi_collect"},
            )
            bad_wh = client.post("/table-split/webhooks/payment", content=b'{"event":"payment.captured"}', headers={"X-Razorpay-Signature": "forged"})
            assert collect_res.status_code == 422 and bad_wh.status_code == 400
            report_rows.append(("2. NPCI Collect Ban + Webhook HMAC Reject", "PASS", f"Collect HTTP `{collect_res.status_code}`, Bad HMAC HTTP `{bad_wh.status_code}`"))

            # Scenario 3: Stuck Pending Reconciler -> Automatic Placement -> Pre-Accept Auto-Refund
            pay2 = client.post(f"/table-sessions/{sid2}/payments/pay", headers={"X-Guest-Token": htok2}, json={"method": "netbanking"}).json()
            pay2_g = client.post(f"/table-sessions/{sid2}/payments/pay", headers={"X-Guest-Token": s2["guest1"]["token"]}, json={"method": "upi_qr"}).json()
            provider.simulate_payment_pending(pay2["gateway_order_id"], settle_on_gateway_later=True)
            provider.simulate_payment_pending(pay2_g["gateway_order_id"], settle_on_gateway_later=True)
            rec = reconcile_pending_payments(db_path=db_file, provider=provider, stale_after_seconds=0)
            canc = client.post(f"/table-sessions/{sid2}/payments/cancel", headers={"X-Guest-Token": htok2}, json={"reason": "test_cancel"}).json()
            assert rec["captured_count"] >= 2 and canc["refund_mode"] == "automatic"
            report_rows.append(("3. Reconciler Recovery + Pre-Accept Auto-Refund", "PASS", f"Reconciled `{rec['captured_count']}` captures, refunded `{canc['payment_state']['refunded_total_paise']}p`"))

            # Scenario 4: host_pays_all + Post-Kitchen-Accept Support Escalation
            s4 = _seed_locked_report_session(db_file, two_diners_only=True)
            sid4, htok4 = s4["session_id"], s4["host"]["token"]
            client.post(f"/table-sessions/{sid4}/payments/start", headers={"X-Guest-Token": htok4}, json={"payment_mode": "host_pays_all"})
            hpay = client.post(f"/table-sessions/{sid4}/payments/pay", headers={"X-Guest-Token": htok4}, json={"method": "card"}).json()
            hp4, hs4 = provider.simulate_payment_capture(hpay["gateway_order_id"])
            client.post(f"/table-sessions/{sid4}/payments/verify", headers={"X-Guest-Token": htok4}, json={"gateway_order_id": hpay["gateway_order_id"], "gateway_payment_id": hp4, "gateway_signature": hs4})
            client.post(f"/table-sessions/{sid4}/kitchen/accept")
            sup = client.post(f"/table-sessions/{sid4}/payments/cancel", headers={"X-Guest-Token": htok4}, json={"reason": "late_change"}).json()
            assert sup["refund_mode"] == "support_required" and str(sup["support_ticket_id"]).startswith("SUP-")
            report_rows.append(("4. Host-Pays-All + Post-Accept Support Path", "PASS", f"Ticket `{sup['support_ticket_id']}`, 0 auto gateway refunds"))
        finally:
            app.dependency_overrides.clear()

    out_md = _REPO_ROOT / "docs" / "table-split" / "PAYMENT_REPORT.md"
    lines = [
        "# Stage 9 Payment Verification Report",
        "",
        "Executed live FastAPI `TestClient` scenarios against SQLite WAL + `SimulatedRazorpayProvider` (real HMAC-SHA256 signatures, NPCI-compliant methods only).",
        "",
        "| Scenario | Result | Observed Output |",
        "|---|---|---|",
    ]
    for name, status, obs in report_rows:
        lines.append(f"| {name} | **{status}** | {obs} |")
    lines.append("")
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()

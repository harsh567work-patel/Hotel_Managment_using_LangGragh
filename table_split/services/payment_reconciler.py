"""
table_split.services.payment_reconciler — Scheduled reconciliation job polling payment gateways
for Table Split payments stuck in `created` or `pending` states.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from config import TABLE_SPLIT_DB_PATH
from table_split.db.engine import get_sqlite_connection
from table_split.db.migrations.runner import migrate_up
from table_split.payment import PaymentError, PaymentProvider
from table_split.services.payment_service import PaymentOrchestrator
from table_split.sse import SSEHub

logger = logging.getLogger(__name__)


def reconcile_pending_payments(
    *,
    db_path: str | Path | None = None,
    provider: PaymentProvider | None = None,
    sse_hub: SSEHub | None = None,
    stale_after_seconds: int = 0,
    now: int | None = None,
) -> dict[str, Any]:
    """
    Polls the PaymentProvider for payments stuck in `created` or `pending` status past `stale_after_seconds`.
    Advances local records cleanly on capture or failure and places pending kitchen orders if 100% captured.
    """
    target_db = Path(db_path or TABLE_SPLIT_DB_PATH)
    migrate_up(target_db)
    ts = int(time.time()) if now is None else int(now)
    orchestrator = PaymentOrchestrator(db_path=target_db, provider=provider, sse_hub=sse_hub)
    active_provider = orchestrator.provider

    cutoff = ts - max(0, int(stale_after_seconds))
    checked_count = 0
    captured_count = 0
    failed_count = 0
    reconciled_payment_ids: list[str] = []
    events_by_session: dict[str, list[dict[str, Any]]] = {}

    conn = get_sqlite_connection(target_db)
    try:
        candidates = conn.execute(
            """
            SELECT p.*, pg.obligation_id, pg.method
            FROM payments p
            JOIN payment_gateway_details pg ON pg.payment_id = p.id
            WHERE p.status IN ('created', 'pending')
              AND p.created_at <= ?
            ORDER BY p.created_at ASC, p.rowid ASC;
            """,
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    for pay_row in candidates:
        checked_count += 1
        order_id = str(pay_row["gateway_order_id"])
        pay_id = str(pay_row["id"])
        session_id = str(pay_row["session_id"])

        try:
            gw_payments = active_provider.fetch_order_payments(order_id)
        except (PaymentError, httpx.HTTPError, RuntimeError) as exc:
            logger.warning("Reconciler gateway poll error for order %s: %s", order_id, exc)
            continue

        if not gw_payments:
            continue

        captured = next(
            (gp for gp in gw_payments if gp.status == "captured" and gp.amount_paise == int(pay_row["amount_paise"])),
            None,
        )
        failed = (
            gw_payments[-1]
            if all(gp.status == "failed" for gp in gw_payments)
            else None
        )

        if captured is None and failed is None:
            continue

        wconn = get_sqlite_connection(target_db)
        try:
            orchestrator._begin_write(wconn)
            try:
                fresh_pay = wconn.execute(
                    """
                    SELECT p.*, pg.obligation_id
                    FROM payments p
                    JOIN payment_gateway_details pg ON pg.payment_id = p.id
                    WHERE p.id = ?;
                    """,
                    (pay_id,),
                ).fetchone()
                if fresh_pay is None or str(fresh_pay["status"]) not in ("created", "pending"):
                    wconn.execute("COMMIT;")
                    continue

                prev_status = str(fresh_pay["status"])
                new_status = prev_status

                if captured is not None:
                    evts = orchestrator._apply_capture_in_conn(
                        wconn,
                        session_id=session_id,
                        payment_row=fresh_pay,
                        gateway_payment_id=captured.gateway_payment_id,
                        method=captured.method,
                        ts=ts,
                    )
                    events_by_session.setdefault(session_id, []).extend(evts)
                    after_row = wconn.execute("SELECT status FROM payments WHERE id = ?;", (pay_id,)).fetchone()
                    new_status = str(after_row["status"]) if after_row else "captured"
                    captured_count += 1

                elif failed is not None:
                    wconn.execute("UPDATE payments SET status = 'failed' WHERE id = ?;", (pay_id,))
                    wconn.execute(
                        """
                        UPDATE payment_gateway_details
                        SET gateway_payment_id = COALESCE(gateway_payment_id, ?),
                            failure_reason = ?, updated_at = ?
                        WHERE payment_id = ?;
                        """,
                        (failed.gateway_payment_id, failed.failure_reason or "RECONCILED_FAILED", ts, pay_id),
                    )
                    wconn.execute(
                        """
                        UPDATE payment_obligations
                        SET status = 'failed', updated_at = ?
                        WHERE id = ? AND status NOT IN ('captured', 'superseded');
                        """,
                        (ts, str(fresh_pay["obligation_id"])),
                    )
                    new_ver = orchestrator._bump_version(wconn, session_id)
                    events_by_session.setdefault(session_id, []).append(
                        orchestrator._append_event(
                            wconn,
                            session_id=session_id,
                            session_version=new_ver,
                            event_type="payment.failed",
                            participant_id=str(fresh_pay["participant_id"]),
                            payload={
                                "payment_id": pay_id,
                                "reason": failed.failure_reason or "RECONCILED_FAILED",
                                "reconciled": True,
                            },
                            now=ts,
                        )
                    )
                    new_status = "failed"
                    failed_count += 1

                wconn.execute(
                    """
                    INSERT INTO payment_reconciliations (
                        id, session_id, payment_id, previous_status, reconciled_status, reconciled_at
                    ) VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    (str(uuid4()), session_id, pay_id, prev_status, new_status, ts),
                )
                reconciled_payment_ids.append(pay_id)
                wconn.execute("COMMIT;")
            except Exception:
                wconn.execute("ROLLBACK;")
                raise
        finally:
            wconn.close()

    if sse_hub is not None:
        for sid, evts in events_by_session.items():
            for evt in evts:
                sse_hub.publish(sid, evt)

    return {
        "checked_count": checked_count,
        "captured_count": captured_count,
        "failed_count": failed_count,
        "reconciled_payment_ids": reconciled_payment_ids,
        "checked_at": ts,
    }

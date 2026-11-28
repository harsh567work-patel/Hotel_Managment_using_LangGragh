"""
Migration 006_stage9_payment_orchestration: Stage 9 Per-Person Payment Orchestration schema.

Adds isolated, reversible payment orchestration tables:
- payment_sessions (frozen window, mode, price re-check digest, host resolution)
- payment_obligations (per-participant frozen paise share or host difference/total)
- payment_gateway_details (1:1 gateway checkout/payment attributes alongside `payments`)
- webhook_events (X-Razorpay-Event-Id deduplication & raw payload audit)
- payment_refunds (per-participant refund ledger)
- kitchen_orders (UNIQUE(session_id) atomic at-most-once kitchen order placement + support ticket ref)
- host_settlements (informational peer IOU & UPI Intent/QR settlement notices in host_pays_all mode)
- payment_reconciliations (scheduled gateway reconciliation audit rows)
"""

from __future__ import annotations

import sqlite3

VERSION = "006"
NAME = "stage9_payment_orchestration"


def upgrade(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS payment_sessions (
            session_id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            payment_mode TEXT NOT NULL DEFAULT 'each_pays_own',
            window_seconds INTEGER NOT NULL DEFAULT 900,
            started_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            frozen_total_paise INTEGER NOT NULL,
            price_recheck_digest TEXT NOT NULL,
            host_resolution TEXT,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(session_id) REFERENCES table_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(proposal_id) REFERENCES proposals(id) ON DELETE CASCADE,
            CONSTRAINT ck_pay_sess_mode_valid CHECK (payment_mode IN ('each_pays_own', 'host_pays_all')),
            CONSTRAINT ck_pay_sess_window_range CHECK (window_seconds >= 60 AND window_seconds <= 1800),
            CONSTRAINT ck_pay_sess_total_non_neg CHECK (frozen_total_paise >= 0),
            CONSTRAINT ck_pay_sess_resolution_valid CHECK (
                host_resolution IS NULL OR host_resolution IN ('cover_unpaid', 'drop_unpaid_re_split', 'cancel_and_refund')
            )
        );

        CREATE TABLE IF NOT EXISTS payment_obligations (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            proposal_id TEXT NOT NULL,
            participant_id TEXT NOT NULL,
            obligation_kind TEXT NOT NULL DEFAULT 'share',
            amount_paise INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'unpaid',
            active_payment_id TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(session_id) REFERENCES table_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(proposal_id) REFERENCES proposals(id) ON DELETE CASCADE,
            FOREIGN KEY(participant_id) REFERENCES participants(id) ON DELETE CASCADE,
            CONSTRAINT ck_obl_kind_valid CHECK (obligation_kind IN ('share', 'host_all', 'host_coverage')),
            CONSTRAINT ck_obl_amount_non_neg CHECK (amount_paise >= 0),
            CONSTRAINT ck_obl_status_valid CHECK (
                status IN ('unpaid', 'pending', 'captured', 'failed', 'refunded', 'superseded')
            )
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pay_obl_unique_role
            ON payment_obligations(session_id, proposal_id, participant_id, obligation_kind);
        CREATE INDEX IF NOT EXISTS idx_pay_obl_session ON payment_obligations(session_id);

        CREATE TABLE IF NOT EXISTS payment_gateway_details (
            payment_id TEXT PRIMARY KEY,
            obligation_id TEXT NOT NULL,
            method TEXT NOT NULL,
            gateway_payment_id TEXT UNIQUE,
            upi_intent_uri TEXT,
            upi_qr_payload TEXT,
            failure_reason TEXT,
            captured_at INTEGER,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(payment_id) REFERENCES payments(id) ON DELETE CASCADE,
            FOREIGN KEY(obligation_id) REFERENCES payment_obligations(id) ON DELETE CASCADE,
            CONSTRAINT ck_pgw_method_valid CHECK (method IN ('upi_intent', 'upi_qr', 'card', 'netbanking'))
        );
        CREATE INDEX IF NOT EXISTS idx_pgw_obligation ON payment_gateway_details(obligation_id);

        CREATE TABLE IF NOT EXISTS webhook_events (
            event_id TEXT PRIMARY KEY,
            gateway TEXT NOT NULL DEFAULT 'razorpay',
            event_type TEXT NOT NULL,
            gateway_order_id TEXT,
            gateway_payment_id TEXT,
            raw_body TEXT NOT NULL,
            signature_verified INTEGER NOT NULL DEFAULT 1,
            processing_status TEXT NOT NULL DEFAULT 'processed',
            received_at INTEGER NOT NULL,
            CONSTRAINT ck_wh_sig_bool CHECK (signature_verified IN (0, 1)),
            CONSTRAINT ck_wh_status_valid CHECK (
                processing_status IN ('processed', 'ignored_stale', 'late_refunded')
            )
        );
        CREATE INDEX IF NOT EXISTS idx_webhook_events_order ON webhook_events(gateway_order_id);

        CREATE TABLE IF NOT EXISTS payment_refunds (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            payment_id TEXT NOT NULL,
            participant_id TEXT NOT NULL,
            gateway_refund_id TEXT UNIQUE NOT NULL,
            amount_paise INTEGER NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'created',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(session_id) REFERENCES table_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(payment_id) REFERENCES payments(id) ON DELETE CASCADE,
            FOREIGN KEY(participant_id) REFERENCES participants(id) ON DELETE CASCADE,
            CONSTRAINT ck_refund_amount_positive CHECK (amount_paise > 0),
            CONSTRAINT ck_refund_status_valid CHECK (status IN ('created', 'pending', 'processed', 'failed'))
        );
        CREATE INDEX IF NOT EXISTS idx_payment_refunds_session ON payment_refunds(session_id);
        CREATE INDEX IF NOT EXISTS idx_payment_refunds_payment ON payment_refunds(payment_id);

        CREATE TABLE IF NOT EXISTS kitchen_orders (
            id TEXT PRIMARY KEY,
            session_id TEXT UNIQUE NOT NULL,
            proposal_id TEXT NOT NULL,
            total_paise INTEGER NOT NULL,
            order_type TEXT NOT NULL DEFAULT 'dine_in',
            kitchen_status TEXT NOT NULL DEFAULT 'pending_acceptance',
            eta_minutes INTEGER NOT NULL DEFAULT 25,
            placed_at INTEGER NOT NULL,
            accepted_at INTEGER,
            items_json TEXT NOT NULL DEFAULT '[]',
            support_ticket_id TEXT,
            FOREIGN KEY(session_id) REFERENCES table_sessions(id) ON DELETE CASCADE,
            CONSTRAINT ck_ko_total_non_neg CHECK (total_paise >= 0),
            CONSTRAINT ck_ko_status_valid CHECK (
                kitchen_status IN ('pending_acceptance', 'accepted', 'cooking', 'served', 'cancelled')
            )
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_kitchen_orders_session ON kitchen_orders(session_id);

        CREATE TABLE IF NOT EXISTS host_settlements (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            proposal_id TEXT NOT NULL,
            participant_id TEXT NOT NULL,
            host_participant_id TEXT NOT NULL,
            amount_paise INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'owes_host',
            informational_only INTEGER NOT NULL DEFAULT 1,
            upi_intent_uri TEXT NOT NULL,
            upi_qr_payload TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(session_id) REFERENCES table_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(participant_id) REFERENCES participants(id) ON DELETE CASCADE,
            FOREIGN KEY(host_participant_id) REFERENCES participants(id) ON DELETE CASCADE,
            CONSTRAINT ck_hs_amount_non_neg CHECK (amount_paise >= 0),
            CONSTRAINT ck_hs_status_valid CHECK (status IN ('owes_host', 'marked_settled')),
            CONSTRAINT ck_hs_info_flag CHECK (informational_only = 1)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_host_settlements_unique
            ON host_settlements(session_id, proposal_id, participant_id);

        CREATE TABLE IF NOT EXISTS payment_reconciliations (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            payment_id TEXT NOT NULL,
            previous_status TEXT NOT NULL,
            reconciled_status TEXT NOT NULL,
            reconciled_at INTEGER NOT NULL,
            FOREIGN KEY(session_id) REFERENCES table_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(payment_id) REFERENCES payments(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_pay_recon_session ON payment_reconciliations(session_id);
        """
    )


def downgrade(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_pay_recon_session;
        DROP TABLE IF EXISTS payment_reconciliations;

        DROP INDEX IF EXISTS idx_host_settlements_unique;
        DROP TABLE IF EXISTS host_settlements;

        DROP INDEX IF EXISTS idx_kitchen_orders_session;
        DROP TABLE IF EXISTS kitchen_orders;

        DROP INDEX IF EXISTS idx_payment_refunds_payment;
        DROP INDEX IF EXISTS idx_payment_refunds_session;
        DROP TABLE IF EXISTS payment_refunds;

        DROP INDEX IF EXISTS idx_webhook_events_order;
        DROP TABLE IF EXISTS webhook_events;

        DROP INDEX IF EXISTS idx_pgw_obligation;
        DROP TABLE IF EXISTS payment_gateway_details;

        DROP INDEX IF EXISTS idx_pay_obl_session;
        DROP INDEX IF EXISTS idx_pay_obl_unique_role;
        DROP TABLE IF EXISTS payment_obligations;

        DROP TABLE IF EXISTS payment_sessions;
        """
    )

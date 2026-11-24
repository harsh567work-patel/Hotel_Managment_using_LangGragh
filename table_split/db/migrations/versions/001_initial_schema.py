"""
Migration 001_initial_schema: Initial domain schema for Table Split Mode.
Implements reversible upgrade() and downgrade() functions.
"""

from __future__ import annotations

import sqlite3

VERSION = "001"
NAME = "initial_table_split_schema"


def upgrade(conn: sqlite3.Connection) -> None:
    """Creates the full normalized Table Split Mode relational schema with CHECK constraints and indices."""
    conn.executescript("""
        -- 1. Menu Item Extensions
        CREATE TABLE IF NOT EXISTS menu_item_extensions (
            item_id TEXT PRIMARY KEY,
            diet_veg TEXT NOT NULL DEFAULT 'UNKNOWN',
            diet_non_veg TEXT NOT NULL DEFAULT 'UNKNOWN',
            diet_egg TEXT NOT NULL DEFAULT 'UNKNOWN',
            diet_vegan TEXT NOT NULL DEFAULT 'UNKNOWN',
            diet_jain TEXT NOT NULL DEFAULT 'UNKNOWN',
            diet_halal TEXT NOT NULL DEFAULT 'UNKNOWN',
            diet_gluten_free TEXT NOT NULL DEFAULT 'UNKNOWN',
            allergen_cereals_gluten TEXT NOT NULL DEFAULT 'UNKNOWN',
            allergen_crustaceans TEXT NOT NULL DEFAULT 'UNKNOWN',
            allergen_milk TEXT NOT NULL DEFAULT 'UNKNOWN',
            allergen_eggs TEXT NOT NULL DEFAULT 'UNKNOWN',
            allergen_fish TEXT NOT NULL DEFAULT 'UNKNOWN',
            allergen_peanuts TEXT NOT NULL DEFAULT 'UNKNOWN',
            allergen_tree_nuts TEXT NOT NULL DEFAULT 'UNKNOWN',
            allergen_soy TEXT NOT NULL DEFAULT 'UNKNOWN',
            allergen_sulphite TEXT NOT NULL DEFAULT 'UNKNOWN',
            contains_dairy TEXT NOT NULL DEFAULT 'UNKNOWN',
            spice_level INTEGER,
            serves INTEGER NOT NULL DEFAULT 1,
            shareable INTEGER NOT NULL DEFAULT 1,
            price_paise INTEGER NOT NULL,
            tax_rate_bps INTEGER NOT NULL DEFAULT 500,
            packaging_paise INTEGER NOT NULL DEFAULT 0,
            verified_by TEXT,
            verified_at INTEGER,
            cross_contact_note TEXT,
            CONSTRAINT ck_spice_level_0_to_4 CHECK (spice_level IS NULL OR (spice_level >= 0 AND spice_level <= 4)),
            CONSTRAINT ck_serves_positive CHECK (serves > 0),
            CONSTRAINT ck_price_paise_non_negative CHECK (price_paise >= 0),
            CONSTRAINT ck_tax_rate_bps_non_negative CHECK (tax_rate_bps >= 0),
            CONSTRAINT ck_packaging_paise_non_negative CHECK (packaging_paise >= 0),
            CONSTRAINT ck_diet_tristate CHECK (
                diet_veg IN ('YES', 'NO', 'UNKNOWN') AND
                diet_non_veg IN ('YES', 'NO', 'UNKNOWN') AND
                diet_egg IN ('YES', 'NO', 'UNKNOWN') AND
                diet_vegan IN ('YES', 'NO', 'UNKNOWN') AND
                diet_jain IN ('YES', 'NO', 'UNKNOWN') AND
                diet_halal IN ('YES', 'NO', 'UNKNOWN') AND
                diet_gluten_free IN ('YES', 'NO', 'UNKNOWN')
            ),
            CONSTRAINT ck_allergen_tristate CHECK (
                allergen_cereals_gluten IN ('YES', 'NO', 'UNKNOWN') AND
                allergen_crustaceans IN ('YES', 'NO', 'UNKNOWN') AND
                allergen_milk IN ('YES', 'NO', 'UNKNOWN') AND
                allergen_eggs IN ('YES', 'NO', 'UNKNOWN') AND
                allergen_fish IN ('YES', 'NO', 'UNKNOWN') AND
                allergen_peanuts IN ('YES', 'NO', 'UNKNOWN') AND
                allergen_tree_nuts IN ('YES', 'NO', 'UNKNOWN') AND
                allergen_soy IN ('YES', 'NO', 'UNKNOWN') AND
                allergen_sulphite IN ('YES', 'NO', 'UNKNOWN') AND
                contains_dairy IN ('YES', 'NO', 'UNKNOWN')
            ),
            CONSTRAINT ck_milk_implies_dairy CHECK (allergen_milk != 'YES' OR contains_dairy = 'YES')
        );

        -- 2. Table Sessions
        CREATE TABLE IF NOT EXISTS table_sessions (
            id TEXT PRIMARY KEY,
            restaurant_id TEXT NOT NULL DEFAULT 'spiceroute_default',
            host_participant_id TEXT,
            status TEXT NOT NULL DEFAULT 'collecting',
            join_token_hash TEXT UNIQUE NOT NULL,
            requires_host_approval INTEGER NOT NULL DEFAULT 0,
            max_participants INTEGER NOT NULL DEFAULT 12,
            deadline_at INTEGER,
            expires_at INTEGER NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            split_mode TEXT NOT NULL DEFAULT 'by_consumption',
            payment_mode TEXT NOT NULL DEFAULT 'each_pays_own',
            delivery_address_id TEXT,
            created_at INTEGER NOT NULL,
            CONSTRAINT ck_max_participants_positive CHECK (max_participants > 0),
            CONSTRAINT ck_session_version_positive CHECK (version >= 1),
            CONSTRAINT ck_session_status_valid CHECK (status IN ('draft', 'collecting', 'proposing', 'locked', 'paying', 'placed', 'cancelled', 'expired')),
            CONSTRAINT ck_split_mode_valid CHECK (split_mode IN ('by_consumption', 'equal')),
            CONSTRAINT ck_payment_mode_valid CHECK (payment_mode IN ('each_pays_own', 'host_pays_all'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_table_sessions_join_token_hash ON table_sessions(join_token_hash);

        -- 3. Participants
        CREATE TABLE IF NOT EXISTS participants (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            user_id TEXT,
            guest_token_hash TEXT,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',
            status TEXT NOT NULL DEFAULT 'active',
            is_ready INTEGER NOT NULL DEFAULT 0,
            joined_at INTEGER NOT NULL,
            FOREIGN KEY(session_id) REFERENCES table_sessions(id) ON DELETE CASCADE,
            CONSTRAINT ck_participant_role_valid CHECK (role IN ('host', 'member')),
            CONSTRAINT ck_participant_status_valid CHECK (status IN ('pending', 'active', 'left', 'removed'))
        );
        CREATE INDEX IF NOT EXISTS idx_participants_session_id ON participants(session_id);
        CREATE INDEX IF NOT EXISTS idx_participants_guest_token_hash ON participants(guest_token_hash);

        -- 4. Preferences
        CREATE TABLE IF NOT EXISTS preferences (
            id TEXT PRIMARY KEY,
            participant_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            strength TEXT NOT NULL,
            weight INTEGER NOT NULL DEFAULT 1,
            value TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'chip',
            raw_text TEXT,
            parser_confidence REAL,
            confirmed_by_user INTEGER NOT NULL DEFAULT 1,
            is_private INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(participant_id) REFERENCES participants(id) ON DELETE CASCADE,
            CONSTRAINT ck_preference_kind_valid CHECK (kind IN ('diet', 'allergen', 'budget_max_paise', 'spice_min', 'spice_max', 'cuisine_like', 'dislike_item', 'must_include_category', 'must_include_item', 'freeform')),
            CONSTRAINT ck_preference_strength_valid CHECK (strength IN ('HARD', 'SOFT')),
            CONSTRAINT ck_preference_weight_range CHECK (weight >= 1 AND weight <= 5),
            CONSTRAINT ck_preference_source_valid CHECK (source IN ('chip', 'freetext_parsed', 'host_set')),
            CONSTRAINT ck_diet_allergen_must_be_hard CHECK ((kind NOT IN ('diet', 'allergen')) OR (strength = 'HARD'))
        );
        CREATE INDEX IF NOT EXISTS idx_preferences_participant_id ON preferences(participant_id);

        -- 5. Proposals
        CREATE TABLE IF NOT EXISTS proposals (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            session_version INTEGER NOT NULL,
            engine_version TEXT NOT NULL DEFAULT '1.0.0',
            status TEXT NOT NULL DEFAULT 'draft',
            constraints_hash TEXT NOT NULL,
            explanation_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL,
            FOREIGN KEY(session_id) REFERENCES table_sessions(id) ON DELETE CASCADE,
            CONSTRAINT ck_proposal_session_version_positive CHECK (session_version >= 1),
            CONSTRAINT ck_proposal_status_valid CHECK (status IN ('draft', 'active', 'superseded', 'approved'))
        );
        CREATE INDEX IF NOT EXISTS idx_proposals_session_id ON proposals(session_id);

        -- 6. Proposal Lines
        CREATE TABLE IF NOT EXISTS proposal_lines (
            id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            menu_item_id TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'SHARED',
            consumer_participant_ids TEXT NOT NULL DEFAULT '[]',
            unit_price_paise INTEGER NOT NULL,
            FOREIGN KEY(proposal_id) REFERENCES proposals(id) ON DELETE CASCADE,
            CONSTRAINT ck_proposal_line_quantity_positive CHECK (quantity > 0),
            CONSTRAINT ck_proposal_line_unit_price_paise_non_negative CHECK (unit_price_paise >= 0),
            CONSTRAINT ck_proposal_line_kind_valid CHECK (kind IN ('SHARED', 'PERSONAL'))
        );
        CREATE INDEX IF NOT EXISTS idx_proposal_lines_proposal_id ON proposal_lines(proposal_id);
        CREATE INDEX IF NOT EXISTS idx_proposal_lines_menu_item_id ON proposal_lines(menu_item_id);

        -- 7. Allocations
        CREATE TABLE IF NOT EXISTS allocations (
            id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            participant_id TEXT NOT NULL,
            items_paise INTEGER NOT NULL DEFAULT 0,
            tax_paise INTEGER NOT NULL DEFAULT 0,
            delivery_share_paise INTEGER NOT NULL DEFAULT 0,
            packaging_share_paise INTEGER NOT NULL DEFAULT 0,
            discount_share_paise INTEGER NOT NULL DEFAULT 0,
            rounding_paise INTEGER NOT NULL DEFAULT 0,
            total_paise INTEGER NOT NULL,
            FOREIGN KEY(proposal_id) REFERENCES proposals(id) ON DELETE CASCADE,
            FOREIGN KEY(participant_id) REFERENCES participants(id) ON DELETE CASCADE,
            CONSTRAINT ck_alloc_items_paise_non_negative CHECK (items_paise >= 0),
            CONSTRAINT ck_alloc_tax_paise_non_negative CHECK (tax_paise >= 0),
            CONSTRAINT ck_alloc_delivery_share_paise_non_negative CHECK (delivery_share_paise >= 0),
            CONSTRAINT ck_alloc_packaging_share_paise_non_negative CHECK (packaging_share_paise >= 0),
            CONSTRAINT ck_alloc_discount_share_paise_non_negative CHECK (discount_share_paise >= 0),
            CONSTRAINT ck_alloc_total_paise_non_negative CHECK (total_paise >= 0)
        );
        CREATE INDEX IF NOT EXISTS idx_allocations_proposal_id ON allocations(proposal_id);
        CREATE INDEX IF NOT EXISTS idx_allocations_participant_id ON allocations(participant_id);

        -- 8. Payments
        CREATE TABLE IF NOT EXISTS payments (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            participant_id TEXT NOT NULL,
            gateway TEXT NOT NULL,
            gateway_order_id TEXT NOT NULL,
            amount_paise INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'created',
            idempotency_key TEXT UNIQUE NOT NULL,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(session_id) REFERENCES table_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(participant_id) REFERENCES participants(id) ON DELETE CASCADE,
            CONSTRAINT ck_payment_amount_paise_non_negative CHECK (amount_paise >= 0),
            CONSTRAINT ck_payment_status_valid CHECK (status IN ('created', 'pending', 'captured', 'failed', 'refunded', 'partially_refunded'))
        );
        CREATE INDEX IF NOT EXISTS idx_payments_session_id ON payments(session_id);
        CREATE INDEX IF NOT EXISTS idx_payments_participant_id ON payments(participant_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_idempotency_key ON payments(idempotency_key);

        -- 9. Session Events (Append-only Audit Log)
        CREATE TABLE IF NOT EXISTS session_events (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            participant_id TEXT,
            type TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL,
            FOREIGN KEY(session_id) REFERENCES table_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(participant_id) REFERENCES participants(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_session_events_session_id ON session_events(session_id);
        CREATE INDEX IF NOT EXISTS idx_session_events_created_at ON session_events(created_at);
    """)


def downgrade(conn: sqlite3.Connection) -> None:
    """Drops all tables created in upgrade() in reverse dependency order."""
    conn.executescript("""
        DROP TABLE IF EXISTS session_events;
        DROP TABLE IF EXISTS payments;
        DROP TABLE IF EXISTS allocations;
        DROP TABLE IF EXISTS proposal_lines;
        DROP TABLE IF EXISTS proposals;
        DROP TABLE IF EXISTS preferences;
        DROP TABLE IF EXISTS participants;
        DROP TABLE IF EXISTS table_sessions;
        DROP TABLE IF EXISTS menu_item_extensions;
    """)

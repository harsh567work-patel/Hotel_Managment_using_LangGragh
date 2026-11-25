"""
Migration 002_stage3_dietary_columns: Adds contains_honey, allergen_mustard, and sales_volume
to menu_item_extensions. Implements full reversible upgrade() and downgrade().
"""

from __future__ import annotations

import sqlite3

VERSION = "002"
NAME = "stage3_dietary_columns"


def upgrade(conn: sqlite3.Connection) -> None:
    """Adds Stage 3 dietary & reporting columns with strict CHECK constraints."""
    conn.executescript("""
        ALTER TABLE menu_item_extensions
            ADD COLUMN contains_honey TEXT NOT NULL DEFAULT 'UNKNOWN'
            CHECK (contains_honey IN ('YES', 'NO', 'UNKNOWN'));

        ALTER TABLE menu_item_extensions
            ADD COLUMN allergen_mustard TEXT NOT NULL DEFAULT 'UNKNOWN'
            CHECK (allergen_mustard IN ('YES', 'NO', 'UNKNOWN'));

        ALTER TABLE menu_item_extensions
            ADD COLUMN sales_volume INTEGER NOT NULL DEFAULT 0
            CHECK (sales_volume >= 0);
    """)


def downgrade(conn: sqlite3.Connection) -> None:
    """Rebuilds menu_item_extensions cleanly to Migration 001 schema (dropping Stage 3 columns)."""
    conn.executescript("""
        CREATE TABLE menu_item_extensions_v1 (
            item_id TEXT PRIMARY KEY,
            diet_veg TEXT NOT NULL DEFAULT 'UNKNOWN',
            diet_non_veg TEXT NOT NULL DEFAULT 'UNKNOWN',
            diet_egg TEXT NOT NULL DEFAULT 'UNKNOWN',
            diet_vegan TEXT NOT NULL DEFAULT 'UNKNOWN',
            diet_jain TEXT NOT NULL DEFAULT 'UNKNOWN',
            diet_halal TEXT NOT NULL DEFAULT 'UNKNOWN',
            diet_gluten_free TEXT NOT NULL DEFAULT 'UNKNOWN',
            contains_onion_garlic TEXT NOT NULL DEFAULT 'UNKNOWN',
            contains_root_veg TEXT NOT NULL DEFAULT 'UNKNOWN',
            allergen_cereals_gluten TEXT NOT NULL DEFAULT 'UNKNOWN',
            allergen_crustaceans TEXT NOT NULL DEFAULT 'UNKNOWN',
            allergen_milk TEXT NOT NULL DEFAULT 'UNKNOWN',
            allergen_eggs TEXT NOT NULL DEFAULT 'UNKNOWN',
            allergen_fish TEXT NOT NULL DEFAULT 'UNKNOWN',
            allergen_peanuts TEXT NOT NULL DEFAULT 'UNKNOWN',
            allergen_tree_nuts TEXT NOT NULL DEFAULT 'UNKNOWN',
            allergen_soy TEXT NOT NULL DEFAULT 'UNKNOWN',
            allergen_sulphite TEXT NOT NULL DEFAULT 'UNKNOWN',
            allergen_sesame TEXT NOT NULL DEFAULT 'UNKNOWN',
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
                diet_gluten_free IN ('YES', 'NO', 'UNKNOWN') AND
                contains_onion_garlic IN ('YES', 'NO', 'UNKNOWN') AND
                contains_root_veg IN ('YES', 'NO', 'UNKNOWN')
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
                allergen_sesame IN ('YES', 'NO', 'UNKNOWN') AND
                contains_dairy IN ('YES', 'NO', 'UNKNOWN')
            ),
            CONSTRAINT ck_milk_implies_dairy CHECK (allergen_milk != 'YES' OR contains_dairy = 'YES'),
            CONSTRAINT ck_vegan_implies_no_animal_products CHECK (
                diet_vegan != 'YES' OR (
                    diet_egg = 'NO' AND
                    allergen_milk = 'NO' AND
                    contains_dairy = 'NO' AND
                    diet_non_veg != 'YES'
                )
            ),
            CONSTRAINT ck_vegan_and_non_veg_exclusive CHECK (
                NOT (diet_vegan = 'YES' AND diet_non_veg = 'YES')
            ),
            CONSTRAINT ck_jain_rules CHECK (
                diet_jain != 'YES' OR (
                    contains_onion_garlic != 'YES' AND
                    contains_root_veg != 'YES' AND
                    diet_non_veg != 'YES'
                )
            )
        );

        INSERT INTO menu_item_extensions_v1 (
            item_id, diet_veg, diet_non_veg, diet_egg, diet_vegan, diet_jain, diet_halal, diet_gluten_free,
            contains_onion_garlic, contains_root_veg,
            allergen_cereals_gluten, allergen_crustaceans, allergen_milk, allergen_eggs, allergen_fish,
            allergen_peanuts, allergen_tree_nuts, allergen_soy, allergen_sulphite, allergen_sesame, contains_dairy,
            spice_level, serves, shareable, price_paise, tax_rate_bps, packaging_paise,
            verified_by, verified_at, cross_contact_note
        )
        SELECT
            item_id, diet_veg, diet_non_veg, diet_egg, diet_vegan, diet_jain, diet_halal, diet_gluten_free,
            contains_onion_garlic, contains_root_veg,
            allergen_cereals_gluten, allergen_crustaceans, allergen_milk, allergen_eggs, allergen_fish,
            allergen_peanuts, allergen_tree_nuts, allergen_soy, allergen_sulphite, allergen_sesame, contains_dairy,
            spice_level, serves, shareable, price_paise, tax_rate_bps, packaging_paise,
            verified_by, verified_at, cross_contact_note
        FROM menu_item_extensions;

        DROP TABLE menu_item_extensions;
        ALTER TABLE menu_item_extensions_v1 RENAME TO menu_item_extensions;
    """)

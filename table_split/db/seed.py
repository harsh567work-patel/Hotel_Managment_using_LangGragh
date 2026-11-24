"""
table_split.db.seed — Seed script providing 12 sample menu item extensions.

Guard & Integrity Features:
- Refuses execution unless ALLOW_SEED=1 environment variable is explicitly set.
- All prices are dynamically derived from data/menu.json (single source of truth).
- All verified_by and verified_at attributes set to None (sample fixture, unverified).
- Halal status set to UNKNOWN (cannot be justified from unverified catalog data).
- Includes Jain flags (contains_onion_garlic, contains_root_veg) and non-statutory allergen_sesame.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from config import TABLE_SPLIT_DB_PATH
from table_split.db.engine import get_sqlite_connection
from table_split.db.models import derive_dairy_status
from table_split.enums import TriState

logger = logging.getLogger(__name__)

MENU_JSON_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "menu.json"


def load_menu_json_prices() -> dict[str, int]:
    """Derives item price in paise directly from data/menu.json (rupees * 100)."""
    if not MENU_JSON_PATH.exists():
        raise FileNotFoundError(f"Missing master catalog at {MENU_JSON_PATH}")
    with open(MENU_JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return {item["id"]: int(item["price"]) * 100 for item in data["menu"]}


# 12 Sample Items (sample fixture, unverified)
SAMPLE_MENU_EXTENSIONS: list[dict[str, Any]] = [
    # 1. Vegan Bowl: 100% plant-based, dairy NO, vegan YES, sesame YES (tahini)
    {
        "item_id": "D039",
        "name": "Superfood Vegan Buddha Bowl",
        "diet_veg": TriState.YES.value,
        "diet_non_veg": TriState.NO.value,
        "diet_egg": TriState.NO.value,
        "diet_vegan": TriState.YES.value,
        "diet_jain": TriState.YES.value,
        "diet_halal": TriState.UNKNOWN.value,  # Unverified
        "diet_gluten_free": TriState.YES.value,
        "contains_onion_garlic": TriState.NO.value,
        "contains_root_veg": TriState.NO.value,
        "allergen_cereals_gluten": TriState.NO.value,
        "allergen_crustaceans": TriState.NO.value,
        "allergen_milk": TriState.NO.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.NO.value,
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.NO.value,
        "allergen_sesame": TriState.YES.value,  # Tahini
        "contains_dairy": TriState.NO.value,
        "spice_level": 1,
        "serves": 1,
        "shareable": 0,
        "tax_rate_bps": 500,
        "packaging_paise": 1500,
        "verified_by": None,  # Sample fixture, unverified
        "verified_at": None,
        "cross_contact_note": "Sample fixture, unverified kitchen notes",
    },
    # 2. Paneer Pizza: Dairy YES, veg YES
    {
        "item_id": "D037",
        "name": "Tandoori Paneer Pizza",
        "diet_veg": TriState.YES.value,
        "diet_non_veg": TriState.NO.value,
        "diet_egg": TriState.NO.value,
        "diet_vegan": TriState.NO.value,
        "diet_jain": TriState.NO.value,
        "diet_halal": TriState.UNKNOWN.value,
        "diet_gluten_free": TriState.NO.value,
        "contains_onion_garlic": TriState.YES.value,
        "contains_root_veg": TriState.YES.value,
        "allergen_cereals_gluten": TriState.YES.value,
        "allergen_crustaceans": TriState.NO.value,
        "allergen_milk": TriState.YES.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.NO.value,
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.NO.value,
        "allergen_sesame": TriState.NO.value,
        "contains_dairy": TriState.YES.value,
        "spice_level": 2,
        "serves": 2,
        "shareable": 1,
        "tax_rate_bps": 500,
        "packaging_paise": 2500,
        "verified_by": None,
        "verified_at": None,
        "cross_contact_note": "Sample fixture, unverified",
    },
    # 3. Garlic Bread: Dairy YES, gluten YES
    {
        "item_id": "D038",
        "name": "Artisan Garlic Bread",
        "diet_veg": TriState.YES.value,
        "diet_non_veg": TriState.NO.value,
        "diet_egg": TriState.NO.value,
        "diet_vegan": TriState.NO.value,
        "diet_jain": TriState.NO.value,
        "diet_halal": TriState.UNKNOWN.value,
        "diet_gluten_free": TriState.NO.value,
        "contains_onion_garlic": TriState.YES.value,
        "contains_root_veg": TriState.YES.value,
        "allergen_cereals_gluten": TriState.YES.value,
        "allergen_crustaceans": TriState.NO.value,
        "allergen_milk": TriState.YES.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.NO.value,
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.NO.value,
        "allergen_sesame": TriState.NO.value,
        "contains_dairy": TriState.YES.value,
        "spice_level": 1,
        "serves": 2,
        "shareable": 1,
        "tax_rate_bps": 500,
        "packaging_paise": 1000,
        "verified_by": None,
        "verified_at": None,
        "cross_contact_note": "Sample fixture, unverified",
    },
    # 4. Spicy Pizza: Spice level 4
    {
        "item_id": "D040",
        "name": "Fiery Ghost Pepper Pizza",
        "diet_veg": TriState.YES.value,
        "diet_non_veg": TriState.NO.value,
        "diet_egg": TriState.NO.value,
        "diet_vegan": TriState.NO.value,
        "diet_jain": TriState.NO.value,
        "diet_halal": TriState.UNKNOWN.value,
        "diet_gluten_free": TriState.NO.value,
        "contains_onion_garlic": TriState.YES.value,
        "contains_root_veg": TriState.YES.value,
        "allergen_cereals_gluten": TriState.YES.value,
        "allergen_crustaceans": TriState.NO.value,
        "allergen_milk": TriState.YES.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.NO.value,
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.NO.value,
        "allergen_sesame": TriState.NO.value,
        "contains_dairy": TriState.YES.value,
        "spice_level": 4,  # MAX SPICE
        "serves": 2,
        "shareable": 1,
        "tax_rate_bps": 500,
        "packaging_paise": 2500,
        "verified_by": None,
        "verified_at": None,
        "cross_contact_note": "Sample fixture, unverified",
    },
    # 5. D001: Butter Chicken (Non-veg, dairy YES)
    {
        "item_id": "D001",
        "name": "Butter Chicken",
        "diet_veg": TriState.NO.value,
        "diet_non_veg": TriState.YES.value,
        "diet_egg": TriState.NO.value,
        "diet_vegan": TriState.NO.value,
        "diet_jain": TriState.NO.value,
        "diet_halal": TriState.UNKNOWN.value,
        "diet_gluten_free": TriState.YES.value,
        "contains_onion_garlic": TriState.YES.value,
        "contains_root_veg": TriState.YES.value,
        "allergen_cereals_gluten": TriState.NO.value,
        "allergen_crustaceans": TriState.NO.value,
        "allergen_milk": TriState.YES.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.YES.value,
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.NO.value,
        "allergen_sesame": TriState.NO.value,
        "contains_dairy": TriState.YES.value,
        "spice_level": 2,
        "serves": 2,
        "shareable": 1,
        "tax_rate_bps": 500,
        "packaging_paise": 2000,
        "verified_by": None,
        "verified_at": None,
        "cross_contact_note": "Sample fixture, unverified",
    },
    # 6. D002: Paneer Tikka Masala (Veg, dairy YES)
    {
        "item_id": "D002",
        "name": "Paneer Tikka Masala",
        "diet_veg": TriState.YES.value,
        "diet_non_veg": TriState.NO.value,
        "diet_egg": TriState.NO.value,
        "diet_vegan": TriState.NO.value,
        "diet_jain": TriState.NO.value,
        "diet_halal": TriState.UNKNOWN.value,
        "diet_gluten_free": TriState.YES.value,
        "contains_onion_garlic": TriState.YES.value,
        "contains_root_veg": TriState.YES.value,
        "allergen_cereals_gluten": TriState.NO.value,
        "allergen_crustaceans": TriState.NO.value,
        "allergen_milk": TriState.YES.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.YES.value,
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.NO.value,
        "allergen_sesame": TriState.NO.value,
        "contains_dairy": TriState.YES.value,
        "spice_level": 2,
        "serves": 2,
        "shareable": 1,
        "tax_rate_bps": 500,
        "packaging_paise": 2000,
        "verified_by": None,
        "verified_at": None,
        "cross_contact_note": "Sample fixture, unverified",
    },
    # 7. D005: Garlic Naan (Gluten YES, Dairy YES)
    {
        "item_id": "D005",
        "name": "Garlic Naan",
        "diet_veg": TriState.YES.value,
        "diet_non_veg": TriState.NO.value,
        "diet_egg": TriState.NO.value,
        "diet_vegan": TriState.NO.value,
        "diet_jain": TriState.NO.value,
        "diet_halal": TriState.UNKNOWN.value,
        "diet_gluten_free": TriState.NO.value,
        "contains_onion_garlic": TriState.YES.value,
        "contains_root_veg": TriState.YES.value,
        "allergen_cereals_gluten": TriState.YES.value,
        "allergen_crustaceans": TriState.NO.value,
        "allergen_milk": TriState.YES.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.NO.value,
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.NO.value,
        "allergen_sesame": TriState.NO.value,
        "contains_dairy": TriState.YES.value,
        "spice_level": 0,
        "serves": 1,
        "shareable": 1,
        "tax_rate_bps": 500,
        "packaging_paise": 500,
        "verified_by": None,
        "verified_at": None,
        "cross_contact_note": "Sample fixture, unverified",
    },
    # 8. D008: Veg Samosa — DELIBERATELY UNKNOWN allergens
    {
        "item_id": "D008",
        "name": "Veg Samosa (2 pcs)",
        "diet_veg": TriState.YES.value,
        "diet_non_veg": TriState.NO.value,
        "diet_egg": TriState.NO.value,
        "diet_vegan": TriState.UNKNOWN.value,
        "diet_jain": TriState.NO.value,
        "diet_halal": TriState.UNKNOWN.value,
        "diet_gluten_free": TriState.NO.value,
        "contains_onion_garlic": TriState.UNKNOWN.value,
        "contains_root_veg": TriState.YES.value,
        "allergen_cereals_gluten": TriState.YES.value,
        "allergen_crustaceans": TriState.NO.value,
        "allergen_milk": TriState.UNKNOWN.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.UNKNOWN.value,
        "allergen_tree_nuts": TriState.UNKNOWN.value,
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.UNKNOWN.value,
        "allergen_sesame": TriState.UNKNOWN.value,
        "contains_dairy": TriState.UNKNOWN.value,
        "spice_level": 2,
        "serves": 1,
        "shareable": 1,
        "tax_rate_bps": 500,
        "packaging_paise": 800,
        "verified_by": None,
        "verified_at": None,
        "cross_contact_note": "Sample fixture, unverified",
    },
    # 9. D012: Gulab Jamun (Dairy YES, Tree nuts YES, Jain YES)
    {
        "item_id": "D012",
        "name": "Gulab Jamun (3 pcs)",
        "diet_veg": TriState.YES.value,
        "diet_non_veg": TriState.NO.value,
        "diet_egg": TriState.NO.value,
        "diet_vegan": TriState.NO.value,
        "diet_jain": TriState.YES.value,
        "diet_halal": TriState.UNKNOWN.value,
        "diet_gluten_free": TriState.NO.value,
        "contains_onion_garlic": TriState.NO.value,
        "contains_root_veg": TriState.NO.value,
        "allergen_cereals_gluten": TriState.YES.value,
        "allergen_crustaceans": TriState.NO.value,
        "allergen_milk": TriState.YES.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.YES.value,
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.NO.value,
        "allergen_sesame": TriState.NO.value,
        "contains_dairy": TriState.YES.value,
        "spice_level": 0,
        "serves": 2,
        "shareable": 1,
        "tax_rate_bps": 500,
        "packaging_paise": 1000,
        "verified_by": None,
        "verified_at": None,
        "cross_contact_note": "Sample fixture, unverified",
    },
    # 10. D020: Tandoori Prawns (Crustaceans YES)
    {
        "item_id": "D020",
        "name": "Tandoori Prawns",
        "diet_veg": TriState.NO.value,
        "diet_non_veg": TriState.YES.value,
        "diet_egg": TriState.NO.value,
        "diet_vegan": TriState.NO.value,
        "diet_jain": TriState.NO.value,
        "diet_halal": TriState.UNKNOWN.value,
        "diet_gluten_free": TriState.YES.value,
        "contains_onion_garlic": TriState.YES.value,
        "contains_root_veg": TriState.YES.value,
        "allergen_cereals_gluten": TriState.NO.value,
        "allergen_crustaceans": TriState.YES.value,
        "allergen_milk": TriState.YES.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.NO.value,
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.NO.value,
        "allergen_sesame": TriState.NO.value,
        "contains_dairy": TriState.YES.value,
        "spice_level": 3,
        "serves": 2,
        "shareable": 1,
        "tax_rate_bps": 500,
        "packaging_paise": 2500,
        "verified_by": None,
        "verified_at": None,
        "cross_contact_note": "Sample fixture, unverified",
    },
    # 11. D022: Chana Masala — Plant-based, DELIBERATELY UNKNOWN soy & sulphite
    {
        "item_id": "D022",
        "name": "Chana Masala",
        "diet_veg": TriState.YES.value,
        "diet_non_veg": TriState.NO.value,
        "diet_egg": TriState.NO.value,
        "diet_vegan": TriState.YES.value,
        "diet_jain": TriState.NO.value,
        "diet_halal": TriState.UNKNOWN.value,
        "diet_gluten_free": TriState.YES.value,
        "contains_onion_garlic": TriState.YES.value,
        "contains_root_veg": TriState.YES.value,
        "allergen_cereals_gluten": TriState.NO.value,
        "allergen_crustaceans": TriState.NO.value,
        "allergen_milk": TriState.NO.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.NO.value,
        "allergen_soy": TriState.UNKNOWN.value,
        "allergen_sulphite": TriState.UNKNOWN.value,
        "allergen_sesame": TriState.UNKNOWN.value,
        "contains_dairy": TriState.NO.value,
        "spice_level": 2,
        "serves": 2,
        "shareable": 1,
        "tax_rate_bps": 500,
        "packaging_paise": 1800,
        "verified_by": None,
        "verified_at": None,
        "cross_contact_note": "Sample fixture, unverified",
    },
    # 12. D025: Malabar Fish Curry (Fish YES, Crustaceans UNKNOWN)
    {
        "item_id": "D025",
        "name": "Malabar Fish Curry",
        "diet_veg": TriState.NO.value,
        "diet_non_veg": TriState.YES.value,
        "diet_egg": TriState.NO.value,
        "diet_vegan": TriState.NO.value,
        "diet_jain": TriState.NO.value,
        "diet_halal": TriState.UNKNOWN.value,
        "diet_gluten_free": TriState.YES.value,
        "contains_onion_garlic": TriState.YES.value,
        "contains_root_veg": TriState.YES.value,
        "allergen_cereals_gluten": TriState.NO.value,
        "allergen_crustaceans": TriState.UNKNOWN.value,
        "allergen_milk": TriState.NO.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.YES.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.NO.value,
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.NO.value,
        "allergen_sesame": TriState.NO.value,
        "contains_dairy": TriState.NO.value,
        "spice_level": 3,
        "serves": 2,
        "shareable": 1,
        "tax_rate_bps": 500,
        "packaging_paise": 2000,
        "verified_by": None,
        "verified_at": None,
        "cross_contact_note": "Sample fixture, unverified",
    },
]


def seed_menu_item_extensions(db_path: str | None = None) -> int:
    """
    Inserts sample unverified menu item extensions.
    Refuses execution unless ALLOW_SEED=1 environment variable is explicitly set.
    """
    if os.environ.get("ALLOW_SEED") != "1":
        raise PermissionError(
            "Seeding refused: ALLOW_SEED=1 environment variable must be set explicitly to populate fixture data."
        )

    prices_map = load_menu_json_prices()
    conn = get_sqlite_connection(db_path)
    count = 0

    try:
        with conn:
            for item in SAMPLE_MENU_EXTENSIONS:
                item_id = item["item_id"]
                if item_id not in prices_map:
                    raise KeyError(f"Price for item {item_id} not found in master catalog {MENU_JSON_PATH}")

                # Price derived directly from master catalog
                price_paise = prices_map[item_id]

                # Derived rule verification
                derived_dairy = derive_dairy_status(
                    item["allergen_milk"], item.get("diet_vegan", TriState.UNKNOWN.value)
                ).value

                contains_dairy = item["contains_dairy"]
                if item["allergen_milk"] == TriState.YES.value and contains_dairy != TriState.YES.value:
                    contains_dairy = TriState.YES.value
                elif contains_dairy == TriState.UNKNOWN.value and derived_dairy != TriState.UNKNOWN.value:
                    contains_dairy = derived_dairy

                record = {
                    **item,
                    "price_paise": price_paise,
                    "contains_dairy": contains_dairy,
                }

                conn.execute(
                    """
                    INSERT INTO menu_item_extensions (
                        item_id, diet_veg, diet_non_veg, diet_egg, diet_vegan, diet_jain, diet_halal, diet_gluten_free,
                        contains_onion_garlic, contains_root_veg,
                        allergen_cereals_gluten, allergen_crustaceans, allergen_milk, allergen_eggs, allergen_fish,
                        allergen_peanuts, allergen_tree_nuts, allergen_soy, allergen_sulphite, allergen_sesame, contains_dairy,
                        spice_level, serves, shareable, price_paise, tax_rate_bps, packaging_paise,
                        verified_by, verified_at, cross_contact_note
                    ) VALUES (
                        :item_id, :diet_veg, :diet_non_veg, :diet_egg, :diet_vegan, :diet_jain, :diet_halal, :diet_gluten_free,
                        :contains_onion_garlic, :contains_root_veg,
                        :allergen_cereals_gluten, :allergen_crustaceans, :allergen_milk, :allergen_eggs, :allergen_fish,
                        :allergen_peanuts, :allergen_tree_nuts, :allergen_soy, :allergen_sulphite, :allergen_sesame, :contains_dairy,
                        :spice_level, :serves, :shareable, :price_paise, :tax_rate_bps, :packaging_paise,
                        :verified_by, :verified_at, :cross_contact_note
                    )
                    ON CONFLICT(item_id) DO UPDATE SET
                        diet_veg=excluded.diet_veg,
                        diet_non_veg=excluded.diet_non_veg,
                        diet_egg=excluded.diet_egg,
                        diet_vegan=excluded.diet_vegan,
                        diet_jain=excluded.diet_jain,
                        diet_halal=excluded.diet_halal,
                        diet_gluten_free=excluded.diet_gluten_free,
                        contains_onion_garlic=excluded.contains_onion_garlic,
                        contains_root_veg=excluded.contains_root_veg,
                        allergen_cereals_gluten=excluded.allergen_cereals_gluten,
                        allergen_crustaceans=excluded.allergen_crustaceans,
                        allergen_milk=excluded.allergen_milk,
                        allergen_eggs=excluded.allergen_eggs,
                        allergen_fish=excluded.allergen_fish,
                        allergen_peanuts=excluded.allergen_peanuts,
                        allergen_tree_nuts=excluded.allergen_tree_nuts,
                        allergen_soy=excluded.allergen_soy,
                        allergen_sulphite=excluded.allergen_sulphite,
                        allergen_sesame=excluded.allergen_sesame,
                        contains_dairy=excluded.contains_dairy,
                        spice_level=excluded.spice_level,
                        serves=excluded.serves,
                        shareable=excluded.shareable,
                        price_paise=excluded.price_paise,
                        tax_rate_bps=excluded.tax_rate_bps,
                        packaging_paise=excluded.packaging_paise,
                        verified_by=excluded.verified_by,
                        verified_at=excluded.verified_at,
                        cross_contact_note=excluded.cross_contact_note;
                    """,
                    record,
                )
                count += 1
        logger.info("Successfully seeded %d sample fixture menu item extensions", count)
        return count
    finally:
        conn.close()


if __name__ == "__main__":
    print(f"Seeding sample fixture menu item extensions into {TABLE_SPLIT_DB_PATH}...")
    n = seed_menu_item_extensions()
    print(f"Done: {n} items seeded.")

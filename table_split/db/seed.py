"""
table_split.db.seed — Seed script providing 12 sample menu item extensions.

Requirements met:
- Realistic diet and FSSAI 2020 allergen declarations.
- Deliberately UNKNOWN fields to test fail-safe constraint engine behavior.
- One vegan bowl (Superfood Vegan Buddha Bowl, dairy NO, vegan YES).
- One paneer pizza (Spicy Paneer Pizza, dairy YES).
- One garlic bread (Artisan Garlic Bread, dairy YES).
- One spicy pizza (Fiery Ghost Pepper Pizza, spice_level 4).
"""

from __future__ import annotations

import logging
from typing import Any

from config import TABLE_SPLIT_DB_PATH
from table_split.db.migrations.runner import get_connection
from table_split.db.models import derive_dairy_status
from table_split.enums import TriState

logger = logging.getLogger(__name__)

SAMPLE_MENU_EXTENSIONS: list[dict[str, Any]] = [
    # 1. Vegan Bowl: 100% plant-based, dairy NO, vegan YES
    {
        "item_id": "D039",
        "name": "Superfood Vegan Buddha Bowl",
        "diet_veg": TriState.YES.value,
        "diet_non_veg": TriState.NO.value,
        "diet_egg": TriState.NO.value,
        "diet_vegan": TriState.YES.value,
        "diet_jain": TriState.NO.value,
        "diet_halal": TriState.YES.value,
        "diet_gluten_free": TriState.YES.value,
        "allergen_cereals_gluten": TriState.NO.value,
        "allergen_crustaceans": TriState.NO.value,
        "allergen_milk": TriState.NO.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.NO.value,
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.NO.value,
        "contains_dairy": TriState.NO.value,
        "spice_level": 1,
        "serves": 1,
        "shareable": 0,
        "price_paise": 26000,  # ₹260.00
        "tax_rate_bps": 500,
        "packaging_paise": 1500,
        "verified_by": "Chef Anita (Certified Food Safety Auditor)",
        "verified_at": 1732438800,
        "cross_contact_note": "Dedicated vegan prep station, cold-pressed olive dressing",
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
        "diet_halal": TriState.YES.value,
        "diet_gluten_free": TriState.NO.value,
        "allergen_cereals_gluten": TriState.YES.value,
        "allergen_crustaceans": TriState.NO.value,
        "allergen_milk": TriState.YES.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.NO.value,
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.NO.value,
        "contains_dairy": TriState.YES.value,
        "spice_level": 2,
        "serves": 2,
        "shareable": 1,
        "price_paise": 38000,  # ₹380.00
        "tax_rate_bps": 500,
        "packaging_paise": 2500,
        "verified_by": "Quality Team",
        "verified_at": 1732438800,
        "cross_contact_note": "Baked on stone deck alongside other cheese pizzas",
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
        "diet_halal": TriState.YES.value,
        "diet_gluten_free": TriState.NO.value,
        "allergen_cereals_gluten": TriState.YES.value,
        "allergen_crustaceans": TriState.NO.value,
        "allergen_milk": TriState.YES.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.NO.value,
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.NO.value,
        "contains_dairy": TriState.YES.value,
        "spice_level": 1,
        "serves": 2,
        "shareable": 1,
        "price_paise": 15000,  # ₹150.00
        "tax_rate_bps": 500,
        "packaging_paise": 1000,
        "verified_by": "Chef Ravi",
        "verified_at": 1732438800,
        "cross_contact_note": "Brushed with real dairy butter and minced garlic",
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
        "diet_halal": TriState.YES.value,
        "diet_gluten_free": TriState.NO.value,
        "allergen_cereals_gluten": TriState.YES.value,
        "allergen_crustaceans": TriState.NO.value,
        "allergen_milk": TriState.YES.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.NO.value,
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.NO.value,
        "contains_dairy": TriState.YES.value,
        "spice_level": 4,  # MAX SPICE
        "serves": 2,
        "shareable": 1,
        "price_paise": 42000,  # ₹420.00
        "tax_rate_bps": 500,
        "packaging_paise": 2500,
        "verified_by": "Chef Anita",
        "verified_at": 1732438800,
        "cross_contact_note": "Extreme heat warning: contains ghost pepper sauce and jalapenos",
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
        "diet_halal": TriState.YES.value,
        "diet_gluten_free": TriState.YES.value,
        "allergen_cereals_gluten": TriState.NO.value,
        "allergen_crustaceans": TriState.NO.value,
        "allergen_milk": TriState.YES.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.YES.value,  # Cashew paste in gravy
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.NO.value,
        "contains_dairy": TriState.YES.value,
        "spice_level": 2,
        "serves": 2,
        "shareable": 1,
        "price_paise": 28000,
        "tax_rate_bps": 500,
        "packaging_paise": 2000,
        "verified_by": "Chef Ravi",
        "verified_at": 1732438800,
        "cross_contact_note": "Simmered in butter and heavy cream",
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
        "diet_halal": TriState.YES.value,
        "diet_gluten_free": TriState.YES.value,
        "allergen_cereals_gluten": TriState.NO.value,
        "allergen_crustaceans": TriState.NO.value,
        "allergen_milk": TriState.YES.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.YES.value,
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.NO.value,
        "contains_dairy": TriState.YES.value,
        "spice_level": 2,
        "serves": 2,
        "shareable": 1,
        "price_paise": 24000,
        "tax_rate_bps": 500,
        "packaging_paise": 2000,
        "verified_by": "Chef Ravi",
        "verified_at": 1732438800,
        "cross_contact_note": "Contains dairy paneer & cashew gravy",
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
        "diet_halal": TriState.YES.value,
        "diet_gluten_free": TriState.NO.value,
        "allergen_cereals_gluten": TriState.YES.value,
        "allergen_crustaceans": TriState.NO.value,
        "allergen_milk": TriState.YES.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.NO.value,
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.NO.value,
        "contains_dairy": TriState.YES.value,
        "spice_level": 0,
        "serves": 1,
        "shareable": 1,
        "price_paise": 6000,
        "tax_rate_bps": 500,
        "packaging_paise": 500,
        "verified_by": "Chef Anita",
        "verified_at": 1732438800,
        "cross_contact_note": "Refined wheat flour, milk kneaded",
    },
    # 8. D008: Veg Samosa — DELIBERATELY UNKNOWN allergens (tree nuts UNKNOWN, dairy UNKNOWN)
    {
        "item_id": "D008",
        "name": "Veg Samosa (2 pcs)",
        "diet_veg": TriState.YES.value,
        "diet_non_veg": TriState.NO.value,
        "diet_egg": TriState.NO.value,
        "diet_vegan": TriState.UNKNOWN.value,  # Deliberately UNKNOWN
        "diet_jain": TriState.NO.value,       # Contains potato/onion
        "diet_halal": TriState.YES.value,
        "diet_gluten_free": TriState.NO.value,
        "allergen_cereals_gluten": TriState.YES.value,
        "allergen_crustaceans": TriState.NO.value,
        "allergen_milk": TriState.UNKNOWN.value,  # Deliberately UNKNOWN ghee presence
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.UNKNOWN.value,  # Deliberately UNKNOWN fry oil/traces
        "allergen_tree_nuts": TriState.UNKNOWN.value, # Deliberately UNKNOWN raisin/cashew
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.UNKNOWN.value,
        "contains_dairy": TriState.UNKNOWN.value,
        "spice_level": 2,
        "serves": 1,
        "shareable": 1,
        "price_paise": 8000,
        "tax_rate_bps": 500,
        "packaging_paise": 800,
        "verified_by": "Kitchen Assistant (Pending Lab Audit)",
        "verified_at": None,
        "cross_contact_note": "Commercial fryer shared with peanut/nut appetizers",
    },
    # 9. D012: Gulab Jamun (Dairy YES, Tree nuts YES)
    {
        "item_id": "D012",
        "name": "Gulab Jamun (3 pcs)",
        "diet_veg": TriState.YES.value,
        "diet_non_veg": TriState.NO.value,
        "diet_egg": TriState.NO.value,
        "diet_vegan": TriState.NO.value,
        "diet_jain": TriState.YES.value,
        "diet_halal": TriState.YES.value,
        "diet_gluten_free": TriState.NO.value,
        "allergen_cereals_gluten": TriState.YES.value,
        "allergen_crustaceans": TriState.NO.value,
        "allergen_milk": TriState.YES.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.YES.value,  # Pistachio garnish
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.NO.value,
        "contains_dairy": TriState.YES.value,
        "spice_level": 0,
        "serves": 2,
        "shareable": 1,
        "price_paise": 11000,
        "tax_rate_bps": 500,
        "packaging_paise": 1000,
        "verified_by": "Chef Ravi",
        "verified_at": 1732438800,
        "cross_contact_note": "Khoya and pistachio garnish",
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
        "diet_halal": TriState.YES.value,
        "diet_gluten_free": TriState.YES.value,
        "allergen_cereals_gluten": TriState.NO.value,
        "allergen_crustaceans": TriState.YES.value,
        "allergen_milk": TriState.YES.value,  # Yogurt marinade
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.NO.value,
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.NO.value,
        "contains_dairy": TriState.YES.value,
        "spice_level": 3,
        "serves": 2,
        "shareable": 1,
        "price_paise": 34000,
        "tax_rate_bps": 500,
        "packaging_paise": 2500,
        "verified_by": "Chef Anita",
        "verified_at": 1732438800,
        "cross_contact_note": "Contains tiger prawns marinated in hung curd",
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
        "diet_halal": TriState.YES.value,
        "diet_gluten_free": TriState.YES.value,
        "allergen_cereals_gluten": TriState.NO.value,
        "allergen_crustaceans": TriState.NO.value,
        "allergen_milk": TriState.NO.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.NO.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.NO.value,
        "allergen_soy": TriState.UNKNOWN.value,      # Deliberately UNKNOWN
        "allergen_sulphite": TriState.UNKNOWN.value, # Deliberately UNKNOWN
        "contains_dairy": TriState.NO.value,
        "spice_level": 2,
        "serves": 2,
        "shareable": 1,
        "price_paise": 18000,
        "tax_rate_bps": 500,
        "packaging_paise": 1800,
        "verified_by": "Chef Anita",
        "verified_at": 1732438800,
        "cross_contact_note": "Naturally vegan, cooked with mustard oil and whole spices",
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
        "diet_halal": TriState.YES.value,
        "diet_gluten_free": TriState.YES.value,
        "allergen_cereals_gluten": TriState.NO.value,
        "allergen_crustaceans": TriState.UNKNOWN.value,  # Deliberately UNKNOWN cross-contact
        "allergen_milk": TriState.NO.value,
        "allergen_eggs": TriState.NO.value,
        "allergen_fish": TriState.YES.value,
        "allergen_peanuts": TriState.NO.value,
        "allergen_tree_nuts": TriState.NO.value,
        "allergen_soy": TriState.NO.value,
        "allergen_sulphite": TriState.NO.value,
        "contains_dairy": TriState.NO.value,
        "spice_level": 3,
        "serves": 2,
        "shareable": 1,
        "price_paise": 31000,
        "tax_rate_bps": 500,
        "packaging_paise": 2000,
        "verified_by": "Chef Ravi",
        "verified_at": 1732438800,
        "cross_contact_note": "Coconut milk gravy; prep surfaces shared with shellfish",
    },
]


def seed_menu_item_extensions(db_path: str | None = None) -> int:
    """Inserts or updates the 12 audited menu item extensions."""
    conn = get_connection(db_path)
    count = 0
    try:
        with conn:
            for item in SAMPLE_MENU_EXTENSIONS:
                # Derived rule verification before writing
                derived_dairy = derive_dairy_status(
                    item["allergen_milk"], item.get("diet_vegan", TriState.UNKNOWN.value)
                ).value

                # If allergen_milk == YES, contains_dairy must be YES
                if item["allergen_milk"] == TriState.YES.value and item["contains_dairy"] != TriState.YES.value:
                    item["contains_dairy"] = TriState.YES.value
                elif item["contains_dairy"] == TriState.UNKNOWN.value and derived_dairy != TriState.UNKNOWN.value:
                    item["contains_dairy"] = derived_dairy

                conn.execute(
                    """
                    INSERT INTO menu_item_extensions (
                        item_id, diet_veg, diet_non_veg, diet_egg, diet_vegan, diet_jain, diet_halal, diet_gluten_free,
                        allergen_cereals_gluten, allergen_crustaceans, allergen_milk, allergen_eggs, allergen_fish,
                        allergen_peanuts, allergen_tree_nuts, allergen_soy, allergen_sulphite, contains_dairy,
                        spice_level, serves, shareable, price_paise, tax_rate_bps, packaging_paise,
                        verified_by, verified_at, cross_contact_note
                    ) VALUES (
                        :item_id, :diet_veg, :diet_non_veg, :diet_egg, :diet_vegan, :diet_jain, :diet_halal, :diet_gluten_free,
                        :allergen_cereals_gluten, :allergen_crustaceans, :allergen_milk, :allergen_eggs, :allergen_fish,
                        :allergen_peanuts, :allergen_tree_nuts, :allergen_soy, :allergen_sulphite, :contains_dairy,
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
                        allergen_cereals_gluten=excluded.allergen_cereals_gluten,
                        allergen_crustaceans=excluded.allergen_crustaceans,
                        allergen_milk=excluded.allergen_milk,
                        allergen_eggs=excluded.allergen_eggs,
                        allergen_fish=excluded.allergen_fish,
                        allergen_peanuts=excluded.allergen_peanuts,
                        allergen_tree_nuts=excluded.allergen_tree_nuts,
                        allergen_soy=excluded.allergen_soy,
                        allergen_sulphite=excluded.allergen_sulphite,
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
                    item,
                )
                count += 1
        logger.info("Successfully seeded %d menu item extensions", count)
        return count
    finally:
        conn.close()


if __name__ == "__main__":
    print(f"Seeding menu item extensions into {TABLE_SPLIT_DB_PATH}...")
    n = seed_menu_item_extensions()
    print(f"Done: {n} items seeded.")

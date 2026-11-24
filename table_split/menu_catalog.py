"""
table_split.menu_catalog — Menu loader with explicit tri-state allergen/dietary verification.

CRITICAL RULE (Requirement 2):
Allergens and diet flags per dish MUST be tri-state (CONTAINS / FREE / UNKNOWN), defaulting to UNKNOWN.
Never infer them from tags or dish names.
Every audited dish must record verified_by and verified_at.
Unverified dishes remain strictly UNKNOWN for uncertified properties.
Prices are converted to strict integer paise (rupees * 100).
"""

import json
from pathlib import Path

from config import DATA_DIR
from table_split.enums import Allergen, DietRule, SpiceLevel, TriState
from table_split.models import DishSafetyProfile, DishVerification, MenuItem

# Explicit, audited culinary safety registry for SpiceRoute Kitchen.
# Any dish NOT present here or with missing keys defaults strictly to TriState.UNKNOWN.
VERIFIED_SAFETY_REGISTRY: dict[str, dict] = {
    # D001: Butter Chicken
    "D001": {
        "verified_by": "Chef Ravi (Executive Chef)",
        "verified_at": "2026-11-20T08:30:00Z",
        "notes": "Contains butter, heavy cream, chicken. Nut-free kitchen batch.",
        "spice_level": SpiceLevel.MEDIUM,
        "allergens": {
            Allergen.DAIRY: TriState.CONTAINS,
            Allergen.TREE_NUTS: TriState.FREE,
            Allergen.PEANUTS: TriState.FREE,
            Allergen.GLUTEN: TriState.FREE,
            Allergen.SOY: TriState.FREE,
            Allergen.SHELLFISH: TriState.FREE,
            Allergen.FISH: TriState.FREE,
            Allergen.EGG: TriState.FREE,
            Allergen.SESAME: TriState.FREE,
            Allergen.MUSTARD: TriState.FREE,
        },
        "diets": {
            DietRule.VEGETARIAN: TriState.FREE,   # Contains chicken
            DietRule.VEGAN: TriState.FREE,        # Contains chicken & dairy
            DietRule.JAIN: TriState.FREE,         # Contains chicken, onion, garlic
            DietRule.HALAL: TriState.CONTAINS,    # Halal chicken certified
            DietRule.KOSHER: TriState.FREE,       # Dairy + poultry
        },
    },
    # D002: Paneer Tikka Masala
    "D002": {
        "verified_by": "Chef Priya (Sous Chef)",
        "verified_at": "2026-11-20T09:00:00Z",
        "notes": "Contains paneer (dairy), onion, garlic. Certified gluten-free.",
        "spice_level": SpiceLevel.MEDIUM,
        "allergens": {
            Allergen.DAIRY: TriState.CONTAINS,
            Allergen.TREE_NUTS: TriState.FREE,
            Allergen.PEANUTS: TriState.FREE,
            Allergen.GLUTEN: TriState.FREE,
            Allergen.SOY: TriState.FREE,
            Allergen.SHELLFISH: TriState.FREE,
            Allergen.FISH: TriState.FREE,
            Allergen.EGG: TriState.FREE,
            Allergen.SESAME: TriState.FREE,
            Allergen.MUSTARD: TriState.FREE,
        },
        "diets": {
            DietRule.VEGETARIAN: TriState.CONTAINS,
            DietRule.VEGAN: TriState.FREE,        # Dairy
            DietRule.JAIN: TriState.FREE,         # Onion, garlic
            DietRule.HALAL: TriState.CONTAINS,    # Pure veg
            DietRule.KOSHER: TriState.FREE,
        },
    },
    # D004: Veg Biryani
    "D004": {
        "verified_by": "Chef Priya (Sous Chef)",
        "verified_at": "2026-11-20T09:15:00Z",
        "notes": "Dum-cooked with vegetables and ghee.",
        "spice_level": SpiceLevel.MEDIUM,
        "allergens": {
            Allergen.DAIRY: TriState.CONTAINS,    # Ghee
            Allergen.TREE_NUTS: TriState.FREE,
            Allergen.PEANUTS: TriState.FREE,
            Allergen.GLUTEN: TriState.FREE,
            Allergen.SOY: TriState.FREE,
            Allergen.SHELLFISH: TriState.FREE,
            Allergen.FISH: TriState.FREE,
            Allergen.EGG: TriState.FREE,
            Allergen.SESAME: TriState.FREE,
            Allergen.MUSTARD: TriState.FREE,
        },
        "diets": {
            DietRule.VEGETARIAN: TriState.CONTAINS,
            DietRule.VEGAN: TriState.FREE,        # Ghee
            DietRule.JAIN: TriState.FREE,         # Carrots/onions
            DietRule.HALAL: TriState.CONTAINS,
            DietRule.KOSHER: TriState.FREE,
        },
    },
    # D005: Garlic Naan
    "D005": {
        "verified_by": "Chef Arjun (Tandoor Chef)",
        "verified_at": "2026-11-20T09:30:00Z",
        "notes": "Contains refined wheat flour (maida), milk, butter, garlic.",
        "spice_level": SpiceLevel.NONE,
        "allergens": {
            Allergen.DAIRY: TriState.CONTAINS,    # Milk, butter
            Allergen.TREE_NUTS: TriState.FREE,
            Allergen.PEANUTS: TriState.FREE,
            Allergen.GLUTEN: TriState.CONTAINS,   # Wheat
            Allergen.SOY: TriState.FREE,
            Allergen.SHELLFISH: TriState.FREE,
            Allergen.FISH: TriState.FREE,
            Allergen.EGG: TriState.FREE,
            Allergen.SESAME: TriState.FREE,
            Allergen.MUSTARD: TriState.FREE,
        },
        "diets": {
            DietRule.VEGETARIAN: TriState.CONTAINS,
            DietRule.VEGAN: TriState.FREE,        # Milk/butter
            DietRule.JAIN: TriState.FREE,         # Garlic
            DietRule.HALAL: TriState.CONTAINS,
            DietRule.KOSHER: TriState.FREE,
        },
    },
    # D008: Veg Samosa (2 pcs)
    "D008": {
        "verified_by": "Chef Arjun (Tandoor Chef)",
        "verified_at": "2026-11-20T09:45:00Z",
        "notes": "Potato and peas filling, refined oil fried. Strictly 100% plant-based.",
        "spice_level": SpiceLevel.MILD,
        "allergens": {
            Allergen.DAIRY: TriState.FREE,        # No dairy
            Allergen.TREE_NUTS: TriState.FREE,
            Allergen.PEANUTS: TriState.FREE,
            Allergen.GLUTEN: TriState.CONTAINS,   # Flour pastry
            Allergen.SOY: TriState.FREE,
            Allergen.SHELLFISH: TriState.FREE,
            Allergen.FISH: TriState.FREE,
            Allergen.EGG: TriState.FREE,
            Allergen.SESAME: TriState.FREE,
            Allergen.MUSTARD: TriState.FREE,
        },
        "diets": {
            DietRule.VEGETARIAN: TriState.CONTAINS,
            DietRule.VEGAN: TriState.CONTAINS,    # 100% plant-based
            DietRule.JAIN: TriState.FREE,         # Potatoes
            DietRule.HALAL: TriState.CONTAINS,
            DietRule.KOSHER: TriState.CONTAINS,
        },
    },
    # D009: Dal Tadka
    "D009": {
        "verified_by": "Chef Priya (Sous Chef)",
        "verified_at": "2026-11-20T10:00:00Z",
        "notes": "Yellow lentils tempered in vegetable oil with cumin and garlic.",
        "spice_level": SpiceLevel.MILD,
        "allergens": {
            Allergen.DAIRY: TriState.FREE,        # Prepared with plant oil
            Allergen.TREE_NUTS: TriState.FREE,
            Allergen.PEANUTS: TriState.FREE,
            Allergen.GLUTEN: TriState.FREE,
            Allergen.SOY: TriState.FREE,
            Allergen.SHELLFISH: TriState.FREE,
            Allergen.FISH: TriState.FREE,
            Allergen.EGG: TriState.FREE,
            Allergen.SESAME: TriState.FREE,
            Allergen.MUSTARD: TriState.FREE,
        },
        "diets": {
            DietRule.VEGETARIAN: TriState.CONTAINS,
            DietRule.VEGAN: TriState.CONTAINS,    # Plant-based
            DietRule.JAIN: TriState.FREE,         # Garlic
            DietRule.HALAL: TriState.CONTAINS,
            DietRule.KOSHER: TriState.CONTAINS,
        },
    },
    # D010: Mango Lassi
    "D010": {
        "verified_by": "Chef Priya (Sous Chef)",
        "verified_at": "2026-11-20T10:15:00Z",
        "notes": "Fresh yogurt and mango pulp.",
        "spice_level": SpiceLevel.NONE,
        "allergens": {
            Allergen.DAIRY: TriState.CONTAINS,    # Yogurt
            Allergen.TREE_NUTS: TriState.FREE,
            Allergen.PEANUTS: TriState.FREE,
            Allergen.GLUTEN: TriState.FREE,
            Allergen.SOY: TriState.FREE,
            Allergen.SHELLFISH: TriState.FREE,
            Allergen.FISH: TriState.FREE,
            Allergen.EGG: TriState.FREE,
            Allergen.SESAME: TriState.FREE,
            Allergen.MUSTARD: TriState.FREE,
        },
        "diets": {
            DietRule.VEGETARIAN: TriState.CONTAINS,
            DietRule.VEGAN: TriState.FREE,        # Dairy
            DietRule.JAIN: TriState.CONTAINS,     # Yogurt + fruit allowed
            DietRule.HALAL: TriState.CONTAINS,
            DietRule.KOSHER: TriState.CONTAINS,
        },
    },
    # D018: Crispy Onion Bhaji
    "D018": {
        "verified_by": "Chef Arjun (Tandoor Chef)",
        "verified_at": "2026-11-20T10:30:00Z",
        "notes": "Gram flour (besan) and onions in vegetable oil. Certified Gluten-Free & Vegan.",
        "spice_level": SpiceLevel.MILD,
        "allergens": {
            Allergen.DAIRY: TriState.FREE,
            Allergen.TREE_NUTS: TriState.FREE,
            Allergen.PEANUTS: TriState.FREE,
            Allergen.GLUTEN: TriState.FREE,       # Besan / chickpea flour
            Allergen.SOY: TriState.FREE,
            Allergen.SHELLFISH: TriState.FREE,
            Allergen.FISH: TriState.FREE,
            Allergen.EGG: TriState.FREE,
            Allergen.SESAME: TriState.FREE,
            Allergen.MUSTARD: TriState.FREE,
        },
        "diets": {
            DietRule.VEGETARIAN: TriState.CONTAINS,
            DietRule.VEGAN: TriState.CONTAINS,
            DietRule.JAIN: TriState.FREE,         # Onions
            DietRule.HALAL: TriState.CONTAINS,
            DietRule.KOSHER: TriState.CONTAINS,
        },
    },
}


def load_menu_catalog(menu_path: Path | None = None) -> list[MenuItem]:
    """
    Ingests data/menu.json and maps items into normalized MenuItem objects.
    - Prices are converted to integer paise (rupees * 100).
    - If verified in registry, adopts explicit audited profile.
    - If NOT verified, all allergen and dietary flags strictly default to TriState.UNKNOWN.
    """
    if menu_path is None:
        menu_path = DATA_DIR / "menu.json"

    with open(menu_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    dishes_raw = data.get("menu", [])
    menu_items: list[MenuItem] = []

    for raw in dishes_raw:
        dish_id = raw["id"]
        # Integer paise conversion: rupees * 100
        price_paise = int(raw["price"]) * 100

        # Check audited registry
        if dish_id in VERIFIED_SAFETY_REGISTRY:
            reg = VERIFIED_SAFETY_REGISTRY[dish_id]
            verification = DishVerification(
                verified_by=reg.get("verified_by"),
                verified_at=reg.get("verified_at"),
                verification_notes=reg.get("notes"),
            )
            safety_profile = DishSafetyProfile(
                allergens=reg.get("allergens", {}),
                diets=reg.get("diets", {}),
                spice_level=reg.get("spice_level", SpiceLevel.MILD),
                verification=verification,
            )
        else:
            # UNVERIFIED ITEM: Everything strictly defaults to UNKNOWN
            # Under Requirement 2, UNKNOWN fails any related hard constraint.
            verification = DishVerification(
                verified_by=None,
                verified_at=None,
                verification_notes="Unverified item. All allergens and diet compliance default to UNKNOWN.",
            )
            safety_profile = DishSafetyProfile(
                allergens={a: TriState.UNKNOWN for a in Allergen},
                diets={d: TriState.UNKNOWN for d in DietRule},
                spice_level=SpiceLevel.MILD,
                verification=verification,
            )

        item = MenuItem(
            id=dish_id,
            dish_name=raw["dish_name"],
            category=raw["category"],
            description=raw.get("description", ""),
            price_paise=price_paise,
            prep_time_minutes=raw.get("prep_time_minutes", 15),
            is_available=raw.get("is_available", True),
            safety_profile=safety_profile,
            tags=raw.get("tags", []),
        )
        menu_items.append(item)

    return menu_items

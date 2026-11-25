"""
table_split.safety — Deterministic dietary safety and allergen exclusion engine.

SAFETY INVARIANTS:
1. Deterministic code strictly owns food safety. Zero LLM involvement.
2. Tri-state allergen model:
   - CONTAINS: dish verified to contain allergen. Unsafe for allergic diner.
   - FREE: dish verified to be free of allergen. Safe for allergic diner.
   - UNKNOWN: unverified allergen status. FAILS hard allergen constraint (treated as UNSAFE).
3. Tri-state diet model:
   - CONTAINS: dish verified to comply with diet (e.g. certified VEGAN). Safe for vegan diner.
   - FREE: dish verified not to comply (e.g. non-vegan). Unsafe for vegan diner.
   - UNKNOWN: unverified diet compliance. FAILS hard diet constraint (treated as UNSAFE).
4. Disliked dishes and unavailable dishes are filtered deterministically.
"""

from collections.abc import Sequence

from table_split.enums import PreferenceStrength, TriState
from table_split.models import Diner, DinerPreference, MenuItem


def is_dish_safe_for_diner(dish: MenuItem, pref: DinerPreference) -> tuple[bool, list[str]]:
    """
    Evaluates whether a dish is safe and permissible for a diner based on their preferences.
    Returns (is_safe, reasons_if_unsafe).
    """
    reasons: list[str] = []

    # 1. Availability check
    if not dish.is_available:
        reasons.append(f"Dish '{dish.dish_name}' is currently unavailable in the kitchen.")

    # 2. Disliked dish exclusion
    if dish.id in pref.disliked_dish_ids:
        reasons.append(f"Dish '{dish.dish_name}' is in diner's disliked list.")

    profile = dish.safety_profile

    # 2b. Unverified item exclusion when any hard dietary or allergen constraint is active
    if (pref.allergies or pref.diet_rules) and not profile.is_verified:
        reasons.append(f"Dietary info not verified for '{dish.dish_name}' (excluded when hard constraints are active).")

    # 3. Allergen checks (Hard safety constraints)
    for allergen in pref.allergies:
        status = profile.allergens.get(allergen, TriState.UNKNOWN)
        if status in (TriState.CONTAINS, TriState.YES):
            reasons.append(f"Contains allergen: {allergen.value}.")
        elif status == TriState.UNKNOWN:
            # FAIL-SAFE INVARIANT: Unknown allergen status fails hard safety check
            reasons.append(f"Unverified allergen status for {allergen.value} (treated as UNSAFE).")

    # 4. Dietary rule compliance (e.g. VEGETARIAN, VEGAN, JAIN, HALAL)
    for diet_rule in pref.diet_rules:
        compliance = profile.diets.get(diet_rule, TriState.UNKNOWN)
        if compliance in (TriState.FREE, TriState.NO):
            reasons.append(f"Does not comply with {diet_rule.value} diet.")
        elif compliance == TriState.UNKNOWN:
            # FAIL-SAFE INVARIANT: Unknown diet compliance fails hard dietary check
            reasons.append(f"Unverified compliance for {diet_rule.value} diet (treated as UNSAFE).")

    # 5. Spice level check
    if (
        pref.strength == PreferenceStrength.HARD
        and not profile.spice_level.is_acceptable_for(pref.max_spice)
    ):
        reasons.append(
            f"Spice level {profile.spice_level.value} exceeds maximum tolerance {pref.max_spice.value}."
        )

    is_safe = len(reasons) == 0
    return is_safe, reasons


def filter_safe_menu_for_diner(menu: Sequence[MenuItem], pref: DinerPreference) -> list[MenuItem]:
    """
    Returns only the menu items that are completely safe and acceptable for a given diner.
    """
    return [dish for dish in menu if is_dish_safe_for_diner(dish, pref)[0]]


def find_safe_shared_dishes(
    menu: Sequence[MenuItem],
    prefs: Sequence[DinerPreference],
) -> list[MenuItem]:
    """
    Computes the safe intersection of menu items for a group of sharing diners.
    A dish must be 100% safe for EVERY diner in the group to qualify as a shared dish.
    """
    if not prefs:
        return list(menu)

    safe_dishes: list[MenuItem] = []
    for dish in menu:
        # Dish must pass safety for all diners
        if all(is_dish_safe_for_diner(dish, pref)[0] for pref in prefs):
            safe_dishes.append(dish)

    return safe_dishes


def find_safe_dishes_for_group(
    menu: Sequence[MenuItem],
    diners: Sequence[Diner],
) -> list[MenuItem]:
    """
    Convenience wrapper taking a sequence of Diner models.
    """
    active_prefs = [d.preferences for d in diners if d.preferences is not None]
    return find_safe_shared_dishes(menu, active_prefs)

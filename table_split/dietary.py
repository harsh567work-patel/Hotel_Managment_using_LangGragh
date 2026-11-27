"""
table_split.dietary — Pure, zero-I/O deterministic dietary and allergen rule engine.

SAFETY & PURITY INVARIANTS:
1. ZERO I/O: No database, file system, network, clock, or environment variable access.
2. Tri-State Semantics: Every diet, ingredient flag, and allergen is YES, NO, or UNKNOWN
   (accepting Stage 1 CONTAINS/FREE aliases deterministically).
3. Fail-Safe UNKNOWN Rule: Any field equal to UNKNOWN that an active hard constraint
   depends on causes allowed = False with that field listed in unknownFields.
4. Verification Gate: When any hard constraint is active, unverified items
   (missing verified_by or verified_at) return allowed = False with
   "Dietary info not verified".
5. Diet Implication Rules:
   - vegan: requires vegan=YES, veg=YES, non_veg=NO, dairy=NO, milk=NO, egg=NO, eggs=NO, honey=NO.
   - vegetarian: requires veg=YES, non_veg=NO, and (when exclude_egg_for_vegetarian=True) egg=NO & eggs=NO.
   - jain: requires jain=YES, veg=YES, non_veg=NO, egg=NO, eggs=NO, onion_garlic=NO, root_veg=NO.
   - halal: requires halal=YES (UNKNOWN and NO both fail).
   - gluten_free: requires allergen_cereals_gluten=NO and diet_gluten_free != NO.
6. Monotonicity: Adding any constraint to hardConstraints can only keep or shrink
   the set of allowed items.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from table_split.enums import Allergen, DietRule, TriState
from table_split.models import DinerPreference, MenuItem, validate_strict_paise

# Canonical list of all 21 tri-state dietary, ingredient, and allergen fields tracked per menu item
DIET_TRISTATE_FIELDS: tuple[str, ...] = (
    "diet_veg",
    "diet_non_veg",
    "diet_egg",
    "diet_vegan",
    "diet_jain",
    "diet_halal",
    "diet_gluten_free",
    "contains_onion_garlic",
    "contains_root_veg",
    "contains_honey",
)

ALLERGEN_TRISTATE_FIELDS: tuple[str, ...] = (
    "allergen_cereals_gluten",
    "allergen_crustaceans",
    "allergen_milk",
    "allergen_eggs",
    "allergen_fish",
    "allergen_peanuts",
    "allergen_tree_nuts",
    "allergen_soy",
    "allergen_sulphite",
    "allergen_sesame",
    "allergen_mustard",
    "contains_dairy",
)

ALL_TRISTATE_FIELDS: tuple[str, ...] = DIET_TRISTATE_FIELDS + ALLERGEN_TRISTATE_FIELDS

# Display names for customer allergen declarations and reports
ALLERGEN_FIELD_LABELS: dict[str, str] = {
    "allergen_cereals_gluten": "Cereals containing gluten",
    "allergen_crustaceans": "Crustaceans",
    "allergen_milk": "Milk",
    "contains_dairy": "Dairy",
    "allergen_eggs": "Eggs",
    "allergen_fish": "Fish",
    "allergen_peanuts": "Peanuts",
    "allergen_tree_nuts": "Tree nuts",
    "allergen_soy": "Soybeans",
    "allergen_sulphite": "Sulphites",
    "allergen_sesame": "Sesame (non-statutory)",
    "allergen_mustard": "Mustard (non-statutory)",
}

# Mapping from canonical constraint keys (or synonyms) to item tri-state columns
_DIET_SYNONYMS: dict[str, str] = {
    "veg": "vegetarian",
    "vegetarian": "vegetarian",
    "vegan": "vegan",
    "jain": "jain",
    "halal": "halal",
    "gluten_free": "gluten_free",
    "gluten-free": "gluten_free",
    "gf": "gluten_free",
}

_ALLERGEN_SYNONYMS: dict[str, str] = {
    "gluten": "cereals_gluten",
    "cereals_gluten": "cereals_gluten",
    "cereals containing gluten": "cereals_gluten",
    "allergen_cereals_gluten": "cereals_gluten",
    "wheat": "cereals_gluten",
    "crustaceans": "crustaceans",
    "shellfish": "crustaceans",
    "allergen_crustaceans": "crustaceans",
    "milk": "milk",
    "allergen_milk": "milk",
    "dairy": "dairy",
    "contains_dairy": "dairy",
    "lactose": "dairy",
    "egg": "eggs",
    "eggs": "eggs",
    "allergen_eggs": "eggs",
    "fish": "fish",
    "allergen_fish": "fish",
    "peanut": "peanuts",
    "peanuts": "peanuts",
    "allergen_peanuts": "peanuts",
    "tree_nut": "tree_nuts",
    "tree_nuts": "tree_nuts",
    "tree nuts": "tree_nuts",
    "nuts": "tree_nuts",
    "allergen_tree_nuts": "tree_nuts",
    "soy": "soy",
    "soya": "soy",
    "soybeans": "soy",
    "allergen_soy": "soy",
    "sulphite": "sulphite",
    "sulphites": "sulphite",
    "sulfite": "sulphite",
    "sulfites": "sulphite",
    "allergen_sulphite": "sulphite",
    "sesame": "sesame",
    "allergen_sesame": "sesame",
    "mustard": "mustard",
    "allergen_mustard": "mustard",
}

ALLERGEN_RULE_FIELDS: dict[str, tuple[str, ...]] = {
    "cereals_gluten": ("allergen_cereals_gluten",),
    "crustaceans": ("allergen_crustaceans",),
    "milk": ("allergen_milk",),
    "dairy": ("contains_dairy", "allergen_milk"),
    "eggs": ("allergen_eggs",),
    "fish": ("allergen_fish",),
    "peanuts": ("allergen_peanuts",),
    "tree_nuts": ("allergen_tree_nuts",),
    "soy": ("allergen_soy",),
    "sulphite": ("allergen_sulphite",),
    "sesame": ("allergen_sesame",),
    "mustard": ("allergen_mustard",),
}


def normalize_tristate(value: Any) -> TriState:
    """
    Normalizes YES/NO/UNKNOWN and legacy CONTAINS/FREE tokens to canonical YES/NO/UNKNOWN.
    """
    if value is None:
        return TriState.UNKNOWN
    if isinstance(value, TriState):
        if value == TriState.CONTAINS:
            return TriState.YES
        if value == TriState.FREE:
            return TriState.NO
        return value
    if isinstance(value, str):
        cleaned = value.strip().upper()
        if cleaned in ("YES", "CONTAINS"):
            return TriState.YES
        if cleaned in ("NO", "FREE"):
            return TriState.NO
        if cleaned == "UNKNOWN":
            return TriState.UNKNOWN
    raise ValueError(f"Invalid TriState value: {value!r}. Expected YES, NO, or UNKNOWN.")


def normalize_diet_key(diet: str | DietRule) -> str:
    raw = diet.value if isinstance(diet, DietRule) else str(diet)
    key = raw.strip().lower().replace(" ", "_")
    if key in _DIET_SYNONYMS:
        return _DIET_SYNONYMS[key]
    raise ValueError(f"Unsupported dietary constraint: {diet!r}")


def normalize_allergen_key(allergen: str | Allergen) -> str:
    raw = allergen.value if isinstance(allergen, Allergen) else str(allergen)
    key = raw.strip().lower()
    if key in _ALLERGEN_SYNONYMS:
        return _ALLERGEN_SYNONYMS[key]
    # Also try underscore-normalized lookup
    key_underscore = key.replace(" ", "_").replace("-", "_")
    if key_underscore in _ALLERGEN_SYNONYMS:
        return _ALLERGEN_SYNONYMS[key_underscore]
    raise ValueError(f"Unsupported allergen constraint: {allergen!r}")


class DietaryItem(BaseModel):
    """
    Pure normalized representation of a menu item's dietary, ingredient, allergen,
    and verification metadata. All tri-state fields default strictly to UNKNOWN.
    """

    item_id: str
    dish_name: str = ""
    category: str = ""
    description: str = ""
    price_paise: int = 0
    tax_rate_bps: int = 500
    packaging_paise: int = 0
    is_available: bool = True
    spice_level: int | None = None
    serves: int = 1
    shareable: bool = True
    sales_volume: int = 0
    tags: list[str] = Field(default_factory=list)
    cuisine: str | None = None

    # Verification metadata
    verified_by: str | None = None
    verified_at: int | str | None = None
    cross_contact_note: str | None = None

    # Diet tri-states
    diet_veg: TriState = TriState.UNKNOWN
    diet_non_veg: TriState = TriState.UNKNOWN
    diet_egg: TriState = TriState.UNKNOWN
    diet_vegan: TriState = TriState.UNKNOWN
    diet_jain: TriState = TriState.UNKNOWN
    diet_halal: TriState = TriState.UNKNOWN
    diet_gluten_free: TriState = TriState.UNKNOWN

    # Specific ingredient tri-states
    contains_onion_garlic: TriState = TriState.UNKNOWN
    contains_root_veg: TriState = TriState.UNKNOWN
    contains_honey: TriState = TriState.UNKNOWN

    # Allergen tri-states
    allergen_cereals_gluten: TriState = TriState.UNKNOWN
    allergen_crustaceans: TriState = TriState.UNKNOWN
    allergen_milk: TriState = TriState.UNKNOWN
    allergen_eggs: TriState = TriState.UNKNOWN
    allergen_fish: TriState = TriState.UNKNOWN
    allergen_peanuts: TriState = TriState.UNKNOWN
    allergen_tree_nuts: TriState = TriState.UNKNOWN
    allergen_soy: TriState = TriState.UNKNOWN
    allergen_sulphite: TriState = TriState.UNKNOWN
    allergen_sesame: TriState = TriState.UNKNOWN
    allergen_mustard: TriState = TriState.UNKNOWN
    contains_dairy: TriState = TriState.UNKNOWN

    @field_validator("price_paise", "tax_rate_bps", "packaging_paise", mode="before")
    @classmethod
    def _check_strict_paise(cls, v: Any) -> int:
        return validate_strict_paise(v)

    @field_validator(*ALL_TRISTATE_FIELDS, mode="before")
    @classmethod
    def _coerce_tristates(cls, v: Any) -> TriState:
        return normalize_tristate(v)

    @property
    def is_verified(self) -> bool:
        """True only if a staff reviewer (verified_by) and timestamp (verified_at) are recorded."""
        return bool(self.verified_by and str(self.verified_by).strip() and self.verified_at is not None)

    def get_unknown_fields(self) -> list[str]:
        """Returns ordered list of all tri-state field names currently equal to UNKNOWN."""
        return [
            field_name
            for field_name in ALL_TRISTATE_FIELDS
            if getattr(self, field_name) == TriState.UNKNOWN
        ]

    @property
    def is_fully_verified(self) -> bool:
        """True only when staff-signed-off AND zero tri-state fields remain UNKNOWN."""
        return self.is_verified and len(self.get_unknown_fields()) == 0

    def declared_allergens(self) -> list[str]:
        """Returns human-readable names of allergens explicitly marked YES."""
        present: list[str] = []
        seen: set[str] = set()
        for field_name in ALLERGEN_TRISTATE_FIELDS:
            if getattr(self, field_name) == TriState.YES:
                label = ALLERGEN_FIELD_LABELS.get(field_name, field_name)
                if label not in seen:
                    seen.add(label)
                    present.append(label)
        return present

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> DietaryItem:
        """Creates a DietaryItem from a DB row mapping or dictionary."""
        data = dict(record)
        # Support either 'id' or 'item_id'
        if "item_id" not in data and "id" in data:
            data["item_id"] = str(data["id"])
        return cls.model_validate(data)

    @classmethod
    def from_menu_item(cls, item: MenuItem) -> DietaryItem:
        """Converts a Stage 1 Pydantic MenuItem into a normalized DietaryItem."""
        profile = item.safety_profile
        verif = profile.verification

        veg_state = normalize_tristate(profile.diets.get(DietRule.VEGETARIAN, TriState.UNKNOWN))
        if veg_state == TriState.YES:
            non_veg_state = TriState.NO
        elif veg_state == TriState.NO:
            non_veg_state = TriState.YES
        else:
            non_veg_state = TriState.UNKNOWN

        egg_state = normalize_tristate(profile.allergens.get(Allergen.EGG, TriState.UNKNOWN))
        dairy_state = normalize_tristate(profile.allergens.get(Allergen.DAIRY, TriState.UNKNOWN))

        return cls(
            item_id=item.id,
            dish_name=item.dish_name,
            category=item.category,
            description=item.description,
            price_paise=item.price_paise,
            is_available=item.is_available,
            spice_level=profile.spice_level.rank,
            tags=list(item.tags),
            verified_by=verif.verified_by,
            verified_at=verif.verified_at,
            cross_contact_note=verif.verification_notes,
            diet_veg=veg_state,
            diet_non_veg=non_veg_state,
            diet_egg=egg_state,
            diet_vegan=normalize_tristate(profile.diets.get(DietRule.VEGAN, TriState.UNKNOWN)),
            diet_jain=normalize_tristate(profile.diets.get(DietRule.JAIN, TriState.UNKNOWN)),
            diet_halal=normalize_tristate(profile.diets.get(DietRule.HALAL, TriState.UNKNOWN)),
            diet_gluten_free=normalize_tristate(profile.diets.get(DietRule.GLUTEN_FREE, TriState.UNKNOWN)),
            allergen_cereals_gluten=normalize_tristate(profile.allergens.get(Allergen.GLUTEN, TriState.UNKNOWN)),
            allergen_crustaceans=normalize_tristate(profile.allergens.get(Allergen.SHELLFISH, TriState.UNKNOWN)),
            allergen_milk=dairy_state,
            contains_dairy=dairy_state,
            allergen_eggs=egg_state,
            allergen_fish=normalize_tristate(profile.allergens.get(Allergen.FISH, TriState.UNKNOWN)),
            allergen_peanuts=normalize_tristate(profile.allergens.get(Allergen.PEANUTS, TriState.UNKNOWN)),
            allergen_tree_nuts=normalize_tristate(profile.allergens.get(Allergen.TREE_NUTS, TriState.UNKNOWN)),
            allergen_soy=normalize_tristate(profile.allergens.get(Allergen.SOY, TriState.UNKNOWN)),
            allergen_sesame=normalize_tristate(profile.allergens.get(Allergen.SESAME, TriState.UNKNOWN)),
            allergen_mustard=normalize_tristate(profile.allergens.get(Allergen.MUSTARD, TriState.UNKNOWN)),
        )


class HardDietaryConstraints(BaseModel):
    """
    Set of non-negotiable dietary and allergen restrictions for evaluation.
    """

    diets: set[str] = Field(default_factory=set)
    allergens: set[str] = Field(default_factory=set)
    exclude_egg_for_vegetarian: bool = True
    require_verified_when_constrained: bool = True

    @field_validator("diets", mode="before")
    @classmethod
    def _normalize_diets(cls, v: Any) -> set[str]:
        if v is None:
            return set()
        if isinstance(v, (str, DietRule)):
            return {normalize_diet_key(v)}
        return {normalize_diet_key(item) for item in v}

    @field_validator("allergens", mode="before")
    @classmethod
    def _normalize_allergens(cls, v: Any) -> set[str]:
        if v is None:
            return set()
        if isinstance(v, (str, Allergen)):
            return {normalize_allergen_key(v)}
        return {normalize_allergen_key(item) for item in v}

    @property
    def has_active_constraints(self) -> bool:
        """True if at least one dietary rule or allergen exclusion is active."""
        return bool(self.diets or self.allergens)

    @classmethod
    def coerce(cls, raw: HardDietaryConstraints | DinerPreference | Mapping[str, Any] | None) -> HardDietaryConstraints:
        """Coerces dicts, DinerPreference, or HardDietaryConstraints into normalized form."""
        if raw is None:
            return cls()
        if isinstance(raw, HardDietaryConstraints):
            return raw
        if isinstance(raw, DinerPreference):
            return cls(
                diets={d.value for d in raw.diet_rules},
                allergens={a.value for a in raw.allergies},
            )
        if isinstance(raw, Mapping):
            data = dict(raw)
            # Support common alias keys from specs / JSON payloads
            if "diet_rules" in data and "diets" not in data:
                data["diets"] = data.pop("diet_rules")
            if "allergies" in data and "allergens" not in data:
                data["allergens"] = data.pop("allergies")
            if "diet" in data and "diets" not in data:
                single = data.pop("diet")
                data["diets"] = [single] if single else []
            return cls.model_validate(data)
        raise TypeError(f"Unsupported constraints type: {type(raw).__name__}")


class DietaryCheckResult(BaseModel):
    """
    Deterministic dietary evaluation result matching spec `{ allowed, reasons, unknownFields }`.
    Supports attribute access and dict subscripting.
    """

    allowed: bool
    reasons: list[str] = Field(default_factory=list)
    unknownFields: list[str] = Field(default_factory=list)

    @property
    def unknown_fields(self) -> list[str]:
        return self.unknownFields

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "unknownFields": list(self.unknownFields),
        }

    def __getitem__(self, key: str) -> Any:
        if key == "allowed":
            return self.allowed
        if key == "reasons":
            return self.reasons
        if key in ("unknownFields", "unknown_fields"):
            return self.unknownFields
        raise KeyError(key)


def _coerce_item(item: DietaryItem | MenuItem | Mapping[str, Any]) -> DietaryItem:
    if isinstance(item, DietaryItem):
        return item
    if isinstance(item, MenuItem):
        return DietaryItem.from_menu_item(item)
    if isinstance(item, Mapping):
        return DietaryItem.from_record(item)
    # Support SQLAlchemy MenuItemExtension ORM objects without importing SQLAlchemy here
    if hasattr(item, "item_id") and hasattr(item, "allergen_milk"):
        payload = {
            "item_id": item.item_id,
            "dish_name": getattr(item, "dish_name", ""),
            "verified_by": getattr(item, "verified_by", None),
            "verified_at": getattr(item, "verified_at", None),
            "cross_contact_note": getattr(item, "cross_contact_note", None),
            "spice_level": getattr(item, "spice_level", None),
            "serves": getattr(item, "serves", 1),
            "shareable": bool(getattr(item, "shareable", True)),
            "price_paise": getattr(item, "price_paise", 0),
            "tax_rate_bps": getattr(item, "tax_rate_bps", 500),
            "packaging_paise": getattr(item, "packaging_paise", 0),
            "sales_volume": getattr(item, "sales_volume", 0),
            "tags": list(getattr(item, "tags", []) or []),
            "cuisine": getattr(item, "cuisine", None),
        }
        for field_name in ALL_TRISTATE_FIELDS:
            if hasattr(item, field_name):
                payload[field_name] = getattr(item, field_name)
        return DietaryItem.from_record(payload)
    raise TypeError(f"Unsupported item type: {type(item).__name__}")


def _append_unique(target: list[str], value: str) -> None:
    if value not in target:
        target.append(value)


def _check_required_field(
    item: DietaryItem,
    field_name: str,
    expected: TriState,
    mismatch_reason: str,
    reasons: list[str],
    unknown_fields: list[str],
) -> None:
    actual: TriState = getattr(item, field_name)
    if actual == TriState.UNKNOWN:
        _append_unique(unknown_fields, field_name)
        _append_unique(reasons, f"Unknown '{field_name}' status (treated as UNSAFE).")
    elif actual != expected:
        _append_unique(reasons, mismatch_reason)


def is_item_allowed(
    item: DietaryItem | MenuItem | Mapping[str, Any],
    hard_constraints: HardDietaryConstraints | DinerPreference | Mapping[str, Any] | None,
) -> DietaryCheckResult:
    """
    Pure evaluation function returning { allowed, reasons, unknownFields }.

    Rules:
    - Any UNKNOWN value on a field depended on by an active constraint sets allowed = False
      and appends the field name to unknownFields.
    - When any hard constraint is active, unverified items (missing verified_by or verified_at)
      return allowed = False with "Dietary info not verified".
    - Diet implications and allergen checks evaluate deterministically.
    """
    item_obj = _coerce_item(item)
    constraints = HardDietaryConstraints.coerce(hard_constraints)

    reasons: list[str] = []
    unknown_fields: list[str] = []

    # 1. Verification gate when any hard constraint is active
    if (
        constraints.has_active_constraints
        and constraints.require_verified_when_constrained
        and not item_obj.is_verified
    ):
        _append_unique(reasons, "Dietary info not verified (excluded when hard constraints are active).")

    # 2. Diet implication rules (sorted for deterministic output ordering)
    for diet in sorted(constraints.diets):
        if diet == "vegetarian":
            _check_required_field(
                item_obj, "diet_veg", TriState.YES,
                "Dish is not marked vegetarian.", reasons, unknown_fields,
            )
            _check_required_field(
                item_obj, "diet_non_veg", TriState.NO,
                "Dish is marked non-vegetarian.", reasons, unknown_fields,
            )
            if constraints.exclude_egg_for_vegetarian:
                _check_required_field(
                    item_obj, "diet_egg", TriState.NO,
                    "Contains egg (excluded under vegetarian egg rule).", reasons, unknown_fields,
                )
                _check_required_field(
                    item_obj, "allergen_eggs", TriState.NO,
                    "Contains egg allergen (excluded under vegetarian egg rule).", reasons, unknown_fields,
                )

        elif diet == "vegan":
            # Vegan implies vegetarian + NO dairy, NO milk, NO egg, NO honey
            _check_required_field(
                item_obj, "diet_vegan", TriState.YES,
                "Dish is not certified vegan.", reasons, unknown_fields,
            )
            _check_required_field(
                item_obj, "diet_veg", TriState.YES,
                "Vegan requires vegetarian diet compliance.", reasons, unknown_fields,
            )
            _check_required_field(
                item_obj, "diet_non_veg", TriState.NO,
                "Dish is marked non-vegetarian (not vegan).", reasons, unknown_fields,
            )
            _check_required_field(
                item_obj, "contains_dairy", TriState.NO,
                "Contains dairy (not vegan).", reasons, unknown_fields,
            )
            _check_required_field(
                item_obj, "allergen_milk", TriState.NO,
                "Contains milk allergen (not vegan).", reasons, unknown_fields,
            )
            _check_required_field(
                item_obj, "diet_egg", TriState.NO,
                "Contains egg (not vegan).", reasons, unknown_fields,
            )
            _check_required_field(
                item_obj, "allergen_eggs", TriState.NO,
                "Contains egg allergen (not vegan).", reasons, unknown_fields,
            )
            _check_required_field(
                item_obj, "contains_honey", TriState.NO,
                "Contains honey (not vegan).", reasons, unknown_fields,
            )

        elif diet == "jain":
            # Jain excludes onion, garlic, root vegetables, eggs, and non-veg
            _check_required_field(
                item_obj, "diet_jain", TriState.YES,
                "Dish is not Jain compliant.", reasons, unknown_fields,
            )
            _check_required_field(
                item_obj, "diet_veg", TriState.YES,
                "Jain requires vegetarian diet compliance.", reasons, unknown_fields,
            )
            _check_required_field(
                item_obj, "diet_non_veg", TriState.NO,
                "Dish is marked non-vegetarian (not Jain).", reasons, unknown_fields,
            )
            _check_required_field(
                item_obj, "diet_egg", TriState.NO,
                "Contains egg (not Jain).", reasons, unknown_fields,
            )
            _check_required_field(
                item_obj, "allergen_eggs", TriState.NO,
                "Contains egg allergen (not Jain).", reasons, unknown_fields,
            )
            _check_required_field(
                item_obj, "contains_onion_garlic", TriState.NO,
                "Contains onion or garlic (excluded under Jain diet).", reasons, unknown_fields,
            )
            _check_required_field(
                item_obj, "contains_root_veg", TriState.NO,
                "Contains root vegetables (excluded under Jain diet).", reasons, unknown_fields,
            )

        elif diet == "halal":
            _check_required_field(
                item_obj, "diet_halal", TriState.YES,
                "Dish does not comply with Halal requirement.", reasons, unknown_fields,
            )

        elif diet == "gluten_free":
            _check_required_field(
                item_obj, "allergen_cereals_gluten", TriState.NO,
                "Contains gluten allergen (not gluten-free).", reasons, unknown_fields,
            )
            if item_obj.diet_gluten_free == TriState.NO:
                _append_unique(reasons, "Dish is marked as not gluten-free.")

    # 3. Allergen exclusion checks (sorted for deterministic ordering)
    for allergen in sorted(constraints.allergens):
        target_fields = ALLERGEN_RULE_FIELDS[allergen]
        for field_name in target_fields:
            label = ALLERGEN_FIELD_LABELS.get(field_name, field_name)
            _check_required_field(
                item_obj, field_name, TriState.NO,
                f"Contains allergen: {label}.", reasons, unknown_fields,
            )
        # Extra contradiction guards for paired diet/allergen indicators
        if allergen == "eggs" and item_obj.diet_egg == TriState.YES:
            _append_unique(reasons, "Contains egg ingredient (diet_egg=YES).")
        if allergen == "milk" and item_obj.contains_dairy == TriState.YES:
            _append_unique(reasons, "Contains dairy (milk derivative).")

    return DietaryCheckResult(
        allowed=len(reasons) == 0,
        reasons=reasons,
        unknownFields=unknown_fields,
    )


# Spec-exact camelCase alias
isItemAllowed = is_item_allowed


def filter_items_for_proposal(
    items: Sequence[DietaryItem | MenuItem | Mapping[str, Any]],
    diner_constraints: Sequence[HardDietaryConstraints | DinerPreference | Mapping[str, Any]],
    *,
    only_available: bool = True,
) -> tuple[list[DietaryItem], dict[str, DietaryCheckResult]]:
    """
    Filters candidate items for a Table Split proposal across all diners' hard constraints.
    Returns (allowed_items, rejection_map_by_item_id).

    Unverified items are excluded whenever ANY diner has an active hard constraint.
    When NO diner has any active hard constraint, unverified items remain eligible (if available).
    """
    coerced_items = [_coerce_item(it) for it in items]
    coerced_constraints = [HardDietaryConstraints.coerce(c) for c in diner_constraints]
    any_active_constraint = any(c.has_active_constraints for c in coerced_constraints)

    allowed_items: list[DietaryItem] = []
    rejections: dict[str, DietaryCheckResult] = {}

    for item in coerced_items:
        item_reasons: list[str] = []
        item_unknowns: list[str] = []

        if only_available and not item.is_available:
            _append_unique(item_reasons, "Dish is currently unavailable in the kitchen.")

        if any_active_constraint:
            for c in coerced_constraints:
                if not c.has_active_constraints:
                    continue
                res = is_item_allowed(item, c)
                if not res.allowed:
                    for r in res.reasons:
                        _append_unique(item_reasons, r)
                    for u in res.unknownFields:
                        _append_unique(item_unknowns, u)

        if not item_reasons:
            allowed_items.append(item)
        else:
            rejections[item.item_id] = DietaryCheckResult(
                allowed=False,
                reasons=item_reasons,
                unknownFields=item_unknowns,
            )

    return allowed_items, rejections


def classify_fssai_symbol(item: DietaryItem | MenuItem | Mapping[str, Any]) -> Literal["veg", "non_veg", "unverified"]:
    """
    Pure FSSAI display classifier following Indian food labelling rules:
    - 'non_veg' (brown triangle in brown square): if diet_non_veg == YES OR
      egg-containing (diet_egg == YES or allergen_eggs == YES) [FSSAI egg rule].
    - 'veg' (green circle in green square): ONLY when item is staff-verified AND
      diet_veg == YES AND diet_non_veg == NO AND diet_egg == NO AND allergen_eggs == NO.
    - 'unverified': unverified data or UNKNOWN veg/non-veg/egg status.
      Never invent a third symbol or falsely display green Veg.
    """
    obj = _coerce_item(item)

    # Non-veg & Egg Rule takes precedence so egg/meat is NEVER falsely shown as veg
    if (
        obj.diet_non_veg == TriState.YES
        or obj.diet_egg == TriState.YES
        or obj.allergen_eggs == TriState.YES
    ):
        return "non_veg"

    # Green Veg badge only shown when explicitly verified and free of meat and egg
    if (
        obj.is_verified
        and obj.diet_veg == TriState.YES
        and obj.diet_non_veg == TriState.NO
        and obj.diet_egg == TriState.NO
        and obj.allergen_eggs == TriState.NO
    ):
        return "veg"

    return "unverified"

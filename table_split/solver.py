"""
table_split.solver — Pure, zero-I/O deterministic recommendation engine (Stage 6).

SAFETY & PURITY INVARIANTS:
1. ZERO LLM CALLS: 100% deterministic optimization and rule evaluation.
2. ZERO I/O: No database, file system, network, clock, or environment variable access.
3. Hard Safety First: Hard dietary/allergen constraints are evaluated exclusively via
   Stage 3 `table_split.dietary.is_item_allowed`, where UNKNOWN data fails closed.
4. Budget Enforcement: Every participant's exact allocation (items + tax + packaging +
   delivery share) via Stage 7 `PricingEngine` never exceeds their hard budget.
5. Determinism & Permutation Invariance: Canonical sorting by participant_id and item_id
   with single-worker seeded CP-SAT guarantees identical outputs regardless of input order
   or message history.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from table_split.dietary import (
    ALLERGEN_RULE_FIELDS,
    DietaryCheckResult,
    DietaryItem,
    HardDietaryConstraints,
    is_item_allowed,
    normalize_allergen_key,
    normalize_diet_key,
)
from table_split.enums import PreferenceStrength, TriState
from table_split.models import Diner, DinerPreference, MenuItem, validate_strict_paise
from table_split.pricing import (
    ParticipantAllocation,
    PricingEngine,
    PricingLineInput,
    ProposalPricingResult,
    StubPricingEngine,
    calculate_line_tax_paise,
    split_integer_paise,
)

try:
    from ortools.sat.python import cp_model

    _HAS_ORTOOLS = True
except ImportError:  # pragma: no cover
    cp_model = None  # type: ignore[assignment]
    _HAS_ORTOOLS = False

ENGINE_VERSION = "1.0.0"

PreferenceKind = Literal[
    "diet",
    "allergen",
    "budget_max_paise",
    "spice_min",
    "spice_max",
    "spice_range",
    "cuisine_like",
    "dislike_item",
    "must_include_category",
    "must_include_item",
    "freeform",
    "opt_out_personal_meal",
]

_DRINK_CATEGORY_SLUGS = frozenset({"drinks", "drink", "beverage", "beverages"})
_CATEGORY_ALIASES: dict[str, str] = {
    "main": "main_course",
    "mains": "main_course",
    "main_course": "main_course",
    "entree": "main_course",
    "entrees": "main_course",
    "pizza": "pizzas",
    "pizzas": "pizzas",
    "starter": "starters",
    "starters": "starters",
    "appetizer": "starters",
    "appetizers": "starters",
    "bread": "breads",
    "breads": "breads",
    "drink": "drinks",
    "drinks": "drinks",
    "beverage": "drinks",
    "beverages": "drinks",
    "dessert": "desserts",
    "desserts": "desserts",
    "sweet": "desserts",
    "sweets": "desserts",
    "side": "sides",
    "sides": "sides",
    "bowl": "bowls",
    "bowls": "bowls",
}


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


@lru_cache(maxsize=512)
def normalize_category_id(category: str | None) -> str:
    """Normalizes category names/slugs deterministically (e.g. 'Pizza' -> 'pizzas')."""
    if not category:
        return "other"
    slug = _NON_ALNUM_RE.sub("_", category.strip().lower()).strip("_")
    if not slug:
        return "other"
    if slug in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[slug]
    singular = slug.rstrip("s")
    if singular in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[singular]
    return slug


def is_drink_item(item: DietaryItem) -> bool:
    """Returns True if item belongs to a drink/beverage category (excluded as personal meal)."""
    return normalize_category_id(item.category) in _DRINK_CATEGORY_SLUGS


def _format_rupees(paise: int) -> str:
    if paise % 100 == 0:
        return f"₹{paise // 100}"
    return f"₹{paise / 100:.2f}"


def _parse_spice_bound(raw: Any, default: int) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool):
        raise TypeError(f"Spice bound must be an integer 0..4, got bool ({raw!r}).")
    val = int(raw)
    return max(0, min(4, val))


# ---------------------------------------------------------------------------
# Input & Output Models
# ---------------------------------------------------------------------------
class ParticipantPreference(BaseModel):
    """Confirmed preference item entering the deterministic recommendation engine."""

    model_config = ConfigDict(extra="ignore")

    kind: PreferenceKind
    strength: Literal["HARD", "SOFT"] = "SOFT"
    weight: int = Field(default=1, ge=1, le=5)
    value: dict[str, Any] = Field(default_factory=dict)
    confirmed_by_user: bool = True
    is_private: bool = False

    @field_validator("strength", mode="before")
    @classmethod
    def _normalize_strength(cls, v: Any) -> str:
        if isinstance(v, PreferenceStrength):
            return v.value
        up = str(v).strip().upper()
        if up not in ("HARD", "SOFT"):
            raise ValueError(f"Invalid strength {v!r}; expected 'HARD' or 'SOFT'.")
        return up

    @field_validator("weight", mode="before")
    @classmethod
    def _check_weight(cls, v: Any) -> int:
        if isinstance(v, bool) or not isinstance(v, int) or v < 1 or v > 5:
            raise ValueError(f"Preference weight must be an integer 1..5, got {v!r}.")
        return v

    @model_validator(mode="after")
    def _enforce_hard_safety_and_paise(self) -> ParticipantPreference:
        # Clinical safety rule: diet and allergen constraints are ALWAYS HARD
        if self.kind in ("diet", "allergen"):
            self.strength = "HARD"
        if self.kind == "budget_max_paise":
            raw_paise = self.value.get("max_paise")
            validate_strict_paise(raw_paise)
        return self


class ParticipantInput(BaseModel):
    """Canonical participant representation for the recommendation engine."""

    model_config = ConfigDict(extra="ignore")

    participant_id: str
    display_name: str = ""
    opt_out_personal_meal: bool = False
    preferences: list[ParticipantPreference] = Field(default_factory=list)

    @model_validator(mode="after")
    def _sync_opt_out_flag(self) -> ParticipantInput:
        self.display_name = self.display_name.strip() or self.participant_id
        for pref in self.preferences:
            if (
                pref.confirmed_by_user
                and pref.kind == "opt_out_personal_meal"
                and bool(pref.value.get("opt_out", True))
            ):
                self.opt_out_personal_meal = True
        return self


class SolverConfig(BaseModel):
    """Execution configuration for deterministic solver runs."""

    model_config = ConfigDict(extra="forbid")

    time_limit_ms: int = Field(default=350, ge=50, le=10000)
    delivery_fee_paise: int = 0
    max_items_per_person: int = Field(default=3, ge=1, le=10)
    max_shared_lines: int = Field(default=8, ge=1, le=20)
    candidate_cap_for_large_menus: int = Field(default=22, ge=1, le=100)
    use_ortools: bool = True

    @field_validator("delivery_fee_paise", mode="before")
    @classmethod
    def _check_delivery(cls, v: Any) -> int:
        return validate_strict_paise(v)


class LineServedReason(BaseModel):
    """Machine-readable record of a preference served by a proposal line (no prose)."""

    model_config = ConfigDict(extra="forbid")

    participant_id: str
    preference_kind: str
    preference_value: dict[str, Any] = Field(default_factory=dict)
    weight: int = 1
    match_score: float = 0.5
    is_private: bool = False


class LineExclusionReason(BaseModel):
    """Machine-readable record explaining why a participant was excluded from an item."""

    model_config = ConfigDict(extra="forbid")

    participant_id: str
    reason_codes: list[str] = Field(default_factory=list)
    failed_fields: list[str] = Field(default_factory=list)
    unknown_fields: list[str] = Field(default_factory=list)
    constraint_kinds: list[str] = Field(default_factory=list)


class ProposalLine(BaseModel):
    """Recommended line item (SHARED among >=2 sharers or PERSONAL for 1 diner)."""

    model_config = ConfigDict(extra="forbid")

    menu_item_id: str
    dish_name: str
    category: str
    kind: Literal["SHARED", "PERSONAL"]
    quantity: int = Field(default=1, ge=1)
    consumer_participant_ids: list[str]
    unit_price_paise: int
    serves: int = 1
    reasons: list[LineServedReason] = Field(default_factory=list)
    excluded_participants: list[LineExclusionReason] = Field(default_factory=list)


class ParticipantSatisfactionReport(BaseModel):
    """Per-participant satisfaction and constraint compliance summary."""

    model_config = ConfigDict(extra="forbid")

    participant_id: str
    satisfaction: float
    hard_constraints_met: bool
    soft_met: int
    soft_total: int


class SafetySummary(BaseModel):
    """Hard-constraint compliance guarantee across all participants."""

    model_config = ConfigDict(extra="forbid")

    all_hard_constraints_met: bool
    verified_by_deterministic_engine: bool = True
    participants_checked: int
    violations: list[dict[str, Any]] = Field(default_factory=list)


class BlockedMustInclude(BaseModel):
    """Explains which hard constraint(s) blocked an unmet must-include & offers alternatives."""

    model_config = ConfigDict(extra="forbid")

    participant_id: str
    preference_kind: Literal["must_include_category", "must_include_item"]
    target_id: str
    weight: int
    blocked_by: list[dict[str, Any]] = Field(default_factory=list)
    alternatives_in_same_category: list[dict[str, Any]] = Field(default_factory=list)


class CoreConstraint(BaseModel):
    """Single hard constraint participating in a Minimal Unsatisfiable Core."""

    model_config = ConfigDict(extra="forbid")

    constraint_id: str
    participant_id: str | None = None
    participant_name: str | None = None
    kind: str
    value: dict[str, Any] = Field(default_factory=dict)
    reason_code: str


class RelaxationOption(BaseModel):
    """Ranked feasible relaxation option for resolving NO_SOLUTION."""

    model_config = ConfigDict(extra="forbid")

    rank: int
    action: str
    participant_id: str | None = None
    participant_name: str | None = None
    target: str | None = None
    delta_paise: int | None = None
    description: str
    verified_feasible: bool = True


class ProposalResult(BaseModel):
    """Deterministic output returned by the Stage 6 recommendation engine."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["OK", "NO_SOLUTION"]
    engine_version: str = ENGINE_VERSION
    solver_backend: str
    constraints_hash: str
    lines: list[ProposalLine] = Field(default_factory=list)
    pricing: ProposalPricingResult | None = None
    allocations: dict[str, ParticipantAllocation] = Field(default_factory=dict)
    satisfaction_report: dict[str, ParticipantSatisfactionReport] = Field(default_factory=dict)
    safety_summary: SafetySummary
    blocked_must_includes: list[BlockedMustInclude] = Field(default_factory=list)
    minimal_conflicting_core: list[CoreConstraint] = Field(default_factory=list)
    relaxation_options: list[RelaxationOption] = Field(default_factory=list)
    allowed_items_by_participant: dict[str, list[str]] = Field(default_factory=dict)
    item_scores: dict[str, dict[str, float]] = Field(default_factory=dict)
    objective_metrics: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal Normalized Representations
# ---------------------------------------------------------------------------
class _CompiledSpicePref(BaseModel):
    min_level: int = 0
    max_level: int = 4
    weight: int = 1
    is_private: bool = False


class _CompiledSoftPref(BaseModel):
    kind: str
    value: dict[str, Any]
    weight: int = 1
    is_private: bool = False


class _CompiledHardConstraint(BaseModel):
    cid: str
    participant_id: str
    participant_name: str
    kind: str
    value: dict[str, Any]
    is_private: bool = False


class _CompiledParticipant(BaseModel):
    participant_id: str
    display_name: str
    opt_out_personal_meal: bool
    hard_diets: set[str] = Field(default_factory=set)
    hard_allergens: set[str] = Field(default_factory=set)
    hard_budget_paise: int | None = None
    hard_spice_min: int | None = None
    hard_spice_max: int | None = None
    hard_must_items: set[str] = Field(default_factory=set)
    hard_must_categories: set[str] = Field(default_factory=set)
    hard_dislike_items: set[str] = Field(default_factory=set)
    hard_dislike_labels: list[str] = Field(default_factory=list)

    soft_spice: _CompiledSpicePref | None = None
    soft_cuisines: list[_CompiledSoftPref] = Field(default_factory=list)
    soft_must_categories: list[_CompiledSoftPref] = Field(default_factory=list)
    soft_must_items: list[_CompiledSoftPref] = Field(default_factory=list)
    soft_dislikes: list[_CompiledSoftPref] = Field(default_factory=list)
    soft_budget_paise: _CompiledSoftPref | None = None

    raw_hard_constraints: list[_CompiledHardConstraint] = Field(default_factory=list)
    _cached_hc: HardDietaryConstraints | None = PrivateAttr(default=None)

    def hard_dietary_constraints(self) -> HardDietaryConstraints:
        if self._cached_hc is None:
            self._cached_hc = HardDietaryConstraints(diets=self.hard_diets, allergens=self.hard_allergens)
        return self._cached_hc


# ---------------------------------------------------------------------------
# Input Coercion & Canonicalization
# ---------------------------------------------------------------------------
def coerce_menu_items(
    menu: Sequence[DietaryItem | MenuItem | Mapping[str, Any]],
) -> list[DietaryItem]:
    """Converts MenuItem, DietaryItem, or mapping records into sorted DietaryItems."""
    coerced: list[DietaryItem] = []
    seen_ids: set[str] = set()
    for raw in menu:
        if isinstance(raw, DietaryItem):
            item = raw
        elif isinstance(raw, MenuItem):
            item = DietaryItem.from_menu_item(raw)
        elif isinstance(raw, Mapping):
            data = dict(raw)
            if "price_paise" in data:
                validate_strict_paise(data["price_paise"])
            if "tax_rate_bps" in data:
                validate_strict_paise(data["tax_rate_bps"])
            if "packaging_paise" in data:
                validate_strict_paise(data["packaging_paise"])
            item = DietaryItem.from_record(data)
        else:
            raise TypeError(f"Unsupported menu item type: {type(raw).__name__}")

        validate_strict_paise(item.price_paise)
        validate_strict_paise(item.tax_rate_bps)
        validate_strict_paise(item.packaging_paise)
        if item.serves < 1:
            raise ValueError(f"Item '{item.item_id}' serves must be >= 1, got {item.serves}.")
        if item.item_id in seen_ids:
            raise ValueError(f"Duplicate menu item_id '{item.item_id}'.")
        seen_ids.add(item.item_id)
        coerced.append(item)

    return sorted(coerced, key=lambda it: it.item_id)


def _convert_stage1_diner(diner: Diner) -> ParticipantInput:
    prefs: list[ParticipantPreference] = []
    dp: DinerPreference | None = diner.preferences
    if dp is not None:
        for d in sorted(dp.diet_rules, key=lambda x: x.value):
            prefs.append(
                ParticipantPreference(
                    kind="diet",
                    strength="HARD",
                    weight=5,
                    value={"diet": d.value.lower()},
                    confirmed_by_user=True,
                    is_private=dp.is_private,
                )
            )
        for a in sorted(dp.allergies, key=lambda x: x.value):
            prefs.append(
                ParticipantPreference(
                    kind="allergen",
                    strength="HARD",
                    weight=5,
                    value={"allergen": a.value.lower()},
                    confirmed_by_user=True,
                    is_private=dp.is_private,
                )
            )
        if dp.max_budget_paise is not None:
            prefs.append(
                ParticipantPreference(
                    kind="budget_max_paise",
                    strength=dp.strength.value,  # type: ignore[arg-type]
                    weight=4,
                    value={"max_paise": dp.max_budget_paise},
                    confirmed_by_user=True,
                    is_private=dp.is_private,
                )
            )
        if dp.max_spice.rank < 4:
            prefs.append(
                ParticipantPreference(
                    kind="spice_max",
                    strength=dp.strength.value,  # type: ignore[arg-type]
                    weight=2,
                    value={"level": dp.max_spice.rank},
                    confirmed_by_user=True,
                    is_private=dp.is_private,
                )
            )
        for mid in dp.must_have_dish_ids:
            prefs.append(
                ParticipantPreference(
                    kind="must_include_item",
                    strength="SOFT",
                    weight=5,
                    value={"item_id": mid},
                    confirmed_by_user=True,
                    is_private=dp.is_private,
                )
            )
        for did in dp.disliked_dish_ids:
            prefs.append(
                ParticipantPreference(
                    kind="dislike_item",
                    strength="SOFT",
                    weight=4,
                    value={"item_id": did},
                    confirmed_by_user=True,
                    is_private=dp.is_private,
                )
            )
    return ParticipantInput(
        participant_id=diner.diner_id,
        display_name=diner.name,
        opt_out_personal_meal=False,
        preferences=prefs,
    )


def coerce_participants(
    participants: Sequence[ParticipantInput | Diner | Mapping[str, Any]],
) -> list[ParticipantInput]:
    """Converts participant inputs into canonical sorted ParticipantInputs."""
    out: list[ParticipantInput] = []
    seen: set[str] = set()
    for raw in participants:
        if isinstance(raw, ParticipantInput):
            p = raw
        elif isinstance(raw, Diner):
            p = _convert_stage1_diner(raw)
        elif isinstance(raw, Mapping):
            data = dict(raw)
            pid = str(data.get("participant_id") or data.get("id") or data.get("diner_id") or "").strip()
            if not pid:
                raise ValueError("Participant mapping missing participant_id / id.")
            dname = str(data.get("display_name") or data.get("name") or pid)
            opt_out = bool(data.get("opt_out_personal_meal", False))
            raw_prefs = data.get("preferences") or []
            prefs = [
                pr if isinstance(pr, ParticipantPreference) else ParticipantPreference.model_validate(pr)
                for pr in raw_prefs
            ]
            p = ParticipantInput(
                participant_id=pid,
                display_name=dname,
                opt_out_personal_meal=opt_out,
                preferences=prefs,
            )
        else:
            raise TypeError(f"Unsupported participant type: {type(raw).__name__}")

        if p.participant_id in seen:
            raise ValueError(f"Duplicate participant_id '{p.participant_id}'.")
        seen.add(p.participant_id)

        # Keep only confirmed preferences, deterministically ordered
        confirmed = [pr for pr in p.preferences if pr.confirmed_by_user]
        confirmed.sort(
            key=lambda pr: (
                pr.kind,
                pr.strength,
                pr.weight,
                json.dumps(pr.value, sort_keys=True, separators=(",", ":")),
            )
        )
        out.append(
            ParticipantInput(
                participant_id=p.participant_id,
                display_name=p.display_name,
                opt_out_personal_meal=p.opt_out_personal_meal,
                preferences=confirmed,
            )
        )

    return sorted(out, key=lambda p: p.participant_id)


def build_participant_inputs_from_session(
    session_state: Mapping[str, Any],
    engine_preferences: Sequence[Mapping[str, Any]],
) -> list[ParticipantInput]:
    """
    Bridges Stage 5 SessionService.get_session_state() + get_confirmed_engine_preferences()
    into canonical ParticipantInput objects (ignoring non-active guests and unconfirmed rows).
    """
    prefs_by_pid: dict[str, list[ParticipantPreference]] = {}
    for row in engine_preferences:
        if not bool(row.get("confirmed_by_user", True)):
            continue
        pid = str(row["participant_id"])
        prefs_by_pid.setdefault(pid, []).append(
            ParticipantPreference(
                kind=row["kind"],
                strength=row["strength"],
                weight=int(row.get("weight", 1)),
                value=dict(row.get("value") or {}),
                confirmed_by_user=True,
                is_private=bool(row.get("is_private", False)),
            )
        )

    participants: list[ParticipantInput] = []
    for p_row in session_state.get("participants", []):
        if p_row.get("status", "active") != "active":
            continue
        pid = str(p_row["id"])
        participants.append(
            ParticipantInput(
                participant_id=pid,
                display_name=str(p_row.get("display_name") or pid),
                opt_out_personal_meal=bool(p_row.get("opt_out_personal_meal", False)),
                preferences=prefs_by_pid.get(pid, []),
            )
        )
    return sorted(participants, key=lambda p: p.participant_id)


def _compile_participant(p: ParticipantInput) -> _CompiledParticipant:
    comp = _CompiledParticipant(
        participant_id=p.participant_id,
        display_name=p.display_name,
        opt_out_personal_meal=p.opt_out_personal_meal,
    )

    soft_spice_mins: list[tuple[int, int, bool]] = []
    soft_spice_maxs: list[tuple[int, int, bool]] = []
    soft_budget_vals: list[tuple[int, int, bool]] = []

    for idx, pref in enumerate(p.preferences):
        if not pref.confirmed_by_user:
            continue
        val = pref.value
        if pref.kind == "diet":
            raw_d = str(val.get("diet") or "").strip()
            if raw_d:
                norm_d = normalize_diet_key(raw_d)
                comp.hard_diets.add(norm_d)
                comp.raw_hard_constraints.append(
                    _CompiledHardConstraint(
                        cid=f"{p.participant_id}:diet:{norm_d}",
                        participant_id=p.participant_id,
                        participant_name=p.display_name,
                        kind="diet",
                        value={"diet": norm_d},
                        is_private=pref.is_private,
                    )
                )
        elif pref.kind == "allergen":
            raw_a = str(val.get("allergen") or "").strip()
            if raw_a:
                norm_a = normalize_allergen_key(raw_a)
                comp.hard_allergens.add(norm_a)
                comp.raw_hard_constraints.append(
                    _CompiledHardConstraint(
                        cid=f"{p.participant_id}:allergen:{norm_a}",
                        participant_id=p.participant_id,
                        participant_name=p.display_name,
                        kind="allergen",
                        value={"allergen": norm_a},
                        is_private=pref.is_private,
                    )
                )
        elif pref.kind == "budget_max_paise":
            paise = validate_strict_paise(val["max_paise"])
            if pref.strength == "HARD":
                comp.hard_budget_paise = paise if comp.hard_budget_paise is None else min(comp.hard_budget_paise, paise)
                comp.raw_hard_constraints.append(
                    _CompiledHardConstraint(
                        cid=f"{p.participant_id}:budget:{paise}:{idx}",
                        participant_id=p.participant_id,
                        participant_name=p.display_name,
                        kind="budget_max_paise",
                        value={"max_paise": paise},
                        is_private=pref.is_private,
                    )
                )
            else:
                soft_budget_vals.append((paise, pref.weight, pref.is_private))
        elif pref.kind == "spice_range":
            mn = _parse_spice_bound(val.get("min"), 0)
            mx = _parse_spice_bound(val.get("max"), 4)
            if mn > mx:
                mn, mx = mx, mn
            if pref.strength == "HARD":
                comp.hard_spice_min = mn if comp.hard_spice_min is None else max(comp.hard_spice_min, mn)
                comp.hard_spice_max = mx if comp.hard_spice_max is None else min(comp.hard_spice_max, mx)
                comp.raw_hard_constraints.append(
                    _CompiledHardConstraint(
                        cid=f"{p.participant_id}:spice_range:{mn}:{mx}:{idx}",
                        participant_id=p.participant_id,
                        participant_name=p.display_name,
                        kind="spice_range",
                        value={"min": mn, "max": mx},
                        is_private=pref.is_private,
                    )
                )
            else:
                soft_spice_mins.append((mn, pref.weight, pref.is_private))
                soft_spice_maxs.append((mx, pref.weight, pref.is_private))
        elif pref.kind == "spice_min":
            raw_min = val.get("level")
            if raw_min is None:
                raw_min = val.get("min")
            lvl = _parse_spice_bound(raw_min, 0)
            if pref.strength == "HARD":
                comp.hard_spice_min = lvl if comp.hard_spice_min is None else max(comp.hard_spice_min, lvl)
                comp.raw_hard_constraints.append(
                    _CompiledHardConstraint(
                        cid=f"{p.participant_id}:spice_min:{lvl}:{idx}",
                        participant_id=p.participant_id,
                        participant_name=p.display_name,
                        kind="spice_min",
                        value={"level": lvl},
                        is_private=pref.is_private,
                    )
                )
            else:
                soft_spice_mins.append((lvl, pref.weight, pref.is_private))
        elif pref.kind == "spice_max":
            raw_max = val.get("level")
            if raw_max is None:
                raw_max = val.get("max")
            lvl = _parse_spice_bound(raw_max, 4)
            if pref.strength == "HARD":
                comp.hard_spice_max = lvl if comp.hard_spice_max is None else min(comp.hard_spice_max, lvl)
                comp.raw_hard_constraints.append(
                    _CompiledHardConstraint(
                        cid=f"{p.participant_id}:spice_max:{lvl}:{idx}",
                        participant_id=p.participant_id,
                        participant_name=p.display_name,
                        kind="spice_max",
                        value={"level": lvl},
                        is_private=pref.is_private,
                    )
                )
            else:
                soft_spice_maxs.append((lvl, pref.weight, pref.is_private))
        elif pref.kind == "cuisine_like":
            cuisine = str(val.get("cuisine") or "").strip()
            if cuisine:
                comp.soft_cuisines.append(
                    _CompiledSoftPref(
                        kind="cuisine_like",
                        value={"cuisine": cuisine},
                        weight=pref.weight,
                        is_private=pref.is_private,
                    )
                )
        elif pref.kind == "must_include_category":
            raw_cat = str(val.get("category_id") or val.get("category") or "").strip()
            if not raw_cat:
                continue
            cat = normalize_category_id(raw_cat)
            if pref.strength == "HARD":
                comp.hard_must_categories.add(cat)
                comp.raw_hard_constraints.append(
                    _CompiledHardConstraint(
                        cid=f"{p.participant_id}:must_cat:{cat}:{idx}",
                        participant_id=p.participant_id,
                        participant_name=p.display_name,
                        kind="must_include_category",
                        value={"category_id": cat},
                        is_private=pref.is_private,
                    )
                )
            else:
                comp.soft_must_categories.append(
                    _CompiledSoftPref(
                        kind="must_include_category",
                        value={"category_id": cat},
                        weight=pref.weight,
                        is_private=pref.is_private,
                    )
                )
        elif pref.kind == "must_include_item":
            iid = str(val.get("item_id") or "").strip()
            if iid:
                if pref.strength == "HARD":
                    comp.hard_must_items.add(iid)
                    comp.raw_hard_constraints.append(
                        _CompiledHardConstraint(
                            cid=f"{p.participant_id}:must_item:{iid}:{idx}",
                            participant_id=p.participant_id,
                            participant_name=p.display_name,
                            kind="must_include_item",
                            value={"item_id": iid},
                            is_private=pref.is_private,
                        )
                    )
                else:
                    comp.soft_must_items.append(
                        _CompiledSoftPref(
                            kind="must_include_item",
                            value={"item_id": iid},
                            weight=pref.weight,
                            is_private=pref.is_private,
                        )
                    )
        elif pref.kind == "dislike_item":
            iid = str(val.get("item_id") or "").strip()
            lbl = str(val.get("label") or "").strip()
            if iid or lbl:
                if pref.strength == "HARD":
                    if iid:
                        comp.hard_dislike_items.add(iid)
                    elif lbl:
                        comp.hard_dislike_labels.append(lbl)
                    comp.raw_hard_constraints.append(
                        _CompiledHardConstraint(
                            cid=f"{p.participant_id}:dislike:{iid or lbl}:{idx}",
                            participant_id=p.participant_id,
                            participant_name=p.display_name,
                            kind="dislike_item",
                            value={k: v for k, v in (("item_id", iid), ("label", lbl)) if v},
                            is_private=pref.is_private,
                        )
                    )
                else:
                    comp.soft_dislikes.append(
                        _CompiledSoftPref(
                            kind="dislike_item",
                            value={k: v for k, v in (("item_id", iid), ("label", lbl)) if v},
                            weight=pref.weight,
                            is_private=pref.is_private,
                        )
                    )

    if soft_spice_mins or soft_spice_maxs:
        mn_val = max((x[0] for x in soft_spice_mins), default=0)
        mx_val = min((x[0] for x in soft_spice_maxs), default=4)
        mx_val = max(mx_val, mn_val)
        w_val = max([x[1] for x in soft_spice_mins] + [x[1] for x in soft_spice_maxs])
        priv = any(x[2] for x in soft_spice_mins + soft_spice_maxs)
        comp.soft_spice = _CompiledSpicePref(min_level=mn_val, max_level=mx_val, weight=w_val, is_private=priv)

    if soft_budget_vals:
        best_paise, best_w, best_priv = min(soft_budget_vals, key=lambda x: (x[0], -x[1]))
        comp.soft_budget_paise = _CompiledSoftPref(
            kind="budget_max_paise",
            value={"max_paise": best_paise},
            weight=best_w,
            is_private=best_priv,
        )

    # Deduplicate normalized soft preferences deterministically by canonical key
    def _dedup_soft(
        entries: list[_CompiledSoftPref],
        key_fn: Any,
    ) -> list[_CompiledSoftPref]:
        merged: dict[str, _CompiledSoftPref] = {}
        for item in entries:
            k = key_fn(item)
            if k not in merged:
                merged[k] = item
            else:
                prev = merged[k]
                merged[k] = _CompiledSoftPref(
                    kind=prev.kind,
                    value=prev.value,
                    weight=max(prev.weight, item.weight),
                    is_private=prev.is_private or item.is_private,
                )
        return [merged[k] for k in sorted(merged.keys())]

    comp.soft_cuisines = _dedup_soft(
        comp.soft_cuisines,
        lambda s: "_".join(tok for tok in _NON_ALNUM_RE.split(str(s.value.get("cuisine") or "").strip().lower()) if tok),
    )
    comp.soft_must_categories = _dedup_soft(
        comp.soft_must_categories,
        lambda s: normalize_category_id(str(s.value.get("category_id") or "")),
    )
    comp.soft_must_items = _dedup_soft(
        comp.soft_must_items,
        lambda s: str(s.value.get("item_id") or "").strip(),
    )
    comp.soft_dislikes = _dedup_soft(
        comp.soft_dislikes,
        lambda s: f"{str(s.value.get('item_id') or '').strip().upper()}|{str(s.value.get('label') or '').strip().lower()}",
    )

    # Deduplicate raw hard constraints deterministically by cid
    seen_cids: set[str] = set()
    dedup_hard: list[_CompiledHardConstraint] = []
    for hc in sorted(comp.raw_hard_constraints, key=lambda c: c.cid):
        if hc.cid not in seen_cids:
            seen_cids.add(hc.cid)
            dedup_hard.append(hc)
    comp.raw_hard_constraints = dedup_hard
    return comp


def _compute_constraints_hash(
    items: Sequence[DietaryItem],
    participants: Sequence[ParticipantInput],
    config: SolverConfig,
) -> str:
    payload = {
        "engine_version": ENGINE_VERSION,
        "delivery_fee_paise": config.delivery_fee_paise,
        "items": [
            {
                "item_id": it.item_id,
                "price_paise": it.price_paise,
                "tax_rate_bps": it.tax_rate_bps,
                "packaging_paise": it.packaging_paise,
                "is_available": it.is_available,
                "serves": it.serves,
                "shareable": it.shareable,
                "spice_level": it.spice_level,
                "verified_by": it.verified_by,
                "verified_at": it.verified_at,
            }
            for it in items
        ],
        "participants": [p.model_dump(mode="json") for p in participants],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# ---------------------------------------------------------------------------
# Step 1 & Step 2: Matching, Hard Eligibility & Soft Candidate Scoring
# ---------------------------------------------------------------------------
def _item_matches_category(item: DietaryItem, target_cat_id: str) -> bool:
    target = normalize_category_id(target_cat_id)
    if item.category and normalize_category_id(item.category) == target:
        return True
    tag_norms = {normalize_category_id(t) for t in item.tags if t}
    return target in tag_norms


def _effective_max_items_for_participant(cp: _CompiledParticipant, config: SolverConfig) -> int:
    """
    Ensures hard must-include items/categories (plus required meal slot) are never blocked
    by an artificial internal max_items_per_person cap.
    """
    req = len(cp.hard_must_items) + len(cp.hard_must_categories)
    if req > 0 and not cp.opt_out_personal_meal:
        req += 1
    return max(config.max_items_per_person, req)


def _item_matches_cuisine(item: DietaryItem, target_cuisine: str) -> Literal["match", "mismatch", "missing"]:
    target = target_cuisine.strip().lower()
    if not target:
        return "missing"

    tokens = [tok for tok in _NON_ALNUM_RE.split(target) if tok]
    if not tokens:
        return "missing"
    searchable_parts = [
        item.cuisine or "",
        " ".join(item.tags),
        item.category or "",
        item.dish_name or "",
        item.description or "",
    ]
    haystack = " ".join(searchable_parts).lower()

    if all(re.search(rf"\b{re.escape(t)}\b", haystack) for t in tokens):
        return "match"

    if item.cuisine and item.cuisine.strip():
        return "mismatch"
    return "missing"


def _item_matches_dislike(item: DietaryItem, pref_val: Mapping[str, Any]) -> bool:
    target_id = str(pref_val.get("item_id") or "").strip()
    if target_id and target_id.upper() == item.item_id.upper():
        return True
    label = str(pref_val.get("label") or "").strip().lower()
    if not label:
        return False
    haystack = " ".join([item.dish_name, item.category, " ".join(item.tags), item.description]).lower()
    if re.search(rf"\b{re.escape(label)}\b", haystack):
        return True
    return label in item.dish_name.lower()


def _spice_fit(spice_level: int | None, min_s: int, max_s: int) -> float:
    if spice_level is None:
        return 0.5  # Missing data strictly neutral 0.5, never a boost
    if min_s <= spice_level <= max_s:
        return 1.0
    dist = min(abs(spice_level - min_s), abs(spice_level - max_s))
    return max(0.0, round(1.0 - 0.35 * dist, 4))


def _evaluate_hard_eligibility(
    item: DietaryItem,
    cp: _CompiledParticipant,
) -> tuple[bool, LineExclusionReason | None]:
    """Evaluates Stage 3 dietary/allergen safety + hard availability/spice/dislike."""
    reason_codes: list[str] = []
    failed_fields: list[str] = []
    unknown_fields: list[str] = []
    constraint_kinds: list[str] = []

    if not item.is_available:
        reason_codes.append("ITEM_UNAVAILABLE")

    if cp.hard_diets or cp.hard_allergens:
        diet_res: DietaryCheckResult = is_item_allowed(item, cp.hard_dietary_constraints())
        if not diet_res.allowed:
            if cp.hard_diets:
                constraint_kinds.append("diet")
            if cp.hard_allergens:
                constraint_kinds.append("allergen")
            for unk in diet_res.unknownFields:
                if unk not in unknown_fields:
                    unknown_fields.append(unk)
                code = f"UNKNOWN_{unk.upper()}"
                if code not in reason_codes:
                    reason_codes.append(code)
            for r in diet_res.reasons:
                low = r.lower()
                if "not verified" in low:
                    if "UNVERIFIED_DIETARY_INFO" not in reason_codes:
                        reason_codes.append("UNVERIFIED_DIETARY_INFO")
                elif "unknown" in low:
                    continue
                elif "allergen" in low or "dairy" in low or "egg" in low:
                    if "ALLERGEN_OR_INGREDIENT_CONFLICT" not in reason_codes:
                        reason_codes.append("ALLERGEN_OR_INGREDIENT_CONFLICT")
                else:
                    if "DIET_RULE_CONFLICT" not in reason_codes:
                        reason_codes.append("DIET_RULE_CONFLICT")
            # Extract explicit failed fields for machine-readable reporting
            for f in unknown_fields:
                if f not in failed_fields:
                    failed_fields.append(f)
            if not item.is_verified and "verified_by" not in failed_fields:
                failed_fields.append("verified_by")
            hc_obj = cp.hard_dietary_constraints()
            diet_req_map: dict[str, tuple[tuple[str, TriState], ...]] = {
                "vegetarian": (
                    ("diet_veg", TriState.YES),
                    ("diet_non_veg", TriState.NO),
                    ("diet_egg", TriState.NO),
                    ("allergen_eggs", TriState.NO),
                ),
                "vegan": (
                    ("diet_vegan", TriState.YES),
                    ("diet_veg", TriState.YES),
                    ("diet_non_veg", TriState.NO),
                    ("contains_dairy", TriState.NO),
                    ("allergen_milk", TriState.NO),
                    ("diet_egg", TriState.NO),
                    ("allergen_eggs", TriState.NO),
                    ("contains_honey", TriState.NO),
                ),
                "jain": (
                    ("diet_jain", TriState.YES),
                    ("diet_veg", TriState.YES),
                    ("diet_non_veg", TriState.NO),
                    ("diet_egg", TriState.NO),
                    ("allergen_eggs", TriState.NO),
                    ("contains_onion_garlic", TriState.NO),
                    ("contains_root_veg", TriState.NO),
                ),
                "halal": (("diet_halal", TriState.YES),),
                "gluten_free": (("allergen_cereals_gluten", TriState.NO),),
            }
            for d_name in sorted(hc_obj.diets):
                for fname, exp in diet_req_map.get(d_name, ()):
                    if getattr(item, fname) != exp and fname not in failed_fields:
                        failed_fields.append(fname)
                if d_name == "gluten_free" and item.diet_gluten_free == TriState.NO and "diet_gluten_free" not in failed_fields:
                    failed_fields.append("diet_gluten_free")
            for a_name in sorted(hc_obj.allergens):
                for fname in ALLERGEN_RULE_FIELDS.get(a_name, ()):
                    if getattr(item, fname) != TriState.NO and fname not in failed_fields:
                        failed_fields.append(fname)
                if a_name == "eggs" and item.diet_egg == TriState.YES and "diet_egg" not in failed_fields:
                    failed_fields.append("diet_egg")
                if a_name == "milk" and item.contains_dairy == TriState.YES and "contains_dairy" not in failed_fields:
                    failed_fields.append("contains_dairy")

    # Hard spice check
    if cp.hard_spice_min is not None or cp.hard_spice_max is not None:
        mn = cp.hard_spice_min if cp.hard_spice_min is not None else 0
        mx = cp.hard_spice_max if cp.hard_spice_max is not None else 4
        if item.spice_level is None:
            if "spice" not in constraint_kinds:
                constraint_kinds.append("spice")
            reason_codes.append("UNKNOWN_SPICE_LEVEL")
            unknown_fields.append("spice_level")
        elif item.spice_level < mn or item.spice_level > mx:
            if "spice" not in constraint_kinds:
                constraint_kinds.append("spice")
            reason_codes.append("HARD_SPICE_OUT_OF_RANGE")
            failed_fields.append("spice_level")

    # Hard dislike check
    if item.item_id in cp.hard_dislike_items or any(
        _item_matches_dislike(item, {"label": lbl}) for lbl in cp.hard_dislike_labels
    ):
        constraint_kinds.append("dislike_item")
        reason_codes.append("HARD_DISLIKED_ITEM")

    if not reason_codes:
        return True, None

    return False, LineExclusionReason(
        participant_id=cp.participant_id,
        reason_codes=sorted(set(reason_codes)),
        failed_fields=sorted(set(failed_fields)),
        unknown_fields=sorted(set(unknown_fields)),
        constraint_kinds=sorted(set(constraint_kinds)),
    )


def score_item_for_participant(
    item: DietaryItem,
    participant: ParticipantInput | _CompiledParticipant,
) -> float:
    """
    Step 2: Computes per-item soft preference satisfaction score in [0.0, 1.0].
    - Incorporates spice fit, cuisine, must-include categories/items weighted by weight,
      and dislikes as strong negatives.
    - Missing data returns strictly neutral 0.5, never a boost.
    """
    cp = participant if isinstance(participant, _CompiledParticipant) else _compile_participant(participant)

    weighted_sum = 0.0
    total_weight = 0.0

    if cp.soft_spice is not None:
        w = float(cp.soft_spice.weight)
        s_fit = _spice_fit(item.spice_level, cp.soft_spice.min_level, cp.soft_spice.max_level)
        weighted_sum += w * s_fit
        total_weight += w

    for c_pref in cp.soft_cuisines:
        w = float(c_pref.weight)
        res = _item_matches_cuisine(item, str(c_pref.value.get("cuisine") or ""))
        c_fit = 1.0 if res == "match" else (0.25 if res == "mismatch" else 0.5)
        weighted_sum += w * c_fit
        total_weight += w

    for mc in cp.soft_must_categories:
        w = float(mc.weight)
        if not item.category.strip() and not item.tags:
            m_fit = 0.5  # missing data -> neutral 0.5
        elif _item_matches_category(item, str(mc.value.get("category_id") or "")):
            m_fit = 1.0
        else:
            m_fit = 0.5
        weighted_sum += w * m_fit
        total_weight += w

    for mi in cp.soft_must_items:
        w = float(mi.weight)
        m_fit = 1.0 if item.item_id == str(mi.value.get("item_id") or "") else 0.5
        weighted_sum += w * m_fit
        total_weight += w

    disliked_hit = False
    for dl in cp.soft_dislikes:
        # Dislikes are strong negatives
        w = float(max(dl.weight, 4))
        if _item_matches_dislike(item, dl.value):
            disliked_hit = True
            weighted_sum += w * 0.0
        else:
            weighted_sum += w * 0.5
        total_weight += w

    if total_weight == 0.0:
        return 0.5

    score = weighted_sum / total_weight
    if disliked_hit:
        # Strong negative ceiling so disliked items never outrank neutral items
        score = min(score, 0.15)
    return round(max(0.0, min(1.0, score)), 4)


def _compute_participant_satisfaction_raw(
    cp: _CompiledParticipant,
    consumed_items: Sequence[DietaryItem],
    total_cost_paise: int,
    hard_met: bool,
    *,
    p_idx: int | None = None,
    consumed_idxs: Sequence[int] | None = None,
    cache: _EvalCache | None = None,
) -> tuple[float, int, int]:
    """Fast zero-Pydantic satisfaction computation returning (satisfaction, soft_met, soft_total)."""
    weighted_sum = 0.0
    total_weight = 0.0
    soft_met = 0
    soft_total = 0
    fast_c = cache if (cache is not None and p_idx is not None and consumed_idxs is not None) else None
    fast_p = p_idx if p_idx is not None else 0
    fast_idxs = consumed_idxs if consumed_idxs is not None else ()

    for k_mc, mc in enumerate(cp.soft_must_categories):
        soft_total += 1
        if fast_c is not None:
            mc_tbl = fast_c.mc_match[fast_p][k_mc]
            met = any(mc_tbl[i] for i in fast_idxs)
        else:
            cat_id = str(mc.value.get("category_id") or "")
            met = any(_item_matches_category(it, cat_id) for it in consumed_items)
        if met:
            soft_met += 1
        weighted_sum += mc.weight * (1.0 if met else 0.0)
        total_weight += mc.weight

    for k_mi, mi in enumerate(cp.soft_must_items):
        soft_total += 1
        if fast_c is not None:
            mi_tbl = fast_c.mi_match[fast_p][k_mi]
            met = any(mi_tbl[i] for i in fast_idxs)
        else:
            target_id = str(mi.value.get("item_id") or "")
            met = any(it.item_id == target_id for it in consumed_items)
        if met:
            soft_met += 1
        weighted_sum += mi.weight * (1.0 if met else 0.0)
        total_weight += mi.weight

    for k_dl, dl in enumerate(cp.soft_dislikes):
        soft_total += 1
        if fast_c is not None:
            dl_tbl = fast_c.dl_match[fast_p][k_dl]
            violated = any(dl_tbl[i] for i in fast_idxs)
        else:
            violated = any(_item_matches_dislike(it, dl.value) for it in consumed_items)
        eff_w = float(max(dl.weight, 4))
        if not violated:
            soft_met += 1
        weighted_sum += eff_w * (0.0 if violated else 1.0)
        total_weight += eff_w

    if cp.soft_spice is not None:
        soft_total += 1
        if not consumed_items:
            s_score = 0.5
            s_met = False
        else:
            if fast_c is not None:
                sp_tbl = fast_c.sp_fit[fast_p]
                fits = [sp_tbl[i] for i in fast_idxs]
            else:
                fits = [_spice_fit(it.spice_level, cp.soft_spice.min_level, cp.soft_spice.max_level) for it in consumed_items]
            best_f = max(fits)
            mean_f = sum(fits) / len(fits)
            s_score = 0.5 * best_f + 0.5 * mean_f
            s_met = best_f >= 0.99 and mean_f >= 0.5
        if s_met:
            soft_met += 1
        weighted_sum += cp.soft_spice.weight * s_score
        total_weight += cp.soft_spice.weight

    for k_cu, cu in enumerate(cp.soft_cuisines):
        soft_total += 1
        if not consumed_items:
            c_score = 0.5
            c_met = False
        elif fast_c is not None:
            cu_tbl = fast_c.cu_status[fast_p][k_cu]
            best_st = max(cu_tbl[i] for i in fast_idxs)
            if best_st == 2:
                c_score = 1.0
                c_met = True
            elif best_st == 1:
                c_score = 0.5
                c_met = False
            else:
                c_score = 0.25
                c_met = False
        else:
            c_name = str(cu.value.get("cuisine") or "")
            statuses = [_item_matches_cuisine(it, c_name) for it in consumed_items]
            if "match" in statuses:
                c_score = 1.0
                c_met = True
            elif "missing" in statuses:
                c_score = 0.5
                c_met = False
            else:
                c_score = 0.25
                c_met = False
        if c_met:
            soft_met += 1
        weighted_sum += cu.weight * c_score
        total_weight += cu.weight

    if cp.soft_budget_paise is not None:
        soft_total += 1
        cap = int(cp.soft_budget_paise.value["max_paise"])
        if total_cost_paise <= cap:
            b_score = 1.0
            soft_met += 1
        else:
            b_score = max(0.0, 1.0 - (total_cost_paise - cap) / max(1, cap))
        weighted_sum += cp.soft_budget_paise.weight * b_score
        total_weight += cp.soft_budget_paise.weight

    if soft_total == 0:
        satisfaction = 1.0 if hard_met else 0.0
    else:
        satisfaction = round(max(0.0, min(1.0, weighted_sum / total_weight)), 4)

    return satisfaction, soft_met, soft_total


def _evaluate_participant_satisfaction(
    cp: _CompiledParticipant,
    consumed_items: Sequence[DietaryItem],
    total_cost_paise: int,
    hard_met: bool,
) -> ParticipantSatisfactionReport:
    """Computes Step 10 per-participant satisfaction {0..1, hard_constraints_met, soft_met, soft_total}."""
    satisfaction, soft_met, soft_total = _compute_participant_satisfaction_raw(
        cp, consumed_items, total_cost_paise, hard_met
    )
    return ParticipantSatisfactionReport(
        participant_id=cp.participant_id,
        satisfaction=satisfaction,
        hard_constraints_met=hard_met,
        soft_met=soft_met,
        soft_total=soft_total,
    )


# ---------------------------------------------------------------------------
# Candidate Selection (keeps 12 x 100 fast while preserving feasibility)
# ---------------------------------------------------------------------------
def _select_candidate_indices(
    items: Sequence[DietaryItem],
    participants: Sequence[_CompiledParticipant],
    allowed: list[list[bool]],
    item_scores: list[list[float]],
    personal_costs: list[int],
    delivery_shares: list[int],
    pricing_engine: PricingEngine,
    cap: int,
) -> list[int]:
    m = len(items)
    n = len(participants)
    if m <= cap:
        return list(range(m))

    chosen: set[int] = set()
    allow_counts = [sum(1 for q in range(n) if allowed[i][q]) for i in range(m)]
    pers_aff_cache: dict[tuple[int, int], bool] = {}
    shared_aff_cache: dict[tuple[int, int], bool] = {}

    def _is_affordable_personally(i: int, p_idx: int) -> bool:
        key = (i, p_idx)
        cached = pers_aff_cache.get(key)
        if cached is not None:
            return cached
        b = participants[p_idx].hard_budget_paise
        res = b is None or personal_costs[i] + delivery_shares[p_idx] <= b
        pers_aff_cache[key] = res
        return res

    def _is_affordable_shared(i: int, p_idx: int) -> bool:
        key = (i, p_idx)
        cached = shared_aff_cache.get(key)
        if cached is not None:
            return cached
        if not items[i].shareable or allow_counts[i] < 2:
            shared_aff_cache[key] = False
            return False
        b_self = participants[p_idx].hard_budget_paise
        # Find largest allowed peer subset including p_idx that can co-share
        peers = [q for q in range(n) if allowed[i][q]]
        for k_try in range(len(peers), 1, -1):
            uc = pricing_engine.estimate_line_participant_upper_bound(
                unit_price_paise=items[i].price_paise,
                tax_rate_bps=items[i].tax_rate_bps,
                packaging_paise=items[i].packaging_paise,
                quantity=1,
                num_sharers=k_try,
            )
            if b_self is not None and uc + delivery_shares[p_idx] > b_self:
                continue
            aff_others = 0
            for q in peers:
                if q != p_idx:
                    qb = participants[q].hard_budget_paise
                    if qb is None or uc + delivery_shares[q] <= qb:
                        aff_others += 1
            if aff_others >= k_try - 1:
                shared_aff_cache[key] = True
                return True
        shared_aff_cache[key] = False
        return False

    # 1. Include all hard/soft must_include_item targets and hard must_include_category covers
    must_item_ids: set[str] = set()
    for p in participants:
        must_item_ids.update(p.hard_must_items)
        for mi in p.soft_must_items:
            must_item_ids.add(str(mi.value.get("item_id") or ""))
    for idx, it in enumerate(items):
        if it.item_id in must_item_ids:
            chosen.add(idx)

    for p_idx, cp in enumerate(participants):
        for cat_id in sorted(cp.hard_must_categories):
            cat_matches = [i for i in range(m) if allowed[i][p_idx] and _item_matches_category(items[i], cat_id)]
            if cat_matches:
                cat_matches.sort(key=lambda i: (personal_costs[i], -item_scores[i][p_idx], items[i].item_id))
                chosen.add(cat_matches[0])

    # 2. Feasibility cover: ensure each non-opted-out participant has at least 1 affordable non-drink item
    # (prefer reusing an already-chosen affordable item before adding another!)
    for p_idx, cp in enumerate(participants):
        if cp.opt_out_personal_meal:
            continue
        avail_non_drink = [
            i for i in range(m) if allowed[i][p_idx] and not is_drink_item(items[i])
        ]
        if not avail_non_drink:
            continue
        has_chosen_affordable = any(
            i in chosen and (_is_affordable_personally(i, p_idx) or _is_affordable_shared(i, p_idx))
            for i in avail_non_drink
        )
        if not has_chosen_affordable:
            avail_non_drink.sort(key=lambda i: (personal_costs[i], -item_scores[i][p_idx], items[i].item_id))
            chosen.add(avail_non_drink[0])
            avail_shareable = [
                i for i in avail_non_drink if items[i].shareable and allow_counts[i] >= 2
            ]
            if avail_shareable:
                avail_shareable.sort(
                    key=lambda i: (
                        pricing_engine.estimate_line_participant_upper_bound(
                            unit_price_paise=items[i].price_paise,
                            tax_rate_bps=items[i].tax_rate_bps,
                            packaging_paise=items[i].packaging_paise,
                            quantity=1,
                            num_sharers=allow_counts[i],
                        ),
                        items[i].item_id,
                    )
                )
                chosen.add(avail_shareable[0])

    # 3. Prioritize high-weight soft must_include_categories (ensuring AFFORDABLE options aren't shadowed!)
    if len(chosen) < cap:
        cat_reqs: list[tuple[int, int, str]] = []
        for p_idx, cp in enumerate(participants):
            for mc in cp.soft_must_categories:
                cat_reqs.append((-mc.weight, p_idx, str(mc.value.get("category_id") or "")))
        cat_reqs.sort()
        for _, p_idx, cat_id in cat_reqs:
            if len(chosen) >= cap:
                break
            cat_matches = [
                i for i in range(m) if allowed[i][p_idx] and _item_matches_category(items[i], cat_id)
            ]
            if not cat_matches:
                continue
            # Skip only if an affordable match is ALREADY in chosen
            if any(
                i in chosen and (_is_affordable_personally(i, p_idx) or _is_affordable_shared(i, p_idx))
                for i in cat_matches
            ):
                continue
            pers_aff = [i for i in cat_matches if _is_affordable_personally(i, p_idx)]
            shar_aff = [i for i in cat_matches if _is_affordable_shared(i, p_idx)]
            if pers_aff:
                pers_aff.sort(
                    key=lambda i: (-item_scores[i][p_idx], -allow_counts[i], personal_costs[i], items[i].item_id)
                )
                chosen.add(pers_aff[0])
            if shar_aff and len(chosen) < cap:
                shar_aff.sort(
                    key=lambda i: (-item_scores[i][p_idx], -allow_counts[i], personal_costs[i], items[i].item_id)
                )
                if shar_aff[0] not in chosen:
                    chosen.add(shar_aff[0])
            if not pers_aff and not shar_aff and not any(i in chosen for i in cat_matches):
                cat_matches.sort(
                    key=lambda i: (-item_scores[i][p_idx], -allow_counts[i], personal_costs[i], items[i].item_id)
                )
                chosen.add(cat_matches[0])

    # 3b. Top affordable personal and shareable dishes per participant
    if len(chosen) < cap:
        for p_idx, cp in enumerate(participants):
            if len(chosen) >= cap:
                break
            if cp.opt_out_personal_meal:
                continue
            avail_non_drink = [
                i for i in range(m) if allowed[i][p_idx] and not is_drink_item(items[i])
            ]
            affordable = [
                i
                for i in avail_non_drink
                if _is_affordable_personally(i, p_idx) or _is_affordable_shared(i, p_idx)
            ]
            affordable.sort(
                key=lambda i: (-item_scores[i][p_idx], -allow_counts[i], personal_costs[i], items[i].item_id)
            )
            for i in affordable[:2]:
                if len(chosen) >= cap:
                    break
                chosen.add(i)

    # 4. Fill remaining capacity up to `cap` with top shared dishes across the group
    shared_pool = [
        i
        for i in range(m)
        if i not in chosen and items[i].shareable and allow_counts[i] >= 2
    ]

    def _shared_rank(i: int) -> tuple[float, int, int, str]:
        allow_ps = [q for q in range(n) if allowed[i][q]]
        avg_sc = sum(item_scores[i][q] for q in allow_ps) / len(allow_ps)
        est_cost = pricing_engine.estimate_line_participant_upper_bound(
            unit_price_paise=items[i].price_paise,
            tax_rate_bps=items[i].tax_rate_bps,
            packaging_paise=items[i].packaging_paise,
            quantity=1,
            num_sharers=min(len(allow_ps), max(2, items[i].serves)),
        )
        return (-round(avg_sc, 4), -len(allow_ps), est_cost, items[i].item_id)

    shared_pool.sort(key=_shared_rank)
    for i in shared_pool:
        if len(chosen) >= cap:
            break
        chosen.add(i)

    return sorted(chosen)


# ---------------------------------------------------------------------------
# Fast Greedy Initial Solution Builder (used for CP-SAT Hint & Python Fallback)
# ---------------------------------------------------------------------------
class _RawAssignment:
    def __init__(self, shared: dict[int, list[int]], personal: dict[int, int]) -> None:
        # shared: item_idx -> sorted list of participant indices (len >= 2)
        # personal: participant_idx -> item_idx
        self.shared = shared
        self.personal = personal


def _greedy_feasible_seed(
    items: Sequence[DietaryItem],
    participants: Sequence[_CompiledParticipant],
    allowed: list[list[bool]],
    item_scores: list[list[float]],
    personal_costs: list[int],
    delivery_shares: list[int],
    pricing_engine: PricingEngine,
    *,
    use_lower_bound: bool = False,
    config: SolverConfig | None = None,
    banned_shared_items: set[int] | None = None,
) -> _RawAssignment | None:
    """
    Fast deterministic greedy constructive heuristic finding a valid feasible
    assignment respecting all hard constraints (diets, allergens, budgets,
    hard spice/must-include/dislikes, >=1 non-drink meal unless opted out).
    """
    n = len(participants)
    m = len(items)
    if n == 0:
        return None
    cfg = config or SolverConfig()
    eff_caps = [_effective_max_items_for_participant(cp, cfg) for cp in participants]
    banned_sh = banned_shared_items or set()

    def _share_unit_cost(item_idx: int, k: int) -> int:
        it = items[item_idx]
        if use_lower_bound:
            return pricing_engine.estimate_line_participant_lower_bound(
                unit_price_paise=it.price_paise,
                tax_rate_bps=it.tax_rate_bps,
                packaging_paise=it.packaging_paise,
                quantity=1,
                num_sharers=k,
            )
        return pricing_engine.estimate_line_participant_upper_bound(
            unit_price_paise=it.price_paise,
            tax_rate_bps=it.tax_rate_bps,
            packaging_paise=it.packaging_paise,
            quantity=1,
            num_sharers=k,
        )

    budgets = [cp.hard_budget_paise for cp in participants]
    rem_budget: list[int | None] = [
        None if b is None else (b - delivery_shares[p]) for p, b in enumerate(budgets)
    ]
    for p, rb in enumerate(rem_budget):
        if rb is not None and rb < 0:
            return None

    personal: dict[int, int] = {}
    shared: dict[int, list[int]] = {}
    has_meal = [cp.opt_out_personal_meal for cp in participants]
    item_count_p = [0] * n

    def _try_share_item_for(idx: int, p: int) -> bool:
        if not items[idx].shareable or idx in banned_sh:
            return False
        if idx in shared:
            cur_s = list(shared[idx])
            if p in cur_s:
                return True
            if len(shared) > cfg.max_shared_lines:
                return False
            new_s = sorted([*cur_s, p])
            old_uc = _share_unit_cost(idx, len(cur_s))
            new_uc = _share_unit_cost(idx, len(new_s))
            for q in new_s:
                if q == p:
                    if item_count_p[q] >= eff_caps[q]:
                        return False
                    if rem_budget[q] is not None and new_uc > rem_budget[q]:  # type: ignore[operator]
                        return False
                else:
                    if rem_budget[q] is not None and (rem_budget[q] + old_uc) < new_uc:  # type: ignore[operator]
                        return False
            # Commit join
            for q in cur_s:
                if rem_budget[q] is not None:
                    rem_budget[q] = rem_budget[q] + old_uc - new_uc  # type: ignore[operator]
            if rem_budget[p] is not None:
                rem_budget[p] = rem_budget[p] - new_uc  # type: ignore[operator]
            if not is_drink_item(items[idx]):
                has_meal[p] = True
            item_count_p[p] += 1
            shared[idx] = new_s
            return True

        if len(shared) >= cfg.max_shared_lines:
            return False
        others = [
            q
            for q in range(n)
            if q != p
            and allowed[idx][q]
            and personal.get(q) != idx
            and item_count_p[q] < eff_caps[q]
        ]
        if not others:
            return False
        others.sort(key=lambda q: (-item_scores[idx][q], q))
        sharers = [p]
        for q in others:
            k_next = len(sharers) + 1
            u_cost = _share_unit_cost(idx, k_next)
            if all(
                rem_budget[s] is None or u_cost <= rem_budget[s]  # type: ignore[operator]
                for s in (*sharers, q)
            ):
                sharers.append(q)
        if len(sharers) < 2:
            return False
        u_cost = _share_unit_cost(idx, len(sharers))
        shared[idx] = sorted(sharers)
        for s in sharers:
            if rem_budget[s] is not None:
                rem_budget[s] = rem_budget[s] - u_cost  # type: ignore[operator]
            if not is_drink_item(items[idx]):
                has_meal[s] = True
            item_count_p[s] += 1
        return True

    def _assign_required_item(idx: int, p: int) -> bool:
        if idx in shared and p in shared[idx]:
            return True
        if personal.get(p) == idx:
            return True
        if item_count_p[p] >= eff_caps[p]:
            return False
        cost = personal_costs[idx]
        rb = rem_budget[p]
        # Prefer personal if participant has no personal slot yet & can afford & not opted out
        if (
            p not in personal
            and not participants[p].opt_out_personal_meal
            and (rb is None or cost <= rb)
        ):
            personal[p] = idx
            if rem_budget[p] is not None:
                rem_budget[p] = rem_budget[p] - cost  # type: ignore[operator]
            if not is_drink_item(items[idx]):
                has_meal[p] = True
            item_count_p[p] += 1
            return True
        # Otherwise try sharing idx directly
        if _try_share_item_for(idx, p):
            return True
        # Or if p already has a shareable personal item, try moving personal[p] to shared and putting idx in personal[p]
        if p in personal and not participants[p].opt_out_personal_meal and (rb is None or cost <= rb):
            old_idx = personal[p]
            if items[old_idx].shareable:
                old_cost = personal_costs[old_idx]
                del personal[p]
                item_count_p[p] -= 1
                if rem_budget[p] is not None:
                    rem_budget[p] = rem_budget[p] + old_cost  # type: ignore[operator]
                if (
                    _try_share_item_for(old_idx, p)
                    and item_count_p[p] < eff_caps[p]
                    and (rem_budget[p] is None or cost <= rem_budget[p])  # type: ignore[operator]
                ):
                    personal[p] = idx
                    if rem_budget[p] is not None:
                        rem_budget[p] = rem_budget[p] - cost  # type: ignore[operator]
                    if not is_drink_item(items[idx]):
                        has_meal[p] = True
                    item_count_p[p] += 1
                    return True
                # Rollback if move failed
                if old_idx in shared and p in shared[old_idx]:
                    pass
                else:
                    personal[p] = old_idx
                    item_count_p[p] += 1
                    if rem_budget[p] is not None:
                        rem_budget[p] = rem_budget[p] - old_cost  # type: ignore[operator]
        return False

    # 1. Satisfy any HARD must_include_item / must_include_category first
    for p, cp in enumerate(participants):
        for iid in sorted(cp.hard_must_items):
            matches = [i for i in range(m) if items[i].item_id == iid and allowed[i][p]]
            if not matches or not _assign_required_item(matches[0], p):
                return None

        for cat in sorted(cp.hard_must_categories):
            consumed_idxs = [i for i, ps in shared.items() if p in ps]
            if p in personal:
                consumed_idxs.append(personal[p])
            if any(_item_matches_category(items[i], cat) for i in consumed_idxs):
                continue
            cat_matches = [i for i in range(m) if allowed[i][p] and _item_matches_category(items[i], cat)]
            cat_matches.sort(key=lambda i: (personal_costs[i], -item_scores[i][p], items[i].item_id))
            if not any(_assign_required_item(i, p) for i in cat_matches):
                return None

    # 2. Try high-value shared meals for uncovered participants (especially tight budgets!)
    shared_candidates = [
        i
        for i in range(m)
        if i not in banned_sh and items[i].shareable and not is_drink_item(items[i]) and sum(1 for q in range(n) if allowed[i][q]) >= 2
    ]
    shared_candidates.sort(
        key=lambda i: (
            _share_unit_cost(i, min(n, max(2, sum(1 for q in range(n) if allowed[i][q])))),
            -sum(item_scores[i][q] for q in range(n) if allowed[i][q]),
            items[i].item_id,
        )
    )

    for idx in shared_candidates:
        if all(has_meal):
            break
        if idx in shared or len(shared) >= cfg.max_shared_lines:
            continue
        elig = [
            p
            for p in range(n)
            if not has_meal[p]
            and allowed[idx][p]
            and personal.get(p) != idx
            and item_count_p[p] < eff_caps[p]
        ]
        if len(elig) < 2:
            # Also allow already-covered participants with spare budget to co-share for a tight-budget peer
            elig_all = [
                p
                for p in range(n)
                if allowed[idx][p] and personal.get(p) != idx and item_count_p[p] < eff_caps[p]
            ]
        else:
            elig_all = elig

        if len(elig_all) < 2:
            continue

        # Sort uncovered first, then highest score
        elig_all.sort(key=lambda p: (0 if not has_meal[p] else 1, -item_scores[idx][p], p))
        chosen_s: list[int] = []
        for k_try in range(len(elig_all), 1, -1):
            sub = elig_all[:k_try]
            if not any(not has_meal[p] for p in sub):
                continue
            uc = _share_unit_cost(idx, k_try)
            if all(rem_budget[p] is None or uc <= rem_budget[p] for p in sub):  # type: ignore[operator]
                chosen_s = sub
                break
        if len(chosen_s) >= 2:
            uc = _share_unit_cost(idx, len(chosen_s))
            shared[idx] = sorted(chosen_s)
            for p in chosen_s:
                if rem_budget[p] is not None:
                    rem_budget[p] = rem_budget[p] - uc  # type: ignore[operator]
                has_meal[p] = True
                item_count_p[p] += 1

    # 3. Cover remaining non-opted-out participants with best affordable personal non-drink meal
    for p in range(n):
        if has_meal[p]:
            continue
        if p in personal or item_count_p[p] >= eff_caps[p]:
            return None
        rb = rem_budget[p]
        cands = [
            i
            for i in range(m)
            if allowed[i][p]
            and not is_drink_item(items[i])
            and (rb is None or personal_costs[i] <= rb)
            and p not in shared.get(i, [])
        ]
        if not cands:
            return None
        cands.sort(key=lambda i: (-item_scores[i][p], personal_costs[i], items[i].item_id))
        pick = cands[0]
        personal[p] = pick
        if rem_budget[p] is not None:
            rem_budget[p] = rem_budget[p] - personal_costs[pick]  # type: ignore[operator]
        has_meal[p] = True
        item_count_p[p] += 1

    # 4. Verify any HARD must_include_categories are met
    for p, cp in enumerate(participants):
        consumed_idxs = [i for i, ps in shared.items() if p in ps]
        if p in personal:
            consumed_idxs.append(personal[p])
        for cat in cp.hard_must_categories:
            if not any(_item_matches_category(items[i], cat) for i in consumed_idxs):
                return None

    return _RawAssignment(shared=shared, personal=personal)


# ---------------------------------------------------------------------------
# CP-SAT Primary Solver
# ---------------------------------------------------------------------------
def _extract_raw_assignment(
    cands: Sequence[int],
    n: int,
    share: Mapping[tuple[int, int], Any],
    personal: Mapping[tuple[int, int], Any],
    best_vals: Mapping[int, int],
) -> _RawAssignment:
    sh_out: dict[int, list[int]] = {}
    pers_out: dict[int, int] = {}
    for j, orig_i in enumerate(cands):
        sharers = [p for p in range(n) if (j, p) in share and best_vals[share[j, p].index] == 1]
        if len(sharers) >= 2:
            sh_out[orig_i] = sorted(sharers)
        for p in range(n):
            if (j, p) in personal and best_vals[personal[j, p].index] == 1:
                pers_out[p] = orig_i
    return _RawAssignment(shared=sh_out, personal=pers_out)


def _compute_exact_totals_by_p(
    assignment: _RawAssignment,
    items: Sequence[DietaryItem],
    participants: Sequence[_CompiledParticipant],
    delivery_fee_paise: int,
    pricing_engine: PricingEngine,
) -> dict[str, int]:
    pids = [cp.participant_id for cp in participants]
    if type(pricing_engine) is StubPricingEngine:
        del_map = split_integer_paise(delivery_fee_paise, pids) if pids else {}
        totals_by_p: dict[str, int] = {pid: del_map.get(pid, 0) for pid in pids}
        for orig_i in sorted(assignment.shared.keys()):
            sharer_idxs = sorted(assignment.shared[orig_i])
            it = items[orig_i]
            c_pids = [participants[p].participant_id for p in sharer_idxs]
            l_items = it.price_paise
            l_tax = calculate_line_tax_paise(l_items, it.tax_rate_bps)
            l_pkg = it.packaging_paise
            i_sp = split_integer_paise(l_items, c_pids)
            t_sp = split_integer_paise(l_tax, c_pids)
            p_sp = split_integer_paise(l_pkg, c_pids)
            for pid in c_pids:
                totals_by_p[pid] += i_sp[pid] + t_sp[pid] + p_sp[pid]
        for p_idx in sorted(assignment.personal.keys()):
            orig_i = assignment.personal[p_idx]
            it = items[orig_i]
            pid = participants[p_idx].participant_id
            l_items = it.price_paise
            l_tax = calculate_line_tax_paise(l_items, it.tax_rate_bps)
            totals_by_p[pid] += l_items + l_tax + it.packaging_paise
        return totals_by_p

    lines: list[PricingLineInput] = []
    for orig_i in sorted(assignment.shared.keys()):
        sharer_idxs = sorted(assignment.shared[orig_i])
        it = items[orig_i]
        c_pids = [participants[p].participant_id for p in sharer_idxs]
        lines.append(
            PricingLineInput(
                menu_item_id=it.item_id,
                dish_name=it.dish_name,
                quantity=1,
                kind="SHARED",
                consumer_participant_ids=c_pids,
                unit_price_paise=it.price_paise,
                tax_rate_bps=it.tax_rate_bps,
                packaging_paise=it.packaging_paise,
            )
        )
    for p_idx in sorted(assignment.personal.keys()):
        orig_i = assignment.personal[p_idx]
        it = items[orig_i]
        pid = participants[p_idx].participant_id
        lines.append(
            PricingLineInput(
                menu_item_id=it.item_id,
                dish_name=it.dish_name,
                quantity=1,
                kind="PERSONAL",
                consumer_participant_ids=[pid],
                unit_price_paise=it.price_paise,
                tax_rate_bps=it.tax_rate_bps,
                packaging_paise=it.packaging_paise,
            )
        )
    pricing = pricing_engine.calculate_proposal_pricing(
        lines,
        pids,
        delivery_fee_paise=delivery_fee_paise,
    )
    return {pid: alloc.total_paise for pid, alloc in pricing.allocations.items()}


def _solve_ortools(
    items: Sequence[DietaryItem],
    participants: Sequence[_CompiledParticipant],
    allowed: list[list[bool]],
    item_scores: list[list[float]],
    personal_costs: list[int],
    delivery_shares: list[int],
    pricing_engine: PricingEngine,
    config: SolverConfig,
    *,
    hard_only: bool = False,
    use_lower_bound: bool = False,
) -> _RawAssignment | None:
    """
    Solves the Table Split assignment problem via OR-Tools CP-SAT with single-worker
    seeded determinism. Returns None if infeasible within hard constraints.
    """
    if not _HAS_ORTOOLS or cp_model is None:  # pragma: no cover
        return None

    n = len(participants)
    m_full = len(items)
    if n == 0:
        return None

    eff_caps = [_effective_max_items_for_participant(cp, config) for cp in participants]

    # Fast deterministic necessary check before constructing CP-SAT
    for p, cp in enumerate(participants):
        if cp.hard_budget_paise is not None and delivery_shares[p] > cp.hard_budget_paise:
            return None
        if not cp.opt_out_personal_meal:
            # Must have at least 1 allowed non-drink item with min possible cost <= budget
            has_any = False
            for i in range(m_full):
                if not allowed[i][p] or is_drink_item(items[i]):
                    continue
                if cp.hard_budget_paise is None:
                    has_any = True
                    break
                allowed_count = sum(1 for q in range(n) if allowed[i][q])
                if items[i].shareable and allowed_count >= 2:
                    min_c = pricing_engine.estimate_line_participant_lower_bound(
                        unit_price_paise=items[i].price_paise,
                        tax_rate_bps=items[i].tax_rate_bps,
                        packaging_paise=items[i].packaging_paise,
                        quantity=1,
                        num_sharers=allowed_count,
                    )
                else:
                    min_c = personal_costs[i]
                if min_c + delivery_shares[p] <= cp.hard_budget_paise:
                    has_any = True
                    break
            if not has_any:
                return None

    cands = _select_candidate_indices(
        items,
        participants,
        allowed,
        item_scores,
        personal_costs,
        delivery_shares,
        pricing_engine,
        cap=m_full if (hard_only or m_full <= 40) else config.candidate_cap_for_large_menus,
    )

    seed = _greedy_feasible_seed(
        items,
        participants,
        allowed,
        item_scores,
        personal_costs,
        delivery_shares,
        pricing_engine,
        use_lower_bound=use_lower_bound,
        config=config,
    )
    if (
        hard_only
        and seed is not None
        and _score_assignment_tuple(
            seed,
            items,
            participants,
            config.delivery_fee_paise,
            pricing_engine,
            config=config,
            enforce_upper_bound=not use_lower_bound,
        )
        is not None
    ):
        return seed
    if seed is not None:
        cand_set = set(cands)
        cand_set.update(seed.shared.keys())
        cand_set.update(seed.personal.values())
        cands = sorted(cand_set)

    m = len(cands)
    if m == 0:
        if all(cp.opt_out_personal_meal and not cp.hard_must_items and not cp.hard_must_categories for cp in participants):
            return _RawAssignment(shared={}, personal={})
        return None

    cand_idx_by_orig = {orig_i: j for j, orig_i in enumerate(cands)}
    model = cp_model.CpModel()
    personal: dict[tuple[int, int], Any] = {}
    share: dict[tuple[int, int], Any] = {}
    is_shared: list[Any] = [model.new_bool_var(f"sh_{j}") for j in range(m)]
    unit_cost: list[Any] = []
    cost_share: dict[tuple[int, int], Any] = {}
    k_inds_map: dict[tuple[int, int], Any] = {}
    waste_vars: list[Any] = []
    all_vars: list[Any] = list(is_shared)

    def _share_cost_fn(orig_i: int, k: int) -> int:
        it = items[orig_i]
        if use_lower_bound:
            return pricing_engine.estimate_line_participant_lower_bound(
                unit_price_paise=it.price_paise,
                tax_rate_bps=it.tax_rate_bps,
                packaging_paise=it.packaging_paise,
                quantity=1,
                num_sharers=k,
            )
        return pricing_engine.estimate_line_participant_upper_bound(
            unit_price_paise=it.price_paise,
            tax_rate_bps=it.tax_rate_bps,
            packaging_paise=it.packaging_paise,
            quantity=1,
            num_sharers=k,
        )

    for j, orig_i in enumerate(cands):
        it = items[orig_i]
        p_cost = personal_costs[orig_i]
        uc = model.new_int_var(0, p_cost, f"uc_{j}")
        unit_cost.append(uc)
        all_vars.append(uc)

        allowed_p = [p for p in range(n) if allowed[orig_i][p]]
        sh_vars: list[Any] = []
        cs_vars: list[Any] = []

        for p in allowed_p:
            pv = model.new_bool_var(f"pers_{j}_{p}")
            sv = model.new_bool_var(f"s_{j}_{p}")
            personal[j, p] = pv
            share[j, p] = sv
            all_vars.extend([pv, sv])
            sh_vars.append(sv)
            model.add(pv + sv <= 1)

            cs = model.new_int_var(0, p_cost, f"cs_{j}_{p}")
            cost_share[j, p] = cs
            cs_vars.append(cs)
            all_vars.append(cs)
            model.add(cs == uc).only_enforce_if(sv)
            model.add(cs == 0).only_enforce_if(sv.Not())

            if it.serves > 1 and not hard_only:
                pw = model.new_int_var(0, it.serves - 1, f"pw_{j}_{p}")
                model.add(pw == (it.serves - 1) * pv)
                waste_vars.append(pw)
                all_vars.append(pw)

        if it.shareable and len(allowed_p) >= 2:
            k_inds: list[tuple[int, Any]] = []
            for k in range(2, len(allowed_p) + 1):
                ki = model.new_bool_var(f"ki_{j}_{k}")
                k_inds.append((k, ki))
                k_inds_map[j, k] = ki
                all_vars.append(ki)
            model.add_exactly_one([ki for _, ki in k_inds] + [is_shared[j].Not()])
            model.add(sum(sh_vars) == sum(k * ki for k, ki in k_inds))
            model.add(uc == sum(_share_cost_fn(orig_i, k) * ki for k, ki in k_inds))

            if not hard_only:
                sw = model.new_int_var(0, max(n, it.serves), f"sw_{j}")
                model.add(sw == sum(abs(it.serves - k) * ki for k, ki in k_inds))
                waste_vars.append(sw)
                all_vars.append(sw)
        else:
            model.add(is_shared[j] == 0)
            model.add(uc == 0)
            for p in allowed_p:
                model.add(share[j, p] == 0)
                model.add(cost_share[j, p] == 0)

    # Participant hard constraints
    max_possible_budget = max(
        (
            cp.hard_budget_paise
            if cp.hard_budget_paise is not None
            else (sum(personal_costs[i] for i in cands) + delivery_shares[p])
        )
        for p, cp in enumerate(participants)
    )
    person_costs: list[Any] = []
    consumed_count_vars: list[Any] = []
    has_any_item_vars: list[Any] = []

    for p, cp in enumerate(participants):
        p_pers = [personal[j, p] for j in range(m) if (j, p) in personal]
        model.add(sum(p_pers) <= 1)
        if cp.opt_out_personal_meal:
            model.add(sum(p_pers) == 0)
        else:
            non_drink_consumed = [
                personal[j, p] + share[j, p]
                for j, orig_i in enumerate(cands)
                if (j, p) in personal and not is_drink_item(items[orig_i])
            ]
            if not non_drink_consumed:
                return None
            model.add(sum(non_drink_consumed) >= 1)

        all_consumed = [personal[j, p] + share[j, p] for j in range(m) if (j, p) in personal]
        model.add(sum(all_consumed) <= eff_caps[p])
        cnt_v = model.new_int_var(0, eff_caps[p], f"cnt_{p}")
        model.add(cnt_v == sum(all_consumed))
        has_any_v = model.new_bool_var(f"any_{p}")
        if all_consumed:
            model.add(cnt_v >= 1).only_enforce_if(has_any_v)
            model.add(cnt_v == 0).only_enforce_if(has_any_v.Not())
        else:
            model.add(has_any_v == 0)
        consumed_count_vars.append(cnt_v)
        has_any_item_vars.append(has_any_v)
        all_vars.extend([cnt_v, has_any_v])

        # Hard must-include items & categories
        for iid in sorted(cp.hard_must_items):
            terms = [
                personal[j, p] + share[j, p]
                for j, orig_i in enumerate(cands)
                if (j, p) in personal and items[orig_i].item_id == iid
            ]
            if not terms:
                return None
            model.add(sum(terms) >= 1)

        for cat in sorted(cp.hard_must_categories):
            terms = [
                personal[j, p] + share[j, p]
                for j, orig_i in enumerate(cands)
                if (j, p) in personal and _item_matches_category(items[orig_i], cat)
            ]
            if not terms:
                return None
            model.add(sum(terms) >= 1)

        ub = cp.hard_budget_paise if cp.hard_budget_paise is not None else max_possible_budget
        if delivery_shares[p] > ub:
            return None
        pc = model.new_int_var(delivery_shares[p], ub, f"pc_{p}")
        model.add(
            pc
            == delivery_shares[p]
            + sum(
                personal[j, p] * personal_costs[cands[j]] + cost_share[j, p]
                for j in range(m)
                if (j, p) in personal
            )
        )
        person_costs.append(pc)
        all_vars.append(pc)

    model.add(sum(is_shared) <= config.max_shared_lines)

    # Pure feasibility check shortcut (used by MUC core extraction!)
    if hard_only:
        s_feas = cp_model.CpSolver()
        s_feas.parameters.num_search_workers = 1
        s_feas.parameters.random_seed = 42
        s_feas.parameters.cp_model_probing_level = 0
        s_feas.parameters.max_time_in_seconds = max(0.05, config.time_limit_ms / 1000.0)
        st = s_feas.solve(model)
        if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return None
        feas_vals = {v.index: s_feas.value(v) for v in all_vars}
        return _extract_raw_assignment(cands, n, share, personal, feas_vals)

    # Exact soft satisfaction variables (0..10000 per participant, 1-to-1 with _compute_participant_satisfaction_raw)
    sat_vars: list[Any] = []
    for p, cp in enumerate(participants):
        weighted_expr_terms: list[Any] = []
        total_w = 0
        has_any_v = has_any_item_vars[p]
        cnt_v = consumed_count_vars[p]

        for idx_mc, mc in enumerate(cp.soft_must_categories):
            cat_id = str(mc.value.get("category_id") or "")
            met = model.new_bool_var(f"mc_{p}_{idx_mc}")
            all_vars.append(met)
            terms = [
                personal[j, p] + share[j, p]
                for j, orig_i in enumerate(cands)
                if (j, p) in personal and _item_matches_category(items[orig_i], cat_id)
            ]
            if terms:
                model.add(sum(terms) >= 1).only_enforce_if(met)
                model.add(sum(terms) == 0).only_enforce_if(met.Not())
            else:
                model.add(met == 0)
            weighted_expr_terms.append(mc.weight * 1000 * met)
            total_w += mc.weight

        for idx_mi, mi in enumerate(cp.soft_must_items):
            target_id = str(mi.value.get("item_id") or "")
            met = model.new_bool_var(f"mi_{p}_{idx_mi}")
            all_vars.append(met)
            terms = [
                personal[j, p] + share[j, p]
                for j, orig_i in enumerate(cands)
                if (j, p) in personal and items[orig_i].item_id == target_id
            ]
            if terms:
                model.add(sum(terms) >= 1).only_enforce_if(met)
                model.add(sum(terms) == 0).only_enforce_if(met.Not())
            else:
                model.add(met == 0)
            weighted_expr_terms.append(mi.weight * 1000 * met)
            total_w += mi.weight

        for idx_dl, dl in enumerate(cp.soft_dislikes):
            eff_w = max(dl.weight, 4)
            viol = model.new_bool_var(f"dl_{p}_{idx_dl}")
            all_vars.append(viol)
            terms = [
                personal[j, p] + share[j, p]
                for j, orig_i in enumerate(cands)
                if (j, p) in personal and _item_matches_dislike(items[orig_i], dl.value)
            ]
            if terms:
                model.add(sum(terms) >= 1).only_enforce_if(viol)
                model.add(sum(terms) == 0).only_enforce_if(viol.Not())
            else:
                model.add(viol == 0)
            weighted_expr_terms.append(eff_w * 1000 * (1 - viol))
            total_w += eff_w

        if cp.soft_spice is not None:
            mn_s = cp.soft_spice.min_level
            mx_s = cp.soft_spice.max_level
            exact_t: list[Any] = []
            near1_t: list[Any] = []
            miss_t: list[Any] = []
            near2_t: list[Any] = []
            fit_terms: list[Any] = []
            for j, orig_i in enumerate(cands):
                if (j, p) not in personal:
                    continue
                v_cons = personal[j, p] + share[j, p]
                fit_1000 = round(_spice_fit(items[orig_i].spice_level, mn_s, mx_s) * 1000)
                fit_terms.append(fit_1000 * v_cons)
                if items[orig_i].spice_level is None:
                    miss_t.append(v_cons)
                elif fit_1000 >= 990:
                    exact_t.append(v_cons)
                elif fit_1000 >= 600:
                    near1_t.append(v_cons)
                elif fit_1000 >= 250:
                    near2_t.append(v_cons)

            has_exact = model.new_bool_var(f"sp_ex_{p}")
            has_near1 = model.new_bool_var(f"sp_n1_{p}")
            has_miss = model.new_bool_var(f"sp_ms_{p}")
            has_near2 = model.new_bool_var(f"sp_n2_{p}")
            all_vars.extend([has_exact, has_near1, has_miss, has_near2])

            for flag, grp in (
                (has_exact, exact_t),
                (has_near1, near1_t),
                (has_miss, miss_t),
                (has_near2, near2_t),
            ):
                if grp:
                    model.add(sum(grp) >= 1).only_enforce_if(flag)
                    model.add(sum(grp) == 0).only_enforce_if(flag.Not())
                else:
                    model.add(flag == 0)

            sp_best = model.new_int_var(0, 1000, f"sp_best_{p}")
            model.add(sp_best == 500).only_enforce_if(has_any_v.Not())
            model.add(sp_best == 1000).only_enforce_if([has_any_v, has_exact])
            model.add(sp_best == 650).only_enforce_if([has_any_v, has_exact.Not(), has_near1])
            model.add(sp_best == 500).only_enforce_if([has_any_v, has_exact.Not(), has_near1.Not(), has_miss])
            model.add(sp_best == 300).only_enforce_if(
                [has_any_v, has_exact.Not(), has_near1.Not(), has_miss.Not(), has_near2]
            )
            model.add(sp_best == 0).only_enforce_if(
                [has_any_v, has_exact.Not(), has_near1.Not(), has_miss.Not(), has_near2.Not()]
            )

            if m * n <= 160:
                cnt_safe = model.new_int_var(1, max(1, eff_caps[p]), f"sp_csf_{p}")
                model.add(cnt_safe == cnt_v).only_enforce_if(has_any_v)
                model.add(cnt_safe == 1).only_enforce_if(has_any_v.Not())

                sum_safe = model.new_int_var(0, 1000 * max(1, eff_caps[p]), f"sp_ssf_{p}")
                model.add(sum_safe == (sum(fit_terms) if fit_terms else 0)).only_enforce_if(has_any_v)
                model.add(sum_safe == 500).only_enforce_if(has_any_v.Not())

                sp_mean = model.new_int_var(0, 1000, f"sp_mean_{p}")
                model.add_division_equality(sp_mean, sum_safe, cnt_safe)

                sp_score = model.new_int_var(0, 1000, f"sp_sc_{p}")
                model.add(sp_score * 2 <= sp_best + sp_mean)
                model.add(sp_score * 2 >= sp_best + sp_mean - 1)
                all_vars.extend([sp_best, cnt_safe, sum_safe, sp_mean, sp_score])
            else:
                # On large models (m*n > 160), avoid division constraints in CP-SAT;
                # _polish_assignment optimizes exact mean+best spice satisfaction.
                sp_score = sp_best
                all_vars.append(sp_best)

            weighted_expr_terms.append(cp.soft_spice.weight * sp_score)
            total_w += cp.soft_spice.weight

        for idx_cu, cu in enumerate(cp.soft_cuisines):
            c_name = str(cu.value.get("cuisine") or "")
            match_t: list[Any] = []
            miss_cu_t: list[Any] = []
            for j, orig_i in enumerate(cands):
                if (j, p) not in personal:
                    continue
                res = _item_matches_cuisine(items[orig_i], c_name)
                if res == "match":
                    match_t.append(personal[j, p] + share[j, p])
                elif res == "missing":
                    miss_cu_t.append(personal[j, p] + share[j, p])

            has_cm = model.new_bool_var(f"cu_m_{p}_{idx_cu}")
            has_cms = model.new_bool_var(f"cu_ms_{p}_{idx_cu}")
            all_vars.extend([has_cm, has_cms])
            if match_t:
                model.add(sum(match_t) >= 1).only_enforce_if(has_cm)
                model.add(sum(match_t) == 0).only_enforce_if(has_cm.Not())
            else:
                model.add(has_cm == 0)
            if miss_cu_t:
                model.add(sum(miss_cu_t) >= 1).only_enforce_if(has_cms)
                model.add(sum(miss_cu_t) == 0).only_enforce_if(has_cms.Not())
            else:
                model.add(has_cms == 0)

            cu_sc = model.new_int_var(250, 1000, f"cu_sc_{p}_{idx_cu}")
            model.add(cu_sc == 500).only_enforce_if(has_any_v.Not())
            model.add(cu_sc == 1000).only_enforce_if([has_any_v, has_cm])
            model.add(cu_sc == 500).only_enforce_if([has_any_v, has_cm.Not(), has_cms])
            model.add(cu_sc == 250).only_enforce_if([has_any_v, has_cm.Not(), has_cms.Not()])
            all_vars.append(cu_sc)
            weighted_expr_terms.append(cu.weight * cu_sc)
            total_w += cu.weight

        if cp.soft_budget_paise is not None:
            cap_b = int(cp.soft_budget_paise.value["max_paise"])
            b_sc = model.new_int_var(0, 1000, f"sb_sc_{p}")
            all_vars.append(b_sc)
            if cap_b <= 0:
                sb_met = model.new_bool_var(f"sb_z_{p}")
                all_vars.append(sb_met)
                model.add(person_costs[p] == 0).only_enforce_if(sb_met)
                model.add(person_costs[p] > 0).only_enforce_if(sb_met.Not())
                model.add(b_sc == 1000 * sb_met)
            else:
                over_v = model.new_int_var(0, max_possible_budget, f"sb_over_{p}")
                model.add_max_equality(over_v, [0, person_costs[p] - cap_b])
                b_num = model.new_int_var(0, cap_b, f"sb_num_{p}")
                model.add_max_equality(b_num, [0, cap_b - over_v])
                if cap_b > 1_000_000_000:
                    cap_scaled = max(1, cap_b // 1000)
                    b_num_sc = model.new_int_var(0, cap_scaled, f"sb_ns_{p}")
                    model.add_division_equality(b_num_sc, b_num, 1000)
                    model.add_division_equality(b_sc, b_num_sc * 1000, cap_scaled)
                    all_vars.append(b_num_sc)
                else:
                    model.add_division_equality(b_sc, b_num * 1000, cap_b)
                all_vars.extend([over_v, b_num])
            weighted_expr_terms.append(cp.soft_budget_paise.weight * b_sc)
            total_w += cp.soft_budget_paise.weight

        sv = model.new_int_var(0, 10000, f"sat_{p}")
        all_vars.append(sv)
        if total_w == 0:
            model.add(sv == 10000)
        else:
            w_sum = sum(weighted_expr_terms)
            model.add(sv * total_w <= w_sum * 10)
            model.add(sv * total_w >= w_sum * 10 - (total_w - 1))
        sat_vars.append(sv)

    min_sat = model.new_int_var(0, 10000, "min_sat")
    for sv in sat_vars:
        model.add(min_sat <= sv)
    sum_sat = model.new_int_var(0, 10000 * n, "sum_sat")
    model.add(sum_sat == sum(sat_vars))

    max_cost_var = model.new_int_var(0, max_possible_budget, "max_c")
    min_cost_var = model.new_int_var(0, max_possible_budget, "min_c")
    for pc in person_costs:
        model.add(max_cost_var >= pc)
        model.add(min_cost_var <= pc)
    spread = model.new_int_var(0, max_possible_budget, "spread")
    model.add(spread == max_cost_var - min_cost_var)

    max_personal_waste = n * max(
        (max(0, items[orig_i].serves - 1) for orig_i in cands),
        default=0,
    )
    max_shared_waste = sum(max(n, items[orig_i].serves) for orig_i in cands)
    max_waste = max(1, max_personal_waste + max_shared_waste)
    total_waste = model.new_int_var(0, max_waste, "waste")
    model.add(total_waste == (sum(waste_vars) if waste_vars else 0))

    # Deterministic item_id tie-break: earlier item_id has smaller index j
    max_item_tie = max(1, m * (m + n))
    item_tie = model.new_int_var(0, max_item_tie, "item_tie")
    model.add(
        item_tie
        == sum(
            (j + 1) * (is_shared[j] + sum(personal[j, p] for p in range(n) if (j, p) in personal))
            for j in range(m)
        )
    )
    all_vars.extend([min_sat, sum_sat, max_cost_var, min_cost_var, spread, total_waste, item_tie])

    # Apply initial complete hint from greedy seed if available
    if seed is not None:
        for (j, p), pv in personal.items():
            orig_i = cands[j]
            model.add_hint(pv, 1 if seed.personal.get(p) == orig_i else 0)
        for j, orig_i in enumerate(cands):
            sharers = seed.shared.get(orig_i, [])
            k_sh = len(sharers)
            model.add_hint(is_shared[j], 1 if k_sh >= 2 else 0)
            uc_val = _share_cost_fn(orig_i, k_sh) if k_sh >= 2 else 0
            model.add_hint(unit_cost[j], uc_val)
            for p in range(n):
                if (j, p) in share:
                    in_sh = p in sharers
                    model.add_hint(share[j, p], 1 if in_sh else 0)
                    model.add_hint(cost_share[j, p], uc_val if in_sh else 0)
            for k in range(2, n + 1):
                if (j, k) in k_inds_map:
                    model.add_hint(k_inds_map[j, k], 1 if k == k_sh else 0)

    budget_sec = config.time_limit_ms / 1000.0
    det_p1 = max(0.014, budget_sec * 0.045)
    det_p2 = max(0.010, budget_sec * 0.035)
    wall_cap = max(0.45, budget_sec * 1.25)
    if m * n > 160 and seed is not None:
        det_p1 = min(det_p1, 0.008)
        det_p2 = min(det_p2, 0.005)
    tie_scale = max(1, max_item_tie + 2)
    waste_scale = max(1, max_waste * tie_scale + 1)

    max_exact_iters = 6 if use_lower_bound else 1
    raw_res: _RawAssignment | None = None

    for _cut_iter in range(max_exact_iters):
        model.maximize(min_sat * (10000 * n + 1) + sum_sat)
        s1 = cp_model.CpSolver()
        s1.parameters.num_search_workers = 1
        s1.parameters.random_seed = 42
        s1.parameters.cp_model_probing_level = 0
        s1.parameters.max_deterministic_time = det_p1
        s1.parameters.max_time_in_seconds = wall_cap
        st1 = s1.solve(model)

        if st1 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            if (
                st1 == cp_model.UNKNOWN
                and seed is not None
                and _cut_iter == 0
                and _score_assignment_tuple(
                    seed,
                    items,
                    participants,
                    config.delivery_fee_paise,
                    pricing_engine,
                    config=config,
                    enforce_upper_bound=not use_lower_bound,
                )
                is not None
            ):
                raw_res = seed
                break
            if _cut_iter == 0 and m < m_full and (st1 == cp_model.INFEASIBLE or seed is None):
                return _solve_ortools(
                    items,
                    participants,
                    allowed,
                    item_scores,
                    personal_costs,
                    delivery_shares,
                    pricing_engine,
                    SolverConfig(
                        time_limit_ms=config.time_limit_ms,
                        delivery_fee_paise=config.delivery_fee_paise,
                        max_items_per_person=config.max_items_per_person,
                        max_shared_lines=config.max_shared_lines,
                        candidate_cap_for_large_menus=max(12, m_full),
                        use_ortools=True,
                    ),
                    hard_only=hard_only,
                    use_lower_bound=use_lower_bound,
                )
            return None

        best_min_sat = s1.value(min_sat)
        best_sum_sat = s1.value(sum_sat)
        best_vals = {v.index: s1.value(v) for v in all_vars}

        # Pass 2 on cloned model for small/medium models (m * n <= 160)
        if m * n <= 160:
            m2 = model.clone()
            m2.add(m2.get_int_var_from_proto_index(min_sat.index) >= best_min_sat)
            m2.add(m2.get_int_var_from_proto_index(sum_sat.index) >= best_sum_sat)
            m2.clear_hints()
            for v in all_vars:
                v2 = (
                    m2.get_bool_var_from_proto_index(v.index)
                    if v.is_boolean
                    else m2.get_int_var_from_proto_index(v.index)
                )
                m2.add_hint(v2, best_vals[v.index])

            sp2 = m2.get_int_var_from_proto_index(spread.index)
            tw2 = m2.get_int_var_from_proto_index(total_waste.index)
            it2 = m2.get_int_var_from_proto_index(item_tie.index)
            m2.minimize(sp2 * waste_scale + tw2 * tie_scale + it2)

            s2 = cp_model.CpSolver()
            s2.parameters.num_search_workers = 1
            s2.parameters.random_seed = 42
            s2.parameters.cp_model_probing_level = 0
            s2.parameters.max_deterministic_time = det_p2
            s2.parameters.max_time_in_seconds = wall_cap
            st2 = s2.solve(m2)
            if st2 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                pass2_vals = {
                    v.index: s2.value(
                        m2.get_bool_var_from_proto_index(v.index)
                        if v.is_boolean
                        else m2.get_int_var_from_proto_index(v.index)
                    )
                    for v in all_vars
                }
                cand_p2 = _extract_raw_assignment(cands, n, share, personal, pass2_vals)
                if not use_lower_bound or _score_assignment_tuple(
                    cand_p2,
                    items,
                    participants,
                    config.delivery_fee_paise,
                    pricing_engine,
                    config=config,
                    enforce_upper_bound=False,
                ) is not None:
                    best_vals = pass2_vals

        cand_raw = _extract_raw_assignment(cands, n, share, personal, best_vals)
        if not use_lower_bound:
            raw_res = cand_raw
            break

        # Exact budget verification under lower-bound relaxation; add sound cut if remainder pushed a diner over
        exact_sc = _score_assignment_tuple(
            cand_raw,
            items,
            participants,
            config.delivery_fee_paise,
            pricing_engine,
            config=config,
            enforce_upper_bound=False,
        )
        if exact_sc is not None:
            raw_res = cand_raw
            break

        totals_exact = _compute_exact_totals_by_p(
            cand_raw, items, participants, config.delivery_fee_paise, pricing_engine
        )
        added_cut = False
        model.clear_hints()
        for p, cp in enumerate(participants):
            if cp.hard_budget_paise is not None and totals_exact[cp.participant_id] > cp.hard_budget_paise:
                lits: list[Any] = []
                for orig_i, sharers in cand_raw.shared.items():
                    if p in sharers:
                        j = cand_idx_by_orig[orig_i]
                        sh_set = set(sharers)
                        for q in range(n):
                            if (j, q) in share:
                                lits.append(share[j, q] if q in sh_set else share[j, q].Not())
                if p in cand_raw.personal:
                    j_p = cand_idx_by_orig[cand_raw.personal[p]]
                    lits.append(personal[j_p, p])
                if lits:
                    model.add(sum(lits) <= len(lits) - 1)
                    added_cut = True
        if not added_cut:
            return None

    if raw_res is None:
        return None

    return _polish_assignment(
        raw_res,
        cands,
        items,
        participants,
        allowed,
        item_scores,
        config,
        pricing_engine,
        max_rounds=1 if (m * n <= 160) else 3,
        enforce_upper_bound=not use_lower_bound,
        light_only=(m * n <= 100),
    )


# ---------------------------------------------------------------------------
# Pure-Python Deterministic Fallback Solver & Deterministic Polish
# ---------------------------------------------------------------------------
class _EvalCache:
    """Precomputed lookup tables accelerating inner-loop objective scoring."""

    def __init__(
        self,
        items: Sequence[DietaryItem],
        participants: Sequence[_CompiledParticipant],
        delivery_fee_paise: int,
        pricing_engine: PricingEngine,
        config: SolverConfig,
    ) -> None:
        self.cfg = config
        self.is_stub = type(pricing_engine) is StubPricingEngine
        self.pids = [cp.participant_id for cp in participants]
        self.eff_caps = [_effective_max_items_for_participant(cp, config) for cp in participants]
        self.is_drink = [is_drink_item(it) for it in items]
        self.line_tax = [
            (it.price_paise * it.tax_rate_bps + 5000) // 10000
            for it in items
        ]
        self.personal_total = [
            it.price_paise + self.line_tax[i] + it.packaging_paise
            for i, it in enumerate(items)
        ]
        self.del_map = (
            split_integer_paise(delivery_fee_paise, self.pids)
            if (self.is_stub and self.pids)
            else {}
        )
        self.ub_cache: dict[tuple[int, int], int] = {}
        if self.is_stub:
            n = len(participants)
            for i, it in enumerate(items):
                if it.shareable and n >= 2:
                    li = it.price_paise
                    lt = self.line_tax[i]
                    lp = it.packaging_paise
                    for k in range(2, n + 1):
                        self.ub_cache[i, k] = (
                            (li + k - 1) // k
                            + (lt + k - 1) // k
                            + (lp + k - 1) // k
                        )

        # Precompute O(1) soft-preference match tables per (participant, item_idx)
        self.mc_match: list[list[list[bool]]] = []
        self.mi_match: list[list[list[bool]]] = []
        self.dl_match: list[list[list[bool]]] = []
        self.sp_fit: list[list[float]] = []
        self.cu_status: list[list[list[int]]] = []
        for cp in participants:
            self.mc_match.append(
                [
                    [_item_matches_category(it, str(mc.value.get("category_id") or "")) for it in items]
                    for mc in cp.soft_must_categories
                ]
            )
            self.mi_match.append(
                [
                    [it.item_id == str(mi.value.get("item_id") or "") for it in items]
                    for mi in cp.soft_must_items
                ]
            )
            self.dl_match.append(
                [
                    [_item_matches_dislike(it, dl.value) for it in items]
                    for dl in cp.soft_dislikes
                ]
            )
            if cp.soft_spice is not None:
                mn_s = cp.soft_spice.min_level
                mx_s = cp.soft_spice.max_level
                self.sp_fit.append([_spice_fit(it.spice_level, mn_s, mx_s) for it in items])
            else:
                self.sp_fit.append([])
            cu_rows: list[list[int]] = []
            for cu in cp.soft_cuisines:
                c_name = str(cu.value.get("cuisine") or "")
                cu_rows.append(
                    [
                        2 if st == "match" else (1 if st == "missing" else 0)
                        for it in items
                        for st in (_item_matches_cuisine(it, c_name),)
                    ]
                )
            self.cu_status.append(cu_rows)


def _score_assignment_tuple(
    assignment: _RawAssignment,
    items: Sequence[DietaryItem],
    participants: Sequence[_CompiledParticipant],
    delivery_fee_paise: int,
    pricing_engine: PricingEngine,
    *,
    config: SolverConfig | None = None,
    enforce_upper_bound: bool = False,
    cache: _EvalCache | None = None,
) -> tuple[int, float, float, int, int, tuple[str, ...]] | None:
    """
    Evaluates a candidate assignment against hard constraints + lexicographic objective:
    Returns (hard_ok, min_sat, avg_sat, -spread, -waste, item_tie_ids) or None if invalid.
    """
    cfg = cache.cfg if cache is not None else (config or SolverConfig())
    if len(assignment.shared) > cfg.max_shared_lines:
        return None

    n = len(participants)
    pids = cache.pids if cache is not None else [cp.participant_id for cp in participants]
    consumed_by_p: list[list[DietaryItem]] = [[] for _ in range(n)]
    consumed_idxs_by_p: list[list[int]] = [[] for _ in range(n)]
    has_non_drink_p: list[bool] = [False] * n

    # Fast zero-Pydantic path when using StubPricingEngine
    if (cache.is_stub if cache is not None else (type(pricing_engine) is StubPricingEngine)):
        del_map = cache.del_map if cache is not None else (split_integer_paise(delivery_fee_paise, pids) if pids else {})
        totals_by_p: list[int] = [del_map.get(pid, 0) for pid in pids]

        for orig_i, raw_sharers in assignment.shared.items():
            sharer_idxs = sorted(raw_sharers)
            k = len(sharer_idxs)
            it = items[orig_i]
            if k < 2 or not it.shareable:
                return None
            non_dr = not (cache.is_drink[orig_i] if cache is not None else is_drink_item(it))
            if enforce_upper_bound:
                ub_unit = (
                    cache.ub_cache[orig_i, k]
                    if cache is not None
                    else pricing_engine.estimate_line_participant_upper_bound(
                        unit_price_paise=it.price_paise,
                        tax_rate_bps=it.tax_rate_bps,
                        packaging_paise=it.packaging_paise,
                        quantity=1,
                        num_sharers=k,
                    )
                )
                for p_i in sharer_idxs:
                    totals_by_p[p_i] += ub_unit
                    consumed_by_p[p_i].append(it)
                    consumed_idxs_by_p[p_i].append(orig_i)
                    if non_dr:
                        has_non_drink_p[p_i] = True
            else:
                l_items = it.price_paise
                l_tax = cache.line_tax[orig_i] if cache is not None else calculate_line_tax_paise(l_items, it.tax_rate_bps)
                l_pkg = it.packaging_paise
                b_i, r_i = divmod(l_items, k)
                b_t, r_t = divmod(l_tax, k)
                b_p, r_p = divmod(l_pkg, k)
                # sharer_idxs are sorted in canonical ascending participant_id order
                for pos, p_i in enumerate(sharer_idxs):
                    totals_by_p[p_i] += (
                        b_i + (1 if pos < r_i else 0)
                        + b_t + (1 if pos < r_t else 0)
                        + b_p + (1 if pos < r_p else 0)
                    )
                    consumed_by_p[p_i].append(it)
                    consumed_idxs_by_p[p_i].append(orig_i)
                    if non_dr:
                        has_non_drink_p[p_i] = True

        for p_idx, orig_i in assignment.personal.items():
            if p_idx in assignment.shared.get(orig_i, ()):
                return None
            it = items[orig_i]
            non_dr = not (cache.is_drink[orig_i] if cache is not None else is_drink_item(it))
            totals_by_p[p_idx] += (
                cache.personal_total[orig_i]
                if cache is not None
                else (it.price_paise + calculate_line_tax_paise(it.price_paise, it.tax_rate_bps) + it.packaging_paise)
            )
            consumed_by_p[p_idx].append(it)
            consumed_idxs_by_p[p_idx].append(orig_i)
            if non_dr:
                has_non_drink_p[p_idx] = True
    else:
        lines: list[PricingLineInput] = []
        for orig_i in sorted(assignment.shared.keys()):
            sharer_idxs = sorted(assignment.shared[orig_i])
            if len(sharer_idxs) < 2 or not items[orig_i].shareable:
                return None
            it = items[orig_i]
            c_pids = [pids[p] for p in sharer_idxs]
            lines.append(
                PricingLineInput(
                    menu_item_id=it.item_id,
                    dish_name=it.dish_name,
                    quantity=1,
                    kind="SHARED",
                    consumer_participant_ids=c_pids,
                    unit_price_paise=it.price_paise,
                    tax_rate_bps=it.tax_rate_bps,
                    packaging_paise=it.packaging_paise,
                )
            )
            non_dr = not (cache.is_drink[orig_i] if cache is not None else is_drink_item(it))
            for p_i in sharer_idxs:
                consumed_by_p[p_i].append(it)
                consumed_idxs_by_p[p_i].append(orig_i)
                if non_dr:
                    has_non_drink_p[p_i] = True

        for p_idx in sorted(assignment.personal.keys()):
            orig_i = assignment.personal[p_idx]
            if p_idx in assignment.shared.get(orig_i, ()):
                return None
            it = items[orig_i]
            pid = pids[p_idx]
            lines.append(
                PricingLineInput(
                    menu_item_id=it.item_id,
                    dish_name=it.dish_name,
                    quantity=1,
                    kind="PERSONAL",
                    consumer_participant_ids=[pid],
                    unit_price_paise=it.price_paise,
                    tax_rate_bps=it.tax_rate_bps,
                    packaging_paise=it.packaging_paise,
                )
            )
            consumed_by_p[p_idx].append(it)
            consumed_idxs_by_p[p_idx].append(orig_i)
            if not (cache.is_drink[orig_i] if cache is not None else is_drink_item(it)):
                has_non_drink_p[p_idx] = True

        pricing = pricing_engine.calculate_proposal_pricing(
            lines,
            pids,
            delivery_fee_paise=delivery_fee_paise,
        )
        totals_by_p = [pricing.allocations[pid].total_paise for pid in pids]

    sats: list[float] = []
    costs: list[int] = []
    for p_idx, cp in enumerate(participants):
        c_items = consumed_by_p[p_idx]
        eff_cap = cache.eff_caps[p_idx] if cache is not None else _effective_max_items_for_participant(cp, cfg)
        if len(c_items) > eff_cap:
            return None
        if cp.opt_out_personal_meal and p_idx in assignment.personal:
            return None
        if not cp.opt_out_personal_meal and not has_non_drink_p[p_idx]:
            return None
        if cp.hard_must_items:
            c_ids = {it.item_id for it in c_items}
            if not cp.hard_must_items.issubset(c_ids):
                return None
        for cat in cp.hard_must_categories:
            if not any(_item_matches_category(it, cat) for it in c_items):
                return None
        total_p = totals_by_p[p_idx]
        if cp.hard_budget_paise is not None and total_p > cp.hard_budget_paise:
            return None
        costs.append(total_p)
        sat_val, _, _ = _compute_participant_satisfaction_raw(
            cp,
            c_items,
            total_p,
            True,
            p_idx=p_idx,
            consumed_idxs=consumed_idxs_by_p[p_idx],
            cache=cache,
        )
        sats.append(sat_val)

    min_sat = min(sats) if sats else 1.0
    avg_sat = sum(sats) / max(1, len(sats))
    spread = (max(costs) - min(costs)) if costs else 0
    waste = 0
    for orig_i, sharers in assignment.shared.items():
        waste += abs(items[orig_i].serves - len(sharers))
    for orig_i in assignment.personal.values():
        if items[orig_i].serves > 1:
            waste += items[orig_i].serves - 1

    tie_ids = tuple(
        sorted([items[i].item_id for i in assignment.shared] + [items[i].item_id for i in assignment.personal.values()])
    )
    return (1, min_sat, avg_sat, -spread, -waste, tuple(str(x) for x in tie_ids))


def _is_better_score(
    cand: tuple[int, float, float, int, int, tuple[str, ...]],
    best: tuple[int, float, float, int, int, tuple[str, ...]],
) -> bool:
    if cand[:5] != best[:5]:
        return cand[:5] > best[:5]
    # Lexicographically smaller item IDs win tie-breaks
    return cand[5] < best[5]


def _polish_assignment(
    initial: _RawAssignment,
    cands: Sequence[int],
    items: Sequence[DietaryItem],
    participants: Sequence[_CompiledParticipant],
    allowed: list[list[bool]],
    item_scores: list[list[float]],
    config: SolverConfig,
    pricing_engine: PricingEngine,
    *,
    max_rounds: int = 2,
    enforce_upper_bound: bool = False,
    light_only: bool = False,
) -> _RawAssignment:
    """
    Deterministic local-improvement pass optimizing the exact 5-level lexicographic
    objective + item_id tie-breaks without ever degrading a higher objective.
    """
    n = len(participants)
    eval_cache = _EvalCache(items, participants, config.delivery_fee_paise, pricing_engine, config)
    best_assign = _RawAssignment(
        shared={k: list(v) for k, v in initial.shared.items()},
        personal=dict(initial.personal),
    )
    best_score = _score_assignment_tuple(
        best_assign,
        items,
        participants,
        config.delivery_fee_paise,
        pricing_engine,
        config=config,
        enforce_upper_bound=enforce_upper_bound,
        cache=eval_cache,
    )
    if best_score is None:
        return initial

    for _round in range(max_rounds):
        improved = False

        # 1. Drop redundant personal items when a participant is already covered
        for p in sorted(best_assign.personal.keys()):
            trial_pers = dict(best_assign.personal)
            del trial_pers[p]
            trial = _RawAssignment(shared=dict(best_assign.shared), personal=trial_pers)
            sc = _score_assignment_tuple(
                trial,
                items,
                participants,
                config.delivery_fee_paise,
                pricing_engine,
                config=config,
                enforce_upper_bound=enforce_upper_bound,
                cache=eval_cache,
            )
            if sc is not None and _is_better_score(sc, best_score):
                best_assign = trial
                best_score = sc
                improved = True

        # 2. Merge 2+ identical personal items of a shareable dish into a single SHARED line
        by_pers_item: dict[int, list[int]] = {}
        for p_idx, orig_i in best_assign.personal.items():
            if items[orig_i].shareable:
                by_pers_item.setdefault(orig_i, []).append(p_idx)
        for orig_i in sorted(by_pers_item.keys()):
            p_list = sorted(by_pers_item[orig_i])
            if len(p_list) >= 2 and orig_i not in best_assign.shared:
                trial_sh = dict(best_assign.shared)
                trial_sh[orig_i] = p_list
                trial_pers = dict(best_assign.personal)
                for p in p_list:
                    trial_pers.pop(p, None)
                trial = _RawAssignment(shared=trial_sh, personal=trial_pers)
                sc = _score_assignment_tuple(
                    trial,
                    items,
                    participants,
                    config.delivery_fee_paise,
                    pricing_engine,
                    config=config,
                    enforce_upper_bound=enforce_upper_bound,
                    cache=eval_cache,
                )
                if sc is not None and _is_better_score(sc, best_score):
                    best_assign = trial
                    best_score = sc
                    improved = True

        if not light_only:
            # 3. Swap each participant's personal item across cands (or add if needed for unmet soft prefs)
            for p in range(n):
                if participants[p].opt_out_personal_meal:
                    continue
                has_pers = p in best_assign.personal
                if not has_pers and best_score[1] >= 1.0 and best_score[2] >= 1.0:
                    continue
                for idx in cands:
                    if not allowed[idx][p] or eval_cache.is_drink[idx]:
                        continue
                    if p in best_assign.shared.get(idx, []):
                        continue
                    if best_assign.personal.get(p) == idx:
                        continue
                    trial_pers = dict(best_assign.personal)
                    trial_pers[p] = idx
                    trial = _RawAssignment(shared=dict(best_assign.shared), personal=trial_pers)
                    sc = _score_assignment_tuple(
                        trial,
                        items,
                        participants,
                        config.delivery_fee_paise,
                        pricing_engine,
                        config=config,
                        enforce_upper_bound=enforce_upper_bound,
                        cache=eval_cache,
                    )
                    if sc is not None and _is_better_score(sc, best_score):
                        best_assign = trial
                        best_score = sc
                        improved = True

            # 4a. Replace an existing shared line with a better shared line for the same sharers
            for old_idx in list(best_assign.shared.keys()):
                for idx in cands:
                    if old_idx not in best_assign.shared:
                        break
                    cur_sharers = best_assign.shared[old_idx]
                    if idx == old_idx or not items[idx].shareable or eval_cache.is_drink[idx]:
                        continue
                    if idx in best_assign.shared:
                        continue
                    if not all(allowed[idx][p] for p in cur_sharers):
                        continue
                    trial_sh = dict(best_assign.shared)
                    del trial_sh[old_idx]
                    trial_sh[idx] = list(cur_sharers)
                    trial_pers = {p: i for p, i in best_assign.personal.items() if not (i == idx and p in cur_sharers)}
                    trial = _RawAssignment(shared=trial_sh, personal=trial_pers)
                    sc = _score_assignment_tuple(
                        trial,
                        items,
                        participants,
                        config.delivery_fee_paise,
                        pricing_engine,
                        config=config,
                        enforce_upper_bound=enforce_upper_bound,
                        cache=eval_cache,
                    )
                    if sc is not None and _is_better_score(sc, best_score):
                        best_assign = trial
                        best_score = sc
                        improved = True
                        break

            # 4b. Introduce or adjust shared lines across allowed subsets
            for idx in cands:
                if not items[idx].shareable or eval_cache.is_drink[idx]:
                    continue
                if idx not in best_assign.shared and len(best_assign.shared) >= config.max_shared_lines:
                    continue
                allowed_p = [p for p in range(n) if allowed[idx][p]]
                if len(allowed_p) < 2:
                    continue

                candidate_subsets: list[list[int]] = [list(allowed_p)]
                scored_p = sorted(allowed_p, key=lambda p: (-item_scores[idx][p], p))
                def _budget_desc_key(p_idx: int, _idx: int = idx) -> tuple[int, int, float, int]:
                    hb = participants[p_idx].hard_budget_paise
                    if hb is None:
                        return (1, 0, -item_scores[_idx][p_idx], p_idx)
                    return (0, -hb, -item_scores[_idx][p_idx], p_idx)

                budget_p = sorted(allowed_p, key=_budget_desc_key)
                if len(cands) * n > 160:
                    srv = items[idx].serves
                    k_sizes = [srv] if 2 <= srv < len(allowed_p) else []
                else:
                    k_sizes = sorted(
                        k for k in {2, 3, items[idx].serves} if 2 <= k < len(allowed_p)
                    )
                for k in k_sizes:
                    sub1 = sorted(scored_p[:k])
                    if sub1 not in candidate_subsets:
                        candidate_subsets.append(sub1)
                    sub2 = sorted(budget_p[:k])
                    if sub2 not in candidate_subsets:
                        candidate_subsets.append(sub2)

                for sub in candidate_subsets:
                    seen_pers_variants: list[dict[int, int]] = []
                    for drop_pers in (True, False):
                        trial_pers = dict(best_assign.personal)
                        if drop_pers:
                            for p in sub:
                                trial_pers.pop(p, None)
                        else:
                            for p in sub:
                                if trial_pers.get(p) == idx:
                                    trial_pers.pop(p, None)
                        if trial_pers in seen_pers_variants:
                            continue
                        seen_pers_variants.append(trial_pers)
                        if (
                            best_assign.shared.get(idx) == sub
                            and trial_pers == best_assign.personal
                        ):
                            continue
                        trial_sh = dict(best_assign.shared)
                        trial_sh[idx] = sub
                        trial = _RawAssignment(shared=trial_sh, personal=trial_pers)
                        sc = _score_assignment_tuple(
                            trial,
                            items,
                            participants,
                            config.delivery_fee_paise,
                            pricing_engine,
                            config=config,
                            enforce_upper_bound=enforce_upper_bound,
                            cache=eval_cache,
                        )
                        if sc is not None and _is_better_score(sc, best_score):
                            best_assign = trial
                            best_score = sc
                            improved = True

        # 5. Drop redundant shared items
        for idx in list(best_assign.shared.keys()):
            trial_sh = dict(best_assign.shared)
            del trial_sh[idx]
            trial = _RawAssignment(shared=trial_sh, personal=dict(best_assign.personal))
            sc = _score_assignment_tuple(
                trial,
                items,
                participants,
                config.delivery_fee_paise,
                pricing_engine,
                config=config,
                enforce_upper_bound=enforce_upper_bound,
                cache=eval_cache,
            )
            if sc is not None and _is_better_score(sc, best_score):
                best_assign = trial
                best_score = sc
                improved = True

        if not improved:
            break

    return best_assign


def _solve_python_fallback(
    items: Sequence[DietaryItem],
    participants: Sequence[_CompiledParticipant],
    allowed: list[list[bool]],
    item_scores: list[list[float]],
    personal_costs: list[int],
    delivery_shares: list[int],
    pricing_engine: PricingEngine,
    config: SolverConfig,
    *,
    max_rounds: int = 3,
    enforce_upper_bound: bool = False,
) -> _RawAssignment | None:
    """
    Pure-Python deterministic constructive + local-search fallback solver.
    Used when OR-Tools is unavailable or explicitly disabled.
    """
    n = len(participants)
    m = len(items)
    if n == 0:
        return None

    best_assign = _greedy_feasible_seed(
        items,
        participants,
        allowed,
        item_scores,
        personal_costs,
        delivery_shares,
        pricing_engine,
        use_lower_bound=False,
        config=config,
    )
    best_score = (
        _score_assignment_tuple(
            best_assign,
            items,
            participants,
            config.delivery_fee_paise,
            pricing_engine,
            config=config,
            enforce_upper_bound=enforce_upper_bound,
        )
        if best_assign is not None
        else None
    )

    if best_score is None and not enforce_upper_bound:
        banned_sh: set[int] = set()
        for _attempt in range(4):
            lb_seed = _greedy_feasible_seed(
                items,
                participants,
                allowed,
                item_scores,
                personal_costs,
                delivery_shares,
                pricing_engine,
                use_lower_bound=True,
                config=config,
                banned_shared_items=banned_sh,
            )
            if lb_seed is None:
                break
            lb_sc = _score_assignment_tuple(
                lb_seed,
                items,
                participants,
                config.delivery_fee_paise,
                pricing_engine,
                config=config,
                enforce_upper_bound=False,
            )
            if lb_sc is not None:
                best_assign = lb_seed
                best_score = lb_sc
                break

            # Try exact-remainder repairs or ban the over-budget shared line(s) for retry
            totals_ex = _compute_exact_totals_by_p(
                lb_seed, items, participants, config.delivery_fee_paise, pricing_engine
            )
            over_ps = [
                p
                for p, cp in enumerate(participants)
                if cp.hard_budget_paise is not None and totals_ex[cp.participant_id] > cp.hard_budget_paise
            ]
            repaired = False
            for p_over in over_ps:
                for alt_i in range(m):
                    if not allowed[alt_i][p_over] or is_drink_item(items[alt_i]):
                        continue
                    trial_sh = {
                        k: [q for q in ps if q != p_over]
                        for k, ps in lb_seed.shared.items()
                    }
                    trial_sh = {k: ps for k, ps in trial_sh.items() if len(ps) >= 2}
                    trial_pers = dict(lb_seed.personal)
                    trial_pers[p_over] = alt_i
                    trial = _RawAssignment(shared=trial_sh, personal=trial_pers)
                    sc = _score_assignment_tuple(
                        trial,
                        items,
                        participants,
                        config.delivery_fee_paise,
                        pricing_engine,
                        config=config,
                        enforce_upper_bound=False,
                    )
                    if sc is not None:
                        best_assign = trial
                        best_score = sc
                        repaired = True
                        break
                if repaired:
                    break
            if repaired:
                break

            newly_banned = {
                sh_i
                for sh_i, ps in lb_seed.shared.items()
                if any(p in ps for p in over_ps)
            }
            if not newly_banned or newly_banned.issubset(banned_sh):
                break
            banned_sh.update(newly_banned)

    if best_assign is None or best_score is None:
        return None

    cands = _select_candidate_indices(
        items,
        participants,
        allowed,
        item_scores,
        personal_costs,
        delivery_shares,
        pricing_engine,
        cap=min(m, 30),
    )

    return _polish_assignment(
        best_assign,
        cands,
        items,
        participants,
        allowed,
        item_scores,
        config,
        pricing_engine,
        max_rounds=max_rounds,
        enforce_upper_bound=enforce_upper_bound,
    )


# ---------------------------------------------------------------------------
# Step 6: Unmet Must-Include Explanation & Same-Category Alternatives
# ---------------------------------------------------------------------------
def _min_shared_unit_cost_for_item(
    it: DietaryItem,
    num_allowed: int,
    pricing_engine: PricingEngine,
) -> int:
    if not it.shareable or num_allowed < 2:
        return pricing_engine.estimate_line_participant_upper_bound(
            unit_price_paise=it.price_paise,
            tax_rate_bps=it.tax_rate_bps,
            packaging_paise=it.packaging_paise,
            quantity=1,
            num_sharers=1,
        )
    return pricing_engine.estimate_line_participant_upper_bound(
        unit_price_paise=it.price_paise,
        tax_rate_bps=it.tax_rate_bps,
        packaging_paise=it.packaging_paise,
        quantity=1,
        num_sharers=num_allowed,
    )


def _find_best_same_category_alt_cost(
    alt_it: DietaryItem,
    alt_personal_cost: int,
    p_idx: int,
    participants: Sequence[_CompiledParticipant],
    exclusion_map: Mapping[str, Mapping[str, LineExclusionReason]],
    delivery_shares: Sequence[int],
    pricing_engine: PricingEngine,
) -> int | None:
    cp = participants[p_idx]
    budget_cap = cp.hard_budget_paise
    aff_personal = budget_cap is None or alt_personal_cost + delivery_shares[p_idx] <= budget_cap

    shared_min_est: int | None = None
    if alt_it.shareable:
        allowed_peers = [
            q_idx
            for q_idx, q_cp in enumerate(participants)
            if exclusion_map.get(alt_it.item_id, {}).get(q_cp.participant_id) is None
        ]
        for k_try in range(len(allowed_peers), 1, -1):
            uc = pricing_engine.estimate_line_participant_upper_bound(
                unit_price_paise=alt_it.price_paise,
                tax_rate_bps=alt_it.tax_rate_bps,
                packaging_paise=alt_it.packaging_paise,
                quantity=1,
                num_sharers=k_try,
            )
            if budget_cap is not None and uc + delivery_shares[p_idx] > budget_cap:
                continue
            aff_others = 0
            for q_idx in allowed_peers:
                if q_idx != p_idx:
                    qb = participants[q_idx].hard_budget_paise
                    if qb is None or uc + delivery_shares[q_idx] <= qb:
                        aff_others += 1
            if aff_others >= k_try - 1:
                shared_min_est = uc
                break

    if not aff_personal and shared_min_est is None:
        return None
    if shared_min_est is not None and (not aff_personal or shared_min_est < alt_personal_cost):
        return shared_min_est
    return alt_personal_cost


def _build_blocked_must_includes(
    items: Sequence[DietaryItem],
    participants: Sequence[_CompiledParticipant],
    consumed_by_p: Mapping[str, list[DietaryItem]],
    exclusion_map: Mapping[str, Mapping[str, LineExclusionReason]],
    personal_costs: Sequence[int],
    delivery_shares: Sequence[int],
    pricing_engine: PricingEngine,
) -> list[BlockedMustInclude]:
    blocked_out: list[BlockedMustInclude] = []

    for p_idx, cp in enumerate(participants):
        c_items = consumed_by_p.get(cp.participant_id, [])

        # Check must_include_category
        for mc in cp.soft_must_categories:
            cat_id = str(mc.value.get("category_id") or "")
            if any(_item_matches_category(it, cat_id) for it in c_items):
                continue
            cat_items = [i for i, it in enumerate(items) if _item_matches_category(it, cat_id)]
            blockers: list[dict[str, Any]] = []
            alternatives: list[dict[str, Any]] = []

            for i in cat_items:
                it = items[i]
                excl_self = exclusion_map.get(it.item_id, {}).get(cp.participant_id)
                if excl_self is not None:
                    blockers.append({
                        "participant_id": cp.participant_id,
                        "participant_name": cp.display_name,
                        "menu_item_id": it.item_id,
                        "dish_name": it.dish_name,
                        "reason_codes": excl_self.reason_codes,
                        "constraint_kinds": excl_self.constraint_kinds,
                        "unknown_fields": excl_self.unknown_fields,
                    })
                else:
                    # Allowed by diet/spice, check budget & sharing blockers
                    budget_cap = cp.hard_budget_paise
                    if budget_cap is not None and personal_costs[i] + delivery_shares[p_idx] > budget_cap:
                        blockers.append({
                            "participant_id": cp.participant_id,
                            "participant_name": cp.display_name,
                            "menu_item_id": it.item_id,
                            "dish_name": it.dish_name,
                            "reason_codes": ["HARD_BUDGET_EXCEEDED"],
                            "constraint_kinds": ["budget_max_paise"],
                            "required_paise": personal_costs[i] + delivery_shares[p_idx],
                            "budget_paise": budget_cap,
                        })
                        allowed_peers = [
                            q_idx
                            for q_idx, q_cp in enumerate(participants)
                            if exclusion_map.get(it.item_id, {}).get(q_cp.participant_id) is None
                        ]
                        est_shared = _min_shared_unit_cost_for_item(it, max(2, len(allowed_peers)), pricing_engine)
                        for q_idx, q_cp in enumerate(participants):
                            if q_idx == p_idx:
                                continue
                            excl_q = exclusion_map.get(it.item_id, {}).get(q_cp.participant_id)
                            if excl_q is not None:
                                blockers.append({
                                    "participant_id": q_cp.participant_id,
                                    "participant_name": q_cp.display_name,
                                    "menu_item_id": it.item_id,
                                    "dish_name": it.dish_name,
                                    "reason_codes": excl_q.reason_codes,
                                    "constraint_kinds": excl_q.constraint_kinds,
                                    "blocks_group_sharing": True,
                                })
                            elif (
                                it.shareable
                                and q_cp.hard_budget_paise is not None
                                and est_shared + delivery_shares[q_idx] > q_cp.hard_budget_paise
                            ):
                                blockers.append({
                                    "participant_id": q_cp.participant_id,
                                    "participant_name": q_cp.display_name,
                                    "menu_item_id": it.item_id,
                                    "dish_name": it.dish_name,
                                    "reason_codes": ["HARD_BUDGET_EXCEEDED"],
                                    "constraint_kinds": ["budget_max_paise"],
                                    "required_paise": est_shared + delivery_shares[q_idx],
                                    "budget_paise": q_cp.hard_budget_paise,
                                    "blocks_group_sharing": True,
                                })
                    else:
                        alt_est = _find_best_same_category_alt_cost(
                            it, personal_costs[i], p_idx, participants, exclusion_map, delivery_shares, pricing_engine
                        )
                        if alt_est is not None:
                            alternatives.append({
                                "menu_item_id": it.item_id,
                                "dish_name": it.dish_name,
                                "category": it.category,
                                "estimated_personal_paise": personal_costs[i],
                                "estimated_min_paise": alt_est,
                            })

            if not blockers:
                if any(is_drink_item(items[i]) for i in cat_items) and not cp.opt_out_personal_meal:
                    blockers.append({
                        "participant_id": cp.participant_id,
                        "participant_name": cp.display_name,
                        "category": cat_id,
                        "reason_codes": ["DRINK_NOT_A_MEAL_OR_CAPACITY_TRADEOFF"],
                        "constraint_kinds": ["meal_requirement"],
                    })
                elif cat_items:
                    blockers.append({
                        "participant_id": cp.participant_id,
                        "participant_name": cp.display_name,
                        "category": cat_id,
                        "reason_codes": ["HIGHER_OBJECTIVE_OR_CAPACITY_TRADEOFF"],
                        "constraint_kinds": ["objective_priority"],
                    })

            blocked_out.append(
                BlockedMustInclude(
                    participant_id=cp.participant_id,
                    preference_kind="must_include_category",
                    target_id=cat_id,
                    weight=mc.weight,
                    blocked_by=blockers,
                    alternatives_in_same_category=alternatives,
                )
            )

        # Check must_include_item
        for mi in cp.soft_must_items:
            target_id = str(mi.value.get("item_id") or "")
            if any(it.item_id == target_id for it in c_items):
                continue
            target_idx = next((i for i, it in enumerate(items) if it.item_id == target_id), None)
            blockers_mi: list[dict[str, Any]] = []
            alts_mi: list[dict[str, Any]] = []

            if target_idx is not None:
                target_it = items[target_idx]
                excl_self = exclusion_map.get(target_it.item_id, {}).get(cp.participant_id)
                if excl_self is not None:
                    blockers_mi.append({
                        "participant_id": cp.participant_id,
                        "participant_name": cp.display_name,
                        "menu_item_id": target_it.item_id,
                        "dish_name": target_it.dish_name,
                        "reason_codes": excl_self.reason_codes,
                        "constraint_kinds": excl_self.constraint_kinds,
                        "unknown_fields": excl_self.unknown_fields,
                    })
                elif cp.hard_budget_paise is not None and personal_costs[target_idx] + delivery_shares[p_idx] > cp.hard_budget_paise:
                    blockers_mi.append({
                        "participant_id": cp.participant_id,
                        "participant_name": cp.display_name,
                        "menu_item_id": target_it.item_id,
                        "dish_name": target_it.dish_name,
                        "reason_codes": ["HARD_BUDGET_EXCEEDED"],
                        "constraint_kinds": ["budget_max_paise"],
                        "required_paise": personal_costs[target_idx] + delivery_shares[p_idx],
                        "budget_paise": cp.hard_budget_paise,
                    })
                    allowed_peers_mi = [
                        q_idx
                        for q_idx, q_cp in enumerate(participants)
                        if exclusion_map.get(target_it.item_id, {}).get(q_cp.participant_id) is None
                    ]
                    est_shared_mi = _min_shared_unit_cost_for_item(
                        target_it, max(2, len(allowed_peers_mi)), pricing_engine
                    )
                    for q_idx, q_cp in enumerate(participants):
                        if q_idx == p_idx:
                            continue
                        excl_q = exclusion_map.get(target_it.item_id, {}).get(q_cp.participant_id)
                        if excl_q is not None:
                            blockers_mi.append({
                                "participant_id": q_cp.participant_id,
                                "participant_name": q_cp.display_name,
                                "menu_item_id": target_it.item_id,
                                "dish_name": target_it.dish_name,
                                "reason_codes": excl_q.reason_codes,
                                "constraint_kinds": excl_q.constraint_kinds,
                                "blocks_group_sharing": True,
                            })
                        elif (
                            target_it.shareable
                            and q_cp.hard_budget_paise is not None
                            and est_shared_mi + delivery_shares[q_idx] > q_cp.hard_budget_paise
                        ):
                            blockers_mi.append({
                                "participant_id": q_cp.participant_id,
                                "participant_name": q_cp.display_name,
                                "menu_item_id": target_it.item_id,
                                "dish_name": target_it.dish_name,
                                "reason_codes": ["HARD_BUDGET_EXCEEDED"],
                                "constraint_kinds": ["budget_max_paise"],
                                "required_paise": est_shared_mi + delivery_shares[q_idx],
                                "budget_paise": q_cp.hard_budget_paise,
                                "blocks_group_sharing": True,
                            })
                else:
                    if is_drink_item(target_it) and not cp.opt_out_personal_meal:
                        blockers_mi.append({
                            "participant_id": cp.participant_id,
                            "participant_name": cp.display_name,
                            "menu_item_id": target_it.item_id,
                            "dish_name": target_it.dish_name,
                            "reason_codes": ["DRINK_NOT_A_MEAL_OR_CAPACITY_TRADEOFF"],
                            "constraint_kinds": ["meal_requirement"],
                        })
                    else:
                        blockers_mi.append({
                            "participant_id": cp.participant_id,
                            "participant_name": cp.display_name,
                            "menu_item_id": target_it.item_id,
                            "dish_name": target_it.dish_name,
                            "reason_codes": ["HIGHER_OBJECTIVE_OR_CAPACITY_TRADEOFF"],
                            "constraint_kinds": ["objective_priority"],
                        })

                # Offer safe, genuinely affordable alternatives in the SAME category
                target_cat = normalize_category_id(target_it.category)
                for i, alt_it in enumerate(items):
                    if alt_it.item_id == target_it.item_id:
                        continue
                    if normalize_category_id(alt_it.category) != target_cat:
                        continue
                    if exclusion_map.get(alt_it.item_id, {}).get(cp.participant_id) is not None:
                        continue
                    est_min = _find_best_same_category_alt_cost(
                        alt_it, personal_costs[i], p_idx, participants, exclusion_map, delivery_shares, pricing_engine
                    )
                    if est_min is not None:
                        alts_mi.append({
                            "menu_item_id": alt_it.item_id,
                            "dish_name": alt_it.dish_name,
                            "category": alt_it.category,
                            "estimated_min_paise": est_min,
                        })
            else:
                blockers_mi.append({
                    "participant_id": cp.participant_id,
                    "participant_name": cp.display_name,
                    "menu_item_id": target_id,
                    "reason_codes": ["ITEM_NOT_IN_MENU"],
                    "constraint_kinds": ["menu_availability"],
                })

            blocked_out.append(
                BlockedMustInclude(
                    participant_id=cp.participant_id,
                    preference_kind="must_include_item",
                    target_id=target_id,
                    weight=mi.weight,
                    blocked_by=blockers_mi,
                    alternatives_in_same_category=alts_mi,
                )
            )

    return blocked_out


# ---------------------------------------------------------------------------
# Step 7: Minimal Unsatisfiable Core (MUC) & Up to 3 Ranked Relaxations
# ---------------------------------------------------------------------------
def _check_trial_feasibility(
    items: Sequence[DietaryItem],
    trial_participants: Sequence[ParticipantInput],
    pricing_engine: PricingEngine,
    config: SolverConfig,
) -> bool:
    compiled = [_compile_participant(p) for p in trial_participants]
    n = len(compiled)
    if n == 0:
        return False
    m = len(items)
    allowed = [[_evaluate_hard_eligibility(it, cp)[0] for cp in compiled] for it in items]
    personal_costs = [
        pricing_engine.estimate_line_participant_upper_bound(
            unit_price_paise=it.price_paise,
            tax_rate_bps=it.tax_rate_bps,
            packaging_paise=it.packaging_paise,
            quantity=1,
            num_sharers=1,
        )
        for it in items
    ]
    d_shares_map = pricing_engine.estimate_delivery_shares(
        [cp.participant_id for cp in compiled],
        config.delivery_fee_paise,
    )
    delivery_shares = [d_shares_map.get(cp.participant_id, 0) for cp in compiled]
    scores = [[0.5 for _ in compiled] for _ in items]

    if config.use_ortools and _HAS_ORTOOLS:
        res = _solve_ortools(
            items,
            compiled,
            allowed,
            scores,
            personal_costs,
            delivery_shares,
            pricing_engine,
            SolverConfig(
                time_limit_ms=120,
                delivery_fee_paise=config.delivery_fee_paise,
                max_items_per_person=config.max_items_per_person,
                max_shared_lines=config.max_shared_lines,
                candidate_cap_for_large_menus=max(12, m),
                use_ortools=True,
            ),
            hard_only=True,
            use_lower_bound=False,
        )
        if res is not None:
            return True
        res_lb = _solve_ortools(
            items,
            compiled,
            allowed,
            scores,
            personal_costs,
            delivery_shares,
            pricing_engine,
            SolverConfig(
                time_limit_ms=120,
                delivery_fee_paise=config.delivery_fee_paise,
                max_items_per_person=config.max_items_per_person,
                max_shared_lines=config.max_shared_lines,
                candidate_cap_for_large_menus=max(12, m),
                use_ortools=True,
            ),
            hard_only=True,
            use_lower_bound=True,
        )
        return res_lb is not None

    fb = _solve_python_fallback(
        items,
        compiled,
        allowed,
        scores,
        personal_costs,
        delivery_shares,
        pricing_engine,
        config,
    )
    return fb is not None


def _strip_soft_and_build_hard_table(
    participants: Sequence[ParticipantInput],
) -> tuple[list[ParticipantInput], list[tuple[int, int, _CompiledHardConstraint]]]:
    """
    Removes all SOFT preferences first (Step 7) and extracts indexed hard constraints
    alongside synthetic IDs for deletion-based Minimal Unsatisfiable Core discovery.
    """
    hard_only_participants: list[ParticipantInput] = []
    indexed_constraints: list[tuple[int, int, _CompiledHardConstraint]] = []

    for p_idx, p in enumerate(participants):
        hard_prefs: list[ParticipantPreference] = []
        for pref in p.preferences:
            if not pref.confirmed_by_user:
                continue
            if pref.strength == "HARD" or pref.kind in ("diet", "allergen"):
                pref_idx = len(hard_prefs)
                hard_prefs.append(pref)
                cid = f"{p.participant_id}:{pref.kind}:{pref_idx}"
                indexed_constraints.append(
                    (
                        p_idx,
                        pref_idx,
                        _CompiledHardConstraint(
                            cid=cid,
                            participant_id=p.participant_id,
                            participant_name=p.display_name,
                            kind=pref.kind,
                            value=dict(pref.value),
                            is_private=pref.is_private,
                        ),
                    )
                )
        hard_only_participants.append(
            ParticipantInput(
                participant_id=p.participant_id,
                display_name=p.display_name,
                opt_out_personal_meal=p.opt_out_personal_meal,
                preferences=hard_prefs,
            )
        )
    return hard_only_participants, indexed_constraints


def _compute_no_solution_diagnostics(
    items: Sequence[DietaryItem],
    participants: Sequence[ParticipantInput],
    compiled: Sequence[_CompiledParticipant],
    pricing_engine: PricingEngine,
    config: SolverConfig,
) -> tuple[list[CoreConstraint], list[RelaxationOption]]:
    """
    Step 7:
    1. Strips all soft preferences first.
    2. Finds a Minimal Unsatisfiable Core (1-minimal subset via deterministic deletion).
    3. Generates up to 3 ranked, feasibility-verified relaxation options.
    """
    if not participants:
        return (
            [
                CoreConstraint(
                    constraint_id="table:no_participants",
                    kind="participants",
                    value={"count": 0},
                    reason_code="NO_PARTICIPANTS",
                )
            ],
            [],
        )

    available_meals = [it for it in items if it.is_available and not is_drink_item(it)]
    if not available_meals:
        return (
            [
                CoreConstraint(
                    constraint_id="menu:no_available_meals",
                    kind="menu_availability",
                    value={"available_meal_count": 0},
                    reason_code="NO_AVAILABLE_NON_DRINK_MEALS",
                )
            ],
            [
                RelaxationOption(
                    rank=1,
                    action="add_or_enable_menu_meals",
                    description="Enable at least one non-drink meal item on the menu.",
                    verified_feasible=False,
                )
            ],
        )

    hard_only_ps, indexed_hc = _strip_soft_and_build_hard_table(participants)

    def _subset_from_active(active_cids: set[str]) -> list[ParticipantInput]:
        by_p: dict[int, set[int]] = {}
        for p_idx, pref_idx, hc in indexed_hc:
            if hc.cid in active_cids:
                by_p.setdefault(p_idx, set()).add(pref_idx)
        out: list[ParticipantInput] = []
        for p_idx, hp in enumerate(hard_only_ps):
            keep_idxs = by_p.get(p_idx, set())
            out.append(
                ParticipantInput(
                    participant_id=hp.participant_id,
                    display_name=hp.display_name,
                    opt_out_personal_meal=hp.opt_out_personal_meal,
                    preferences=[pr for j, pr in enumerate(hp.preferences) if j in keep_idxs],
                )
            )
        return out

    if not _check_trial_feasibility(items, _subset_from_active(set()), pricing_engine, config):
        return (
            [
                CoreConstraint(
                    constraint_id="menu:structural_infeasibility",
                    kind="menu_availability",
                    value={"available_meal_count": len(available_meals)},
                    reason_code="STRUCTURAL_MEAL_INFEASIBILITY",
                )
            ],
            [
                RelaxationOption(
                    rank=1,
                    action="opt_out_personal_meal",
                    participant_id=compiled[0].participant_id,
                    participant_name=compiled[0].display_name,
                    description=f"Opt {compiled[0].display_name} out of requiring a personal meal.",
                    verified_feasible=False,
                )
            ],
        )

    # Deletion-based 1-minimal unsatisfiable core
    active_cids = {hc.cid for _, _, hc in indexed_hc}
    ordered_hcs = sorted(
        indexed_hc,
        key=lambda t: (
            0 if t[2].kind not in ("diet", "allergen") else 1,
            t[2].participant_id,
            t[2].kind,
            t[2].cid,
        ),
    )
    for _, _, hc in ordered_hcs:
        trial_cids = active_cids - {hc.cid}
        if not _check_trial_feasibility(items, _subset_from_active(trial_cids), pricing_engine, config):
            active_cids = trial_cids

    core: list[CoreConstraint] = []
    for _, _, hc in indexed_hc:
        if hc.cid in active_cids:
            core.append(
                CoreConstraint(
                    constraint_id=hc.cid,
                    participant_id=hc.participant_id,
                    participant_name=hc.participant_name,
                    kind=hc.kind,
                    value=hc.value,
                    reason_code=f"CONFLICT_{hc.kind.upper()}",
                )
            )
    core.sort(key=lambda c: (c.participant_id or "", c.kind, c.constraint_id))

    # Build up to 3 ranked feasible relaxation options
    candidates_relax: list[tuple[int, int, RelaxationOption]] = []
    d_shares_map = pricing_engine.estimate_delivery_shares(
        [cp.participant_id for cp in compiled],
        config.delivery_fee_paise,
    )
    personal_costs = [
        pricing_engine.estimate_line_participant_upper_bound(
            unit_price_paise=it.price_paise,
            tax_rate_bps=it.tax_rate_bps,
            packaging_paise=it.packaging_paise,
            quantity=1,
            num_sharers=1,
        )
        for it in items
    ]

    def _with_participant_budget(pid: str, new_budget_paise: int) -> list[ParticipantInput]:
        trial_ps: list[ParticipantInput] = []
        for hp in hard_only_ps:
            if hp.participant_id == pid:
                new_prefs = [
                    (
                        ParticipantPreference(
                            kind="budget_max_paise",
                            strength="HARD",
                            weight=pr.weight,
                            value={"max_paise": new_budget_paise},
                            confirmed_by_user=True,
                            is_private=pr.is_private,
                        )
                        if pr.kind == "budget_max_paise"
                        else pr
                    )
                    for pr in hp.preferences
                ]
                trial_ps.append(
                    ParticipantInput(
                        participant_id=hp.participant_id,
                        display_name=hp.display_name,
                        opt_out_personal_meal=hp.opt_out_personal_meal,
                        preferences=new_prefs,
                    )
                )
            else:
                trial_ps.append(hp)
        return trial_ps

    # Option Type A: Exact minimum budget increase for participants whose budget_max_paise is in core
    elig_matrix = [[_evaluate_hard_eligibility(it, q_cp)[0] for q_cp in compiled] for it in items]
    allowed_count_by_item = [sum(1 for ok in row if ok) for row in elig_matrix]
    core_budget_pids = {c.participant_id for c in core if c.kind == "budget_max_paise" and c.participant_id}
    any_single_budget_relaxed = False
    for cp_idx, cp in enumerate(compiled):
        if cp.participant_id not in core_budget_pids:
            continue
        cur_budget = cp.hard_budget_paise or 0
        d_share = d_shares_map.get(cp.participant_id, 0)
        high_bound = sum(personal_costs) + d_share
        if high_bound <= cur_budget or not _check_trial_feasibility(
            items, _with_participant_budget(cp.participant_id, high_bound), pricing_engine, config
        ):
            continue

        # Fast check on natural breakpoint before binary search
        lo = cur_budget + 1
        hi = high_bound
        must_item_cost = sum(
            personal_costs[i]
            for i, it in enumerate(items)
            if it.item_id in cp.hard_must_items
        )
        allowed_non_drink_costs = [
            (
                pricing_engine.estimate_line_participant_upper_bound(
                    unit_price_paise=it.price_paise,
                    tax_rate_bps=it.tax_rate_bps,
                    packaging_paise=it.packaging_paise,
                    quantity=1,
                    num_sharers=max(2, allowed_count_by_item[i]),
                )
                if it.shareable and allowed_count_by_item[i] >= 2
                else personal_costs[i]
            )
            for i, it in enumerate(items)
            if not is_drink_item(it) and elig_matrix[i][cp_idx]
        ]
        probe_hint = max(lo, must_item_cost + d_share, (min(allowed_non_drink_costs) + d_share) if allowed_non_drink_costs else lo)
        if probe_hint < hi and _check_trial_feasibility(
            items, _with_participant_budget(cp.participant_id, probe_hint), pricing_engine, config
        ):
            hi = probe_hint

        while lo < hi:
            mid = (lo + hi) // 2
            if _check_trial_feasibility(
                items, _with_participant_budget(cp.participant_id, mid), pricing_engine, config
            ):
                hi = mid
            else:
                lo = mid + 1

        delta = hi - cur_budget
        if delta > 0:
            any_single_budget_relaxed = True
            candidates_relax.append(
                (
                    1,
                    delta,
                    RelaxationOption(
                        rank=1,
                        action="raise_budget",
                        participant_id=cp.participant_id,
                        participant_name=cp.display_name,
                        target="budget_max_paise",
                        delta_paise=delta,
                        description=f"Raise {cp.display_name}'s budget by {_format_rupees(delta)} (to {_format_rupees(cur_budget + delta)}).",
                        verified_feasible=True,
                    ),
                )
            )

    all_budget_pids = {cp.participant_id for cp in compiled if cp.hard_budget_paise is not None}
    if not any_single_budget_relaxed and (len(core_budget_pids) >= 2 or len(all_budget_pids) >= 2):
        # Try lifting core budgets simultaneously first; if still blocked by another diner's tight budget, lift all capped budgets.
        max_sum = sum(personal_costs) + max(d_shares_map.values(), default=0)
        budget_pid_groups: list[set[str]] = []
        if len(core_budget_pids) >= 2:
            budget_pid_groups.append(core_budget_pids)
        if len(all_budget_pids) >= 2 and all_budget_pids != core_budget_pids:
            budget_pid_groups.append(all_budget_pids)

        for target_pids in budget_pid_groups:
            lo_d = 1
            hi_d = max_sum

            def _all_raised(delta_val: int, _pids: set[str] = target_pids) -> list[ParticipantInput]:
                out: list[ParticipantInput] = []
                for hp in hard_only_ps:
                    if hp.participant_id in _pids:
                        new_prefs = [
                            (
                                ParticipantPreference(
                                    kind="budget_max_paise",
                                    strength="HARD",
                                    weight=pr.weight,
                                    value={"max_paise": int(pr.value["max_paise"]) + delta_val},
                                    confirmed_by_user=True,
                                    is_private=pr.is_private,
                                )
                                if pr.kind == "budget_max_paise"
                                else pr
                            )
                            for pr in hp.preferences
                        ]
                        out.append(
                            ParticipantInput(
                                participant_id=hp.participant_id,
                                display_name=hp.display_name,
                                opt_out_personal_meal=hp.opt_out_personal_meal,
                                preferences=new_prefs,
                            )
                        )
                    else:
                        out.append(hp)
                return out

            if _check_trial_feasibility(items, _all_raised(hi_d), pricing_engine, config):
                while lo_d < hi_d:
                    mid_d = (lo_d + hi_d) // 2
                    if _check_trial_feasibility(items, _all_raised(mid_d), pricing_engine, config):
                        hi_d = mid_d
                    else:
                        lo_d = mid_d + 1
                candidates_relax.append(
                    (
                        1,
                        hi_d,
                        RelaxationOption(
                            rank=1,
                            action="raise_group_budgets",
                            participant_id=None,
                            participant_name=None,
                            target="budget_max_paise",
                            delta_paise=hi_d,
                            description=f"Raise budgets for constrained diners by {_format_rupees(hi_d)} each to allow shared dishes.",
                            verified_feasible=True,
                        ),
                    )
                )
                break

    # Option Type B: Drop/relax HARD spice constraint in core (deduplicated per participant)
    seen_spice_pids: set[str] = set()
    for c in core:
        if c.kind in ("spice_min", "spice_max", "spice_range") and c.participant_id and c.participant_id not in seen_spice_pids:
            seen_spice_pids.add(c.participant_id)
            trial_ps = []
            for hp in hard_only_ps:
                if hp.participant_id == c.participant_id:
                    new_prefs = [
                        pr for pr in hp.preferences if pr.kind not in ("spice_min", "spice_max", "spice_range")
                    ]
                    trial_ps.append(
                        ParticipantInput(
                            participant_id=hp.participant_id,
                            display_name=hp.display_name,
                            opt_out_personal_meal=hp.opt_out_personal_meal,
                            preferences=new_prefs,
                        )
                    )
                else:
                    trial_ps.append(hp)
            if _check_trial_feasibility(items, trial_ps, pricing_engine, config):
                candidates_relax.append(
                    (
                        2,
                        0,
                        RelaxationOption(
                            rank=1,
                            action="drop_hard_spice",
                            participant_id=c.participant_id,
                            participant_name=c.participant_name,
                            target="spice",
                            description=f"Drop hard spice restriction for {c.participant_name} (treat spice as soft preference).",
                            verified_feasible=True,
                        ),
                    )
                )

    # Option Type C: Relax specific HARD must_include / dislike -> SOFT (or let item be personal)
    for c in core:
        if c.kind in ("must_include_item", "must_include_category", "dislike_item") and c.participant_id:
            trial_ps = []
            for hp in hard_only_ps:
                if hp.participant_id == c.participant_id:
                    removed_one = False
                    filtered_prefs: list[ParticipantPreference] = []
                    for pr in hp.preferences:
                        if not removed_one and pr.kind == c.kind and pr.value == c.value:
                            removed_one = True
                            continue
                        filtered_prefs.append(pr)
                    trial_ps.append(
                        ParticipantInput(
                            participant_id=hp.participant_id,
                            display_name=hp.display_name,
                            opt_out_personal_meal=hp.opt_out_personal_meal,
                            preferences=filtered_prefs,
                        )
                    )
                else:
                    trial_ps.append(hp)
            if _check_trial_feasibility(items, trial_ps, pricing_engine, config):
                action_name = (
                    "make_must_include_soft_or_personal"
                    if c.kind in ("must_include_item", "must_include_category")
                    else "make_dislike_soft"
                )
                target_lbl = str(c.value.get("item_id") or c.value.get("category_id") or c.value.get("label") or c.kind)
                desc = (
                    f"Allow {c.participant_name}'s {target_lbl} to be optional or a personal item."
                    if c.kind.startswith("must_")
                    else f"Treat {c.participant_name}'s dislike ({target_lbl}) as a soft preference."
                )
                candidates_relax.append(
                    (
                        2,
                        10,
                        RelaxationOption(
                            rank=1,
                            action=action_name,
                            participant_id=c.participant_id,
                            participant_name=c.participant_name,
                            target=target_lbl,
                            description=desc,
                            verified_feasible=True,
                        ),
                    )
                )

    # Option Type D: Verify unverified / UNKNOWN dishes if verification gate blocked a core participant
    for cp_idx, cp in enumerate(compiled):
        has_allowed = any(
            not is_drink_item(it) and elig_matrix[i][cp_idx]
            for i, it in enumerate(items)
        )
        if not has_allowed and cp.raw_hard_constraints:
            unverified_meals = [it for it in available_meals if not it.is_verified or len(it.get_unknown_fields()) > 0]
            if unverified_meals:
                top_item = min(unverified_meals, key=lambda it: (it.price_paise, it.item_id))
                candidates_relax.append(
                    (
                        3,
                        top_item.price_paise,
                        RelaxationOption(
                            rank=1,
                            action="verify_menu_item_dietary_info",
                            participant_id=cp.participant_id,
                            participant_name=cp.display_name,
                            target=top_item.item_id,
                            description=(
                                f"Staff-verify dietary & allergen fields for '{top_item.dish_name}' "
                                f"({top_item.item_id}) so {cp.display_name} has a safe option."
                            ),
                            verified_feasible=False,
                        ),
                    )
                )

    # Option Type E: Opt out of personal meal for a participant with a blocking budget/diet
    for cp in compiled:
        if cp.opt_out_personal_meal:
            continue
        if cp.hard_budget_paise is not None and d_shares_map.get(cp.participant_id, 0) > cp.hard_budget_paise:
            continue
        trial_ps = [
            (
                ParticipantInput(
                    participant_id=hp.participant_id,
                    display_name=hp.display_name,
                    opt_out_personal_meal=True,
                    preferences=hp.preferences,
                )
                if hp.participant_id == cp.participant_id
                else hp
            )
            for hp in hard_only_ps
        ]
        if _check_trial_feasibility(items, trial_ps, pricing_engine, config):
            candidates_relax.append(
                (
                    4,
                    0,
                    RelaxationOption(
                        rank=1,
                        action="opt_out_personal_meal",
                        participant_id=cp.participant_id,
                        participant_name=cp.display_name,
                        target="opt_out_personal_meal",
                        description=f"Mark {cp.display_name} as opting out of a required meal.",
                        verified_feasible=True,
                    ),
                )
            )

    # Deduplicate by (action, participant_id, target) and rank up to 3 options deterministically
    candidates_relax.sort(key=lambda t: (t[0], t[1], t[2].action, t[2].participant_id or "", t[2].target or ""))
    seen_keys: set[tuple[str, str | None, str | None]] = set()
    ranked: list[RelaxationOption] = []
    for _, _, opt in candidates_relax:
        key = (opt.action, opt.participant_id, opt.target)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        ranked.append(opt.model_copy(update={"rank": len(ranked) + 1}))
        if len(ranked) >= 3:
            break

    return core, ranked


# ---------------------------------------------------------------------------
# Public Entrypoint (Pure Function)
# ---------------------------------------------------------------------------
def _build_line_served_reasons(
    item: DietaryItem,
    consumer_pids: Sequence[str],
    compiled_by_pid: Mapping[str, _CompiledParticipant],
) -> list[LineServedReason]:
    reasons: list[LineServedReason] = []
    for pid in sorted(consumer_pids):
        cp = compiled_by_pid[pid]
        if not is_drink_item(item):
            reasons.append(
                LineServedReason(
                    participant_id=pid,
                    preference_kind="meal_requirement",
                    preference_value={"category": normalize_category_id(item.category)},
                    weight=1,
                    match_score=1.0,
                    is_private=False,
                )
            )
        for d in sorted(cp.hard_diets):
            reasons.append(
                LineServedReason(
                    participant_id=pid,
                    preference_kind="diet",
                    preference_value={"diet": d},
                    weight=5,
                    match_score=1.0,
                    is_private=any(hc.kind == "diet" and hc.value.get("diet") == d and hc.is_private for hc in cp.raw_hard_constraints),
                )
            )
        for a in sorted(cp.hard_allergens):
            reasons.append(
                LineServedReason(
                    participant_id=pid,
                    preference_kind="allergen",
                    preference_value={"allergen": a},
                    weight=5,
                    match_score=1.0,
                    is_private=any(hc.kind == "allergen" and hc.value.get("allergen") == a and hc.is_private for hc in cp.raw_hard_constraints),
                )
            )
        for mc in cp.soft_must_categories:
            cat_id = str(mc.value.get("category_id") or "")
            if _item_matches_category(item, cat_id):
                reasons.append(
                    LineServedReason(
                        participant_id=pid,
                        preference_kind="must_include_category",
                        preference_value=mc.value,
                        weight=mc.weight,
                        match_score=1.0,
                        is_private=mc.is_private,
                    )
                )
        for mi in cp.soft_must_items:
            if item.item_id == str(mi.value.get("item_id") or ""):
                reasons.append(
                    LineServedReason(
                        participant_id=pid,
                        preference_kind="must_include_item",
                        preference_value=mi.value,
                        weight=mi.weight,
                        match_score=1.0,
                        is_private=mi.is_private,
                    )
                )
        if cp.soft_spice is not None and item.spice_level is not None:
            s_fit = _spice_fit(item.spice_level, cp.soft_spice.min_level, cp.soft_spice.max_level)
            if s_fit > 0.5:
                reasons.append(
                    LineServedReason(
                        participant_id=pid,
                        preference_kind="spice_range",
                        preference_value={"min": cp.soft_spice.min_level, "max": cp.soft_spice.max_level},
                        weight=cp.soft_spice.weight,
                        match_score=s_fit,
                        is_private=cp.soft_spice.is_private,
                    )
                )
        for cu in cp.soft_cuisines:
            if _item_matches_cuisine(item, str(cu.value.get("cuisine") or "")) == "match":
                reasons.append(
                    LineServedReason(
                        participant_id=pid,
                        preference_kind="cuisine_like",
                        preference_value=cu.value,
                        weight=cu.weight,
                        match_score=1.0,
                        is_private=cu.is_private,
                    )
                )
    return reasons


def recommend_proposal(
    menu: Sequence[DietaryItem | MenuItem | Mapping[str, Any]],
    participants: Sequence[ParticipantInput | Diner | Mapping[str, Any]],
    *,
    pricing_engine: PricingEngine | None = None,
    config: SolverConfig | None = None,
    delivery_fee_paise: int | None = None,
) -> ProposalResult:
    """
    Pure, deterministic Stage 6 Table Split recommendation engine.
    NO LLM calls, NO database/network I/O.
    """
    cfg = config or SolverConfig()
    if delivery_fee_paise is not None:
        validated_fee = validate_strict_paise(delivery_fee_paise)
        cfg = SolverConfig.model_validate({**cfg.model_dump(), "delivery_fee_paise": validated_fee})
    engine: PricingEngine = pricing_engine or StubPricingEngine()

    items = coerce_menu_items(menu)
    norm_participants = coerce_participants(participants)
    c_hash = _compute_constraints_hash(items, norm_participants, cfg)

    if not norm_participants:
        return ProposalResult(
            status="NO_SOLUTION",
            engine_version=ENGINE_VERSION,
            solver_backend="none",
            constraints_hash=c_hash,
            safety_summary=SafetySummary(
                all_hard_constraints_met=False,
                participants_checked=0,
                violations=[{"reason": "NO_PARTICIPANTS"}],
            ),
            minimal_conflicting_core=[
                CoreConstraint(
                    constraint_id="table:no_participants",
                    kind="participants",
                    value={"count": 0},
                    reason_code="NO_PARTICIPANTS",
                )
            ],
            relaxation_options=[],
        )

    compiled = [_compile_participant(p) for p in norm_participants]
    compiled_by_pid = {cp.participant_id: cp for cp in compiled}
    n = len(compiled)

    # Step 1: Hard dietary/allergen/spice/availability filtering via Stage 3 dietary module
    allowed: list[list[bool]] = []
    exclusion_map: dict[str, dict[str, LineExclusionReason]] = {}
    allowed_items_by_pid: dict[str, list[str]] = {cp.participant_id: [] for cp in compiled}

    for it in items:
        row_allow: list[bool] = []
        excl_for_item: dict[str, LineExclusionReason] = {}
        for cp in compiled:
            ok, excl = _evaluate_hard_eligibility(it, cp)
            row_allow.append(ok)
            if ok:
                allowed_items_by_pid[cp.participant_id].append(it.item_id)
            elif excl is not None:
                excl_for_item[cp.participant_id] = excl
        allowed.append(row_allow)
        exclusion_map[it.item_id] = excl_for_item

    # Step 2: Candidate item soft satisfaction scores (0-1, missing data -> 0.5)
    item_scores: list[list[float]] = []
    item_scores_dict: dict[str, dict[str, float]] = {}
    for it in items:
        row_sc = [score_item_for_participant(it, cp) for cp in compiled]
        item_scores.append(row_sc)
        item_scores_dict[it.item_id] = {
            compiled[p].participant_id: row_sc[p] for p in range(n)
        }

    personal_costs = [
        engine.estimate_line_participant_upper_bound(
            unit_price_paise=it.price_paise,
            tax_rate_bps=it.tax_rate_bps,
            packaging_paise=it.packaging_paise,
            quantity=1,
            num_sharers=1,
        )
        for it in items
    ]
    d_shares_map = engine.estimate_delivery_shares(
        [cp.participant_id for cp in compiled],
        cfg.delivery_fee_paise,
    )
    delivery_shares = [d_shares_map.get(cp.participant_id, 0) for cp in compiled]

    # Step 3 & 4: Solve with OR-Tools CP-SAT (or pure-Python fallback)
    backend = "ortools_cpsat" if (cfg.use_ortools and _HAS_ORTOOLS) else "python_fallback"
    raw_assign: _RawAssignment | None = None
    if cfg.use_ortools and _HAS_ORTOOLS:
        raw_assign = _solve_ortools(
            items,
            compiled,
            allowed,
            item_scores,
            personal_costs,
            delivery_shares,
            engine,
            cfg,
            hard_only=False,
            use_lower_bound=False,
        )
        if raw_assign is None:
            # Retry with lower-bound rounding check in case exact 1-3 paise remainder fits tight budget
            cand_lb = _solve_ortools(
                items,
                compiled,
                allowed,
                item_scores,
                personal_costs,
                delivery_shares,
                engine,
                cfg,
                hard_only=False,
                use_lower_bound=True,
            )
            if cand_lb is not None and _score_assignment_tuple(cand_lb, items, compiled, cfg.delivery_fee_paise, engine, config=cfg) is not None:
                raw_assign = cand_lb
    else:
        raw_assign = _solve_python_fallback(
            items,
            compiled,
            allowed,
            item_scores,
            personal_costs,
            delivery_shares,
            engine,
            cfg,
        )

    if raw_assign is None:
        core, relaxations = _compute_no_solution_diagnostics(
            items, norm_participants, compiled, engine, cfg
        )
        return ProposalResult(
            status="NO_SOLUTION",
            engine_version=ENGINE_VERSION,
            solver_backend=backend,
            constraints_hash=c_hash,
            safety_summary=SafetySummary(
                all_hard_constraints_met=False,
                participants_checked=n,
                violations=[c.model_dump() for c in core],
            ),
            minimal_conflicting_core=core,
            relaxation_options=relaxations,
            allowed_items_by_participant=allowed_items_by_pid,
            item_scores=item_scores_dict,
        )

    # Assemble deterministic ProposalLine objects + PricingLineInputs
    proposal_lines: list[ProposalLine] = []
    pricing_lines: list[PricingLineInput] = []
    consumed_by_pid: dict[str, list[DietaryItem]] = {cp.participant_id: [] for cp in compiled}

    # Shared lines (sorted by item_id)
    for orig_i in sorted(raw_assign.shared.keys(), key=lambda i: items[i].item_id):
        it = items[orig_i]
        sharer_pids = sorted(compiled[p].participant_id for p in raw_assign.shared[orig_i])
        exclusions = [
            exclusion_map[it.item_id][cp.participant_id]
            for cp in compiled
            if cp.participant_id in exclusion_map[it.item_id]
        ]
        proposal_lines.append(
            ProposalLine(
                menu_item_id=it.item_id,
                dish_name=it.dish_name,
                category=it.category,
                kind="SHARED",
                quantity=1,
                consumer_participant_ids=sharer_pids,
                unit_price_paise=it.price_paise,
                serves=it.serves,
                reasons=_build_line_served_reasons(it, sharer_pids, compiled_by_pid),
                excluded_participants=exclusions,
            )
        )
        pricing_lines.append(
            PricingLineInput(
                menu_item_id=it.item_id,
                dish_name=it.dish_name,
                quantity=1,
                kind="SHARED",
                consumer_participant_ids=sharer_pids,
                unit_price_paise=it.price_paise,
                tax_rate_bps=it.tax_rate_bps,
                packaging_paise=it.packaging_paise,
            )
        )
        for pid in sharer_pids:
            consumed_by_pid[pid].append(it)

    # Personal lines (sorted by item_id, then participant_id)
    pers_entries = sorted(
        raw_assign.personal.items(),
        key=lambda kv: (items[kv[1]].item_id, compiled[kv[0]].participant_id),
    )
    for p_idx, orig_i in pers_entries:
        it = items[orig_i]
        pid = compiled[p_idx].participant_id
        exclusions = [
            exclusion_map[it.item_id][cp.participant_id]
            for cp in compiled
            if cp.participant_id in exclusion_map[it.item_id]
        ]
        proposal_lines.append(
            ProposalLine(
                menu_item_id=it.item_id,
                dish_name=it.dish_name,
                category=it.category,
                kind="PERSONAL",
                quantity=1,
                consumer_participant_ids=[pid],
                unit_price_paise=it.price_paise,
                serves=it.serves,
                reasons=_build_line_served_reasons(it, [pid], compiled_by_pid),
                excluded_participants=exclusions,
            )
        )
        pricing_lines.append(
            PricingLineInput(
                menu_item_id=it.item_id,
                dish_name=it.dish_name,
                quantity=1,
                kind="PERSONAL",
                consumer_participant_ids=[pid],
                unit_price_paise=it.price_paise,
                tax_rate_bps=it.tax_rate_bps,
                packaging_paise=it.packaging_paise,
            )
        )
        consumed_by_pid[pid].append(it)

    # Exact pricing calculation via Stage 7 PricingEngine interface
    pricing_result = engine.calculate_proposal_pricing(
        pricing_lines,
        [cp.participant_id for cp in compiled],
        delivery_fee_paise=cfg.delivery_fee_paise,
    )

    # Independent Belt-and-Suspenders Post-Solve Safety & Budget Audit (Step 10)
    violations: list[dict[str, Any]] = []
    item_by_id = {it.item_id: it for it in items}

    for line in proposal_lines:
        it = item_by_id[line.menu_item_id]
        if not it.is_available:
            violations.append({"item_id": it.item_id, "reason": "ITEM_UNAVAILABLE"})
        if line.kind == "SHARED" and (len(line.consumer_participant_ids) < 2 or not it.shareable):
            violations.append({"item_id": it.item_id, "reason": "INVALID_SHARED_LINE"})
        if line.kind == "PERSONAL" and len(line.consumer_participant_ids) != 1:
            violations.append({"item_id": it.item_id, "reason": "INVALID_PERSONAL_LINE"})
        for pid in line.consumer_participant_ids:
            cp = compiled_by_pid[pid]
            res = is_item_allowed(it, cp.hard_dietary_constraints())
            if not res.allowed:
                violations.append({
                    "participant_id": pid,
                    "item_id": it.item_id,
                    "reason": "DIETARY_OR_ALLERGEN_VIOLATION",
                    "unknown_fields": res.unknownFields,
                })
            ok_elig, excl = _evaluate_hard_eligibility(it, cp)
            if not ok_elig:
                violations.append({
                    "participant_id": pid,
                    "item_id": it.item_id,
                    "reason": "HARD_ELIGIBILITY_VIOLATION",
                    "codes": excl.reason_codes if excl else [],
                })

    for cp in compiled:
        pid = cp.participant_id
        c_items = consumed_by_pid[pid]
        if not cp.opt_out_personal_meal and not any(not is_drink_item(it) for it in c_items):
            violations.append({"participant_id": pid, "reason": "MISSING_REQUIRED_MEAL"})
        if cp.opt_out_personal_meal and any(
            line.kind == "PERSONAL" and pid in line.consumer_participant_ids for line in proposal_lines
        ):
            violations.append({"participant_id": pid, "reason": "OPT_OUT_GIVEN_PERSONAL_MEAL"})
        for iid in cp.hard_must_items:
            if not any(it.item_id == iid for it in c_items):
                violations.append({"participant_id": pid, "item_id": iid, "reason": "HARD_MUST_ITEM_UNMET"})
        for cat in cp.hard_must_categories:
            if not any(_item_matches_category(it, cat) for it in c_items):
                violations.append({"participant_id": pid, "category": cat, "reason": "HARD_MUST_CATEGORY_UNMET"})
        alloc = pricing_result.allocations[pid]
        if cp.hard_budget_paise is not None and alloc.total_paise > cp.hard_budget_paise:
            violations.append({
                "participant_id": pid,
                "total_paise": alloc.total_paise,
                "budget_paise": cp.hard_budget_paise,
                "reason": "HARD_BUDGET_EXCEEDED",
            })

    if violations:
        # Fail closed: proposal MUST NOT be produced if any hard constraint failed
        core, relaxations = _compute_no_solution_diagnostics(
            items, norm_participants, compiled, engine, cfg
        )
        return ProposalResult(
            status="NO_SOLUTION",
            engine_version=ENGINE_VERSION,
            solver_backend=backend,
            constraints_hash=c_hash,
            safety_summary=SafetySummary(
                all_hard_constraints_met=False,
                participants_checked=n,
                violations=violations,
            ),
            minimal_conflicting_core=core,
            relaxation_options=relaxations,
            allowed_items_by_participant=allowed_items_by_pid,
            item_scores=item_scores_dict,
        )

    # Step 10: Per-participant satisfaction report
    sat_report: dict[str, ParticipantSatisfactionReport] = {}
    costs_all: list[int] = []
    sats_all: list[float] = []
    for cp in compiled:
        pid = cp.participant_id
        tot_p = pricing_result.allocations[pid].total_paise
        costs_all.append(tot_p)
        rep = _evaluate_participant_satisfaction(cp, consumed_by_pid[pid], tot_p, hard_met=True)
        sat_report[pid] = rep
        sats_all.append(rep.satisfaction)

    # Step 6: Blocked must-include explanations & same-category alternatives
    blocked_must = _build_blocked_must_includes(
        items,
        compiled,
        consumed_by_pid,
        exclusion_map,
        personal_costs,
        delivery_shares,
        engine,
    )

    waste_val = 0
    for line in proposal_lines:
        if line.kind == "SHARED":
            waste_val += abs(line.serves - len(line.consumer_participant_ids))
        elif line.serves > 1:
            waste_val += line.serves - 1

    return ProposalResult(
        status="OK",
        engine_version=ENGINE_VERSION,
        solver_backend=backend,
        constraints_hash=c_hash,
        lines=proposal_lines,
        pricing=pricing_result,
        allocations=pricing_result.allocations,
        satisfaction_report=sat_report,
        safety_summary=SafetySummary(
            all_hard_constraints_met=True,
            verified_by_deterministic_engine=True,
            participants_checked=n,
            violations=[],
        ),
        blocked_must_includes=blocked_must,
        minimal_conflicting_core=[],
        relaxation_options=[],
        allowed_items_by_participant=allowed_items_by_pid,
        item_scores=item_scores_dict,
        objective_metrics={
            "min_satisfaction": min(sats_all) if sats_all else 1.0,
            "avg_satisfaction": round(sum(sats_all) / max(1, len(sats_all)), 4),
            "cost_spread_paise": (max(costs_all) - min(costs_all)) if costs_all else 0,
            "waste_score": waste_val,
        },
    )


# Spec-friendly alias
solve_table_split = recommend_proposal

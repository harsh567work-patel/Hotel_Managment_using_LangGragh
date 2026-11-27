"""
tests.test_table_split_stage6 — Comprehensive Stage 6 Deterministic Recommendation Engine Tests.

Covers:
1. Purity & Zero-LLM / Zero-I/O Audit on `table_split/solver.py` and `table_split/pricing.py`.
2. Stage 7 Pricing Interface & Stage 6 Integer-Paise Stub:
   - Strict integer paise (rejects float and bool)
   - GST bps + packaging + delivery fee zero-penny conservation across odd splits
   - Deterministic remainder distribution invariant to participant permutation
   - Custom PricingEngine injection compatibility
3. Step 1 & Step 2 Rules:
   - UNKNOWN-data & unverified items never appear when a related hard constraint is active
   - Missing soft data (spice_level=None, missing cuisine/tags) returns neutral 0.5, never a boost
   - Dislikes act as strong negatives
   - Must-include category weight 5 overrides lower weights
   - Drinks excluded as personal meals; opt_out_personal_meal skips personal meal requirement
4. Mockup Scenario (Seeded Menu):
   - Vegan (no dairy), Spice lover, ₹250 hard budget, Pizza lover
   - Vegan never listed as sharer of Paneer Pizza, Ghost Pepper Pizza, or Garlic Bread
   - Pizza lover gets pizza (shared among non-vegans or personal); all budgets respected
5. Objective Ordering & Deterministic Tie-Breaks:
   - Least misery (min satisfaction) > average satisfaction > cost spread > waste (serves) > item_id
   - Message order/length independence & confirmed_by_user=False gating
6. Blocked Must-Include Explanation & Same-Category Alternatives
7. NO_SOLUTION Minimal Unsatisfiable Core & Up to 3 Ranked Relaxations
8. Heavy Property Tests (fuzzed random preferences):
   - Zero hard violations, monotonicity, determinism, participant permutation invariance
9. Pure-Python Fallback Solver (`use_ortools=False`) parity
10. Performance SLA Test: 12 people x 100 items completes strictly under 500 ms.
"""

from __future__ import annotations

import ast
import random
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import table_split.pricing as pricing_mod
import table_split.solver as solver_mod
from table_split.db.dietary_repo import DietaryRepository
from table_split.db.migrations.runner import migrate_up
from table_split.db.seed import SAMPLE_MENU_EXTENSIONS, seed_menu_item_extensions
from table_split.dietary import DietaryItem
from table_split.enums import (
    Allergen,
    DietRule,
    PreferenceStrength,
    SpiceLevel,
    TriState,
)
from table_split.models import (
    Diner,
    DinerPreference,
    DishSafetyProfile,
    DishVerification,
    MenuItem,
)
from table_split.pricing import (
    PricingEngine,
    PricingLineInput,
    ProposalPricingResult,
    StubPricingEngine,
    split_integer_paise,
)
from table_split.services.session_service import SessionService
from table_split.solver import (
    ENGINE_VERSION,
    ParticipantInput,
    ParticipantPreference,
    SolverConfig,
    build_participant_inputs_from_session,
    recommend_proposal,
    score_item_for_participant,
)


def _make_verified_item(
    item_id: str,
    *,
    dish_name: str = "",
    category: str = "Main Course",
    description: str = "",
    price_paise: int = 20000,
    tax_rate_bps: int = 500,
    packaging_paise: int = 0,
    is_available: bool = True,
    spice_level: int | None = 1,
    serves: int = 1,
    shareable: bool = True,
    tags: list[str] | None = None,
    cuisine: str | None = None,
    vegan: TriState = TriState.YES,
    veg: TriState = TriState.YES,
    non_veg: TriState = TriState.NO,
    jain: TriState = TriState.YES,
    halal: TriState = TriState.YES,
    gluten_free: TriState = TriState.YES,
    dairy: TriState = TriState.NO,
    gluten: TriState = TriState.NO,
    peanuts: TriState = TriState.NO,
    tree_nuts: TriState = TriState.NO,
    soy: TriState = TriState.NO,
    sesame: TriState = TriState.NO,
    egg: TriState = TriState.NO,
    honey: TriState = TriState.NO,
    onion_garlic: TriState = TriState.NO,
    root_veg: TriState = TriState.NO,
    verified: bool = True,
) -> DietaryItem:
    return DietaryItem(
        item_id=item_id,
        dish_name=dish_name or f"Dish {item_id}",
        category=category,
        description=description,
        price_paise=price_paise,
        tax_rate_bps=tax_rate_bps,
        packaging_paise=packaging_paise,
        is_available=is_available,
        spice_level=spice_level,
        serves=serves,
        shareable=shareable,
        tags=tags or [],
        cuisine=cuisine,
        verified_by="Chef Audit" if verified else None,
        verified_at=1732500000 if verified else None,
        diet_veg=veg,
        diet_non_veg=non_veg,
        diet_egg=egg,
        diet_vegan=vegan,
        diet_jain=jain,
        diet_halal=halal,
        diet_gluten_free=gluten_free,
        contains_onion_garlic=onion_garlic,
        contains_root_veg=root_veg,
        contains_honey=honey,
        allergen_cereals_gluten=gluten,
        allergen_crustaceans=TriState.NO,
        allergen_milk=dairy,
        allergen_eggs=egg,
        allergen_fish=TriState.NO,
        allergen_peanuts=peanuts,
        allergen_tree_nuts=tree_nuts,
        allergen_soy=soy,
        allergen_sulphite=TriState.NO,
        allergen_sesame=sesame,
        allergen_mustard=TriState.NO,
        contains_dairy=dairy,
    )


def _setup_seeded_verified_menu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[DietaryItem]:
    """
    Seeds the real 12 SAMPLE_MENU_EXTENSIONS into SQLite via Stage 2's seed script,
    then completes Stage 3 staff verification for safe items while leaving D008 unverified
    with UNKNOWN allergens so both valid dining and UNKNOWN-data exclusion are realistic.
    """
    db_path = tmp_path / "seeded_stage6.db"
    migrate_up(db_path)
    monkeypatch.setenv("ALLOW_SEED", "1")
    seed_menu_item_extensions(str(db_path))

    repo = DietaryRepository(db_path=db_path)
    seeded_ids = {row["item_id"] for row in SAMPLE_MENU_EXTENSIONS}

    # Staff sign off 11 seeded items (leaving D008 deliberately unverified & UNKNOWN,
    # and keeping D022's soy/sulphite/sesame deliberately UNKNOWN)
    for item_id in sorted(seeded_ids):
        if item_id == "D008":
            continue
        updates: dict[str, Any] = {
            "contains_honey": "NO",
            "allergen_mustard": "NO",
        }
        if item_id != "D025":
            updates["allergen_crustaceans"] = "NO"
        repo.verify_item(item_id, updates=updates, verified_by="Chef Anita", verified_at=1732600000)

    return [it for it in repo.list_items() if it.item_id in seeded_ids]


# ===========================================================================
# 1. Purity Audit (Zero I/O, Zero LLM Calls)
# ===========================================================================
@pytest.mark.parametrize("mod", [solver_mod, pricing_mod])
def test_stage6_modules_have_zero_io_or_llm_imports(mod: Any) -> None:
    src_path = Path(mod.__file__)
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    forbidden = {
        "sqlite3",
        "os",
        "io",
        "pathlib",
        "urllib",
        "requests",
        "httpx",
        "sqlalchemy",
        "langchain",
        "langchain_groq",
        "langchain_core",
        "groq",
        "openai",
        "random",
        "time",
        "datetime",
    }
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint(forbidden), f"Forbidden import(s) in {src_path.name}: {imported & forbidden}"


# ===========================================================================
# 2. Pricing Interface & Stub Tests (Strict Paise + Zero-Penny Conservation)
# ===========================================================================
def test_pricing_strict_paise_and_zero_penny_conservation() -> None:
    engine: PricingEngine = StubPricingEngine()
    assert isinstance(engine, PricingEngine)

    # Reject float and bool strictly
    with pytest.raises((ValidationError, TypeError)):
        PricingLineInput(
            menu_item_id="D1",
            kind="PERSONAL",
            consumer_participant_ids=["p1"],
            unit_price_paise=100.0,  # type: ignore[arg-type]
        )
    with pytest.raises((ValidationError, TypeError)):
        PricingLineInput(
            menu_item_id="D1",
            kind="PERSONAL",
            consumer_participant_ids=["p1"],
            unit_price_paise=True,
        )
    with pytest.raises((ValidationError, TypeError)):
        ParticipantPreference(
            kind="budget_max_paise",
            strength="HARD",
            value={"max_paise": False},
        )
    with pytest.raises((ValidationError, TypeError)):
        SolverConfig(delivery_fee_paise=50.5)  # type: ignore[arg-type]

    # Odd split conservation across 3, 7, and 11 participants
    for count in (3, 7, 11):
        pids = [f"p_{i:02d}" for i in range(count)]
        lines = [
            PricingLineInput(
                menu_item_id="D037",
                dish_name="Paneer Pizza",
                quantity=1,
                kind="SHARED",
                consumer_participant_ids=pids,
                unit_price_paise=38001,  # odd paise
                tax_rate_bps=500,
                packaging_paise=2501,
            )
        ]
        res1 = engine.calculate_proposal_pricing(lines, pids, delivery_fee_paise=4999)
        res2 = engine.calculate_proposal_pricing(lines, list(reversed(pids)), delivery_fee_paise=4999)

        assert res1.model_dump() == res2.model_dump()
        assert sum(a.items_paise for a in res1.allocations.values()) == res1.subtotal_paise
        assert sum(a.tax_paise for a in res1.allocations.values()) == res1.tax_paise
        assert sum(a.packaging_share_paise for a in res1.allocations.values()) == res1.packaging_paise
        assert sum(a.delivery_share_paise for a in res1.allocations.values()) == res1.delivery_fee_paise
        assert sum(a.total_paise for a in res1.allocations.values()) == res1.grand_total_paise

    assert split_integer_paise(100, ["p_b", "p_a", "p_c"]) == {"p_a": 34, "p_b": 33, "p_c": 33}


def test_custom_stage7_pricing_engine_injection() -> None:
    """Verifies solver honors a custom PricingEngine implementation for budget checks."""

    class HighFeePricingEngine(StubPricingEngine):
        def __init__(self) -> None:
            super().__init__(pricing_version="0.7.0-custom-test")

        def estimate_delivery_shares(self, participant_ids: Any, delivery_fee_paise: int = 0) -> dict[str, int]:
            # Inject ₹100 flat surcharge per person on top of any delivery_fee_paise
            pids = list(participant_ids)
            base = StubPricingEngine().estimate_delivery_shares(pids, delivery_fee_paise)
            return {pid: base.get(pid, 0) + 10000 for pid in pids}

        def calculate_proposal_pricing(
            self,
            lines: Any,
            participant_ids: Any,
            *,
            delivery_fee_paise: int = 0,
            default_tax_rate_bps: int | None = None,
        ) -> ProposalPricingResult:
            pids = list(participant_ids)
            total_fee = delivery_fee_paise + 10000 * len(pids)
            return StubPricingEngine(pricing_version=self.pricing_version).calculate_proposal_pricing(
                lines,
                pids,
                delivery_fee_paise=total_fee,
                default_tax_rate_bps=default_tax_rate_bps,
            )

    menu = [
        _make_verified_item("D1", dish_name="Dal", price_paise=10000),  # ₹105 + ₹100 fee = ₹205
    ]
    ps = [
        ParticipantInput(
            participant_id="p1",
            preferences=[ParticipantPreference(kind="budget_max_paise", strength="HARD", value={"max_paise": 15000})],
        )
    ]
    # Fails budget because custom pricing engine adds ₹100 fee (₹205 > ₹150)
    res_fail = recommend_proposal(menu, ps, pricing_engine=HighFeePricingEngine())
    assert res_fail.status == "NO_SOLUTION"

    # Raising budget to ₹210 succeeds with custom pricing_version reflected
    ps_ok = [
        ParticipantInput(
            participant_id="p1",
            preferences=[ParticipantPreference(kind="budget_max_paise", strength="HARD", value={"max_paise": 21000})],
        )
    ]
    res_ok = recommend_proposal(menu, ps_ok, pricing_engine=HighFeePricingEngine())
    assert res_ok.status == "OK"
    assert res_ok.pricing is not None
    assert res_ok.pricing.pricing_version == "0.7.0-custom-test"
    assert res_ok.allocations["p1"].total_paise == 20500


# ===========================================================================
# 3. UNKNOWN Data, Missing Soft Data (0.5), Dislikes, Drinks & Opt-Out
# ===========================================================================
def test_unknown_data_never_appears_when_related_hard_constraint_active() -> None:
    unknown_peanut_dish = _make_verified_item(
        "D_UNK",
        dish_name="Mystery Satay Bowl",
        price_paise=10000,
        peanuts=TriState.UNKNOWN,
        verified=True,
    )
    safe_dish = _make_verified_item(
        "D_SAFE",
        dish_name="Clean Dal",
        price_paise=18000,
        peanuts=TriState.NO,
        verified=True,
    )
    unverified_dish = _make_verified_item(
        "D_UNVER",
        dish_name="Unverified Snack",
        price_paise=5000,
        verified=False,
    )

    p_allergic = ParticipantInput(
        participant_id="p_peanut",
        preferences=[ParticipantPreference(kind="allergen", strength="HARD", value={"allergen": "peanuts"})],
    )
    p_omni = ParticipantInput(participant_id="p_omni")

    res = recommend_proposal([unknown_peanut_dish, safe_dish, unverified_dish], [p_allergic, p_omni])
    assert res.status == "OK"
    assert "D_UNK" not in res.allowed_items_by_participant["p_peanut"]
    assert "D_UNVER" not in res.allowed_items_by_participant["p_peanut"]
    assert "D_UNK" in res.allowed_items_by_participant["p_omni"]

    for line in res.lines:
        if "p_peanut" in line.consumer_participant_ids:
            assert line.menu_item_id == "D_SAFE"
        if line.menu_item_id in ("D_UNK", "D_UNVER"):
            excl_pids = {e.participant_id: e for e in line.excluded_participants}
            assert "p_peanut" in excl_pids
            if line.menu_item_id == "D_UNK":
                assert "allergen_peanuts" in excl_pids["p_peanut"].unknown_fields


def test_missing_soft_data_returns_neutral_05_never_boost() -> None:
    missing_item = _make_verified_item(
        "D_MISS",
        dish_name="Plain Dish",
        category="",
        spice_level=None,
        tags=[],
        cuisine=None,
    )
    matched_item = _make_verified_item(
        "D_MATCH",
        dish_name="Spicy South Indian Curry",
        category="Main Course",
        spice_level=3,
        tags=["south indian", "spicy"],
        cuisine="South Indian",
    )
    disliked_item = _make_verified_item(
        "D_DISLIKE",
        dish_name="Butter Chicken",
        category="Main Course",
        spice_level=3,
        tags=["south indian"],
        cuisine="South Indian",
    )

    p = ParticipantInput(
        participant_id="p1",
        preferences=[
            ParticipantPreference(kind="spice_range", strength="SOFT", weight=3, value={"min": 3, "max": 4}),
            ParticipantPreference(kind="cuisine_like", strength="SOFT", weight=3, value={"cuisine": "South Indian"}),
            ParticipantPreference(kind="must_include_category", strength="SOFT", weight=5, value={"category_id": "main_course"}),
            ParticipantPreference(kind="dislike_item", strength="SOFT", weight=4, value={"item_id": "D_DISLIKE", "label": "Butter Chicken"}),
        ],
    )

    # Pure missing-data check (only spice + cuisine prefs so category isn't neutral-mixed)
    p_spice_cuisine = ParticipantInput(
        participant_id="p_sc",
        preferences=[
            ParticipantPreference(kind="spice_range", strength="SOFT", weight=3, value={"min": 3, "max": 4}),
            ParticipantPreference(kind="cuisine_like", strength="SOFT", weight=3, value={"cuisine": "South Indian"}),
        ],
    )
    assert score_item_for_participant(missing_item, p_spice_cuisine) == 0.5
    assert score_item_for_participant(matched_item, p_spice_cuisine) == 1.0
    assert score_item_for_participant(missing_item, p) == 0.5
    assert score_item_for_participant(matched_item, p) > 0.7
    assert score_item_for_participant(disliked_item, p) <= 0.15


def test_drinks_not_valid_personal_meal_and_opt_out_supported() -> None:
    drink_only = [
        _make_verified_item("D010", dish_name="Mango Lassi", category="Drinks", price_paise=9000, spice_level=0),
        _make_verified_item("D031", dish_name="Lime Soda", category="Drinks", price_paise=6000, spice_level=0),
    ]
    p_normal = ParticipantInput(participant_id="p1", opt_out_personal_meal=False)
    res_fail = recommend_proposal(drink_only, [p_normal])
    assert res_fail.status == "NO_SOLUTION"

    # With opt_out_personal_meal=True (via boolean or preference) and budget 0, succeeds cleanly
    p_opt1 = ParticipantInput(participant_id="p1", opt_out_personal_meal=True)
    p_opt2 = ParticipantInput(
        participant_id="p2",
        preferences=[
            ParticipantPreference(kind="opt_out_personal_meal", strength="SOFT", value={"opt_out": True}),
            ParticipantPreference(kind="budget_max_paise", strength="HARD", value={"max_paise": 0}),
        ],
    )
    res_opt = recommend_proposal(drink_only, [p_opt1, p_opt2])
    assert res_opt.status == "OK"
    assert res_opt.safety_summary.all_hard_constraints_met is True
    assert res_opt.allocations["p2"].total_paise == 0
    assert not any(line.kind == "PERSONAL" for line in res_opt.lines)

    # Desserts, Starters, Breads, Sides, Bowls ARE valid personal meals
    for cat in ("Desserts", "Starters", "Breads", "Sides", "Bowls", "Pizzas", "Main Course"):
        res_cat = recommend_proposal(
            [_make_verified_item("M1", dish_name=f"Item {cat}", category=cat, price_paise=10000)],
            [p_normal],
        )
        assert res_cat.status == "OK", f"Category {cat} should be a valid personal meal"


# ===========================================================================
# 4. Mockup Scenario (Seeded Menu)
# ===========================================================================
def test_mockup_scenario_seeded_menu_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Mockup scenario using the seeded menu:
    - P1: Vegan with no dairy
    - P2: Spice lover
    - P3: ₹250 hard budget
    - P4: Pizza lover (must_include_category=pizzas, weight 5)
    Expected:
    - Vegan is NEVER listed as a sharer of Paneer Pizza (D037), Ghost Pepper Pizza (D040), or Garlic Bread (D038)
    - Pizza lover gets a pizza (shared among non-vegans or personal)
    - All budgets respected (including GST + packaging + delivery)
    - Unverified D008 is excluded for the vegan
    """
    seeded_menu = _setup_seeded_verified_menu(tmp_path, monkeypatch)
    assert len(seeded_menu) == 12

    p1_vegan = ParticipantInput(
        participant_id="p1_vegan",
        display_name="Ananya (Vegan)",
        preferences=[
            ParticipantPreference(kind="diet", strength="HARD", weight=5, value={"diet": "vegan"}),
            ParticipantPreference(kind="allergen", strength="HARD", weight=5, value={"allergen": "dairy"}),
        ],
    )
    p2_spice = ParticipantInput(
        participant_id="p2_spice",
        display_name="Vikram (Spice Lover)",
        preferences=[
            ParticipantPreference(kind="spice_range", strength="SOFT", weight=4, value={"min": 3, "max": 4}),
        ],
    )
    p3_budget = ParticipantInput(
        participant_id="p3_budget",
        display_name="Amit (₹250 Budget)",
        preferences=[
            ParticipantPreference(kind="budget_max_paise", strength="HARD", weight=5, value={"max_paise": 25000}),
        ],
    )
    p4_pizza = ParticipantInput(
        participant_id="p4_pizza",
        display_name="Rohan (Pizza Lover)",
        preferences=[
            ParticipantPreference(kind="must_include_category", strength="SOFT", weight=5, value={"category_id": "pizzas"}),
        ],
    )

    result = recommend_proposal(seeded_menu, [p1_vegan, p2_spice, p3_budget, p4_pizza])
    assert result.status == "OK"
    assert result.engine_version == ENGINE_VERSION
    assert result.safety_summary.all_hard_constraints_met is True
    assert result.safety_summary.violations == []

    # Vegan never consumes D037 (Paneer Pizza), D040 (Ghost Pepper Pizza), D038 (Garlic Bread), or D008 (Unverified)
    vegan_items = [
        line.menu_item_id
        for line in result.lines
        if "p1_vegan" in line.consumer_participant_ids
    ]
    assert len(vegan_items) >= 1
    assert set(vegan_items).isdisjoint({"D037", "D038", "D040", "D008", "D001", "D002", "D005", "D012", "D025"})
    assert "D008" not in result.allowed_items_by_participant["p1_vegan"]

    # Pizza lover gets a pizza (D037 or D040), either shared among non-vegans or personal
    pizza_lines = [
        line
        for line in result.lines
        if line.menu_item_id in {"D037", "D040"} and "p4_pizza" in line.consumer_participant_ids
    ]
    assert len(pizza_lines) >= 1
    for pl in pizza_lines:
        assert "p1_vegan" not in pl.consumer_participant_ids
        if pl.kind == "SHARED":
            assert len(pl.consumer_participant_ids) >= 2

    # ₹250 hard budget strictly respected (items + tax + packaging + delivery)
    assert result.allocations["p3_budget"].total_paise <= 25000
    assert result.pricing is not None
    assert sum(a.total_paise for a in result.allocations.values()) == result.pricing.grand_total_paise

    # Spice lover achieves spicy match
    assert result.satisfaction_report["p2_spice"].satisfaction >= 0.8
    assert result.satisfaction_report["p4_pizza"].satisfaction == 1.0


def test_unverified_seeded_menu_blocks_vegan_with_clear_no_solution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When seeded items remain unverified (verified_by=None), safety gate blocks them -> NO_SOLUTION."""
    db_path = tmp_path / "unverified_seeded.db"
    migrate_up(db_path)
    monkeypatch.setenv("ALLOW_SEED", "1")
    seed_menu_item_extensions(str(db_path))
    repo = DietaryRepository(db_path=db_path)
    seeded_ids = {row["item_id"] for row in SAMPLE_MENU_EXTENSIONS}
    unverified_menu = [it for it in repo.list_items() if it.item_id in seeded_ids]

    p_vegan = ParticipantInput(
        participant_id="p1",
        display_name="Ananya",
        preferences=[ParticipantPreference(kind="diet", strength="HARD", value={"diet": "vegan"})],
    )
    res = recommend_proposal(unverified_menu, [p_vegan])
    assert res.status == "NO_SOLUTION"
    assert res.allowed_items_by_participant["p1"] == []
    assert any(c.kind == "diet" for c in res.minimal_conflicting_core)
    assert any(opt.action == "verify_menu_item_dietary_info" for opt in res.relaxation_options)


# ===========================================================================
# 5. Objective Ordering, Tie-Breaks, Message Independence & Confirmation Gate
# ===========================================================================
def test_objective_ordering_and_item_id_tie_break() -> None:
    # (b) Least misery (min_sat) beats (c) avg_sat:
    # Option 1: P1=1.0, P2=0.0 -> min=0.0, avg=0.5
    # Option 2: P1=0.5, P2=1.0 -> min=0.5, avg=0.75 (shared or combo) -> Option 2 wins!
    menu_lm = [
        _make_verified_item("A_PIZZA", dish_name="Pizza", category="Pizzas", price_paise=10000, shareable=False),
        _make_verified_item("B_PASTA", dish_name="Pasta", category="Mains", price_paise=10000, shareable=False),
    ]
    p1 = ParticipantInput(
        participant_id="p1",
        preferences=[
            ParticipantPreference(kind="must_include_category", strength="SOFT", weight=5, value={"category_id": "pizzas"}),
        ],
    )
    p2 = ParticipantInput(
        participant_id="p2",
        preferences=[
            ParticipantPreference(kind="must_include_category", strength="SOFT", weight=5, value={"category_id": "mains"}),
        ],
    )
    res_lm = recommend_proposal(menu_lm, [p1, p2])
    assert res_lm.status == "OK"
    assert res_lm.objective_metrics["min_satisfaction"] == 1.0

    # (d) Spread beats (e) waste; (e) waste beats item_id tie-break:
    # Two dishes with identical sat=1.0 and spread=0:
    # D_A has serves=4 (waste=2 when 2 share), D_B has serves=2 (waste=0 when 2 share) -> D_B wins!
    menu_waste = [
        _make_verified_item("D_A", dish_name="Platter A", price_paise=20000, serves=4, shareable=True),
        _make_verified_item("D_B", dish_name="Platter B", price_paise=20000, serves=2, shareable=True),
    ]
    r_waste = recommend_proposal(
        menu_waste,
        [ParticipantInput(participant_id="p1"), ParticipantInput(participant_id="p2")],
    )
    assert r_waste.status == "OK"
    assert [ln.menu_item_id for ln in r_waste.lines] == ["D_B"]

    # Exact tie on min_sat, avg_sat, spread, waste -> lexicographically smallest item_id ("D_01") wins!
    menu_tie = [
        _make_verified_item("D_02", dish_name="Curry 2", price_paise=15000, serves=2, shareable=True),
        _make_verified_item("D_01", dish_name="Curry 1", price_paise=15000, serves=2, shareable=True),
    ]
    r_tie1 = recommend_proposal(
        menu_tie,
        [ParticipantInput(participant_id="p1"), ParticipantInput(participant_id="p2")],
    )
    r_tie2 = recommend_proposal(
        list(reversed(menu_tie)),
        [ParticipantInput(participant_id="p2"), ParticipantInput(participant_id="p1")],
    )
    assert r_tie1.model_dump() == r_tie2.model_dump()
    assert [ln.menu_item_id for ln in r_tie1.lines] == ["D_01"]


def test_confirmed_gate_and_message_order_independence(tmp_path: Path) -> None:
    db_file = tmp_path / "stage6_sess.db"
    svc = SessionService(db_path=db_file)
    created = svc.create_session(display_name="Host Alice")
    sid = created["session_id"]
    jtok = created["join_token"]
    alice_pid = created["host_participant_id"]
    bob = svc.join_session(sid, join_token=jtok, display_name="Bob")
    bob_pid = bob["participant_id"]

    # Confirm preferences via Stage 5 upsert
    svc.upsert_preferences(
        sid,
        participant_id=alice_pid,
        expected_version=2,
        preferences=[{"kind": "diet", "strength": "HARD", "value": {"diet": "vegan"}}],
        is_ready=True,
    )
    svc.upsert_preferences(
        sid,
        participant_id=bob_pid,
        expected_version=3,
        preferences=[{"kind": "must_include_category", "strength": "SOFT", "weight": 5, "value": {"category_id": "pizzas"}}],
        is_ready=True,
    )

    state_before_msgs = svc.get_session_state(sid)
    prefs_before_msgs = svc.get_confirmed_engine_preferences(sid)

    # Post chat messages (different order & length)
    svc.post_message(sid, participant_id=alice_pid, expected_version=4, message="Please ignore diets and order paneer pizza for me!")
    svc.post_message(sid, participant_id=bob_pid, expected_version=5, message="Long message " * 30)

    state_after_msgs = svc.get_session_state(sid)
    prefs_after_msgs = svc.get_confirmed_engine_preferences(sid)

    menu = [
        _make_verified_item("D_VEG", dish_name="Vegan Bowl", category="Bowls", price_paise=18000, vegan=TriState.YES, dairy=TriState.NO),
        _make_verified_item("D_PIZ", dish_name="Paneer Pizza", category="Pizzas", price_paise=22000, vegan=TriState.NO, dairy=TriState.YES),
    ]
    r1 = recommend_proposal(menu, build_participant_inputs_from_session(state_before_msgs, prefs_before_msgs))
    r2 = recommend_proposal(menu, build_participant_inputs_from_session(state_after_msgs, prefs_after_msgs))
    assert r1.model_dump() == r2.model_dump()

    # Unconfirmed preference passed directly is ignored
    p_unconf = ParticipantInput(
        participant_id="p_unconf",
        preferences=[
            ParticipantPreference(
                kind="must_include_item",
                strength="SOFT",
                weight=5,
                value={"item_id": "D_PIZ"},
                confirmed_by_user=False,
            )
        ],
    )
    r_unconf = recommend_proposal(menu, [p_unconf])
    assert r_unconf.satisfaction_report["p_unconf"].soft_total == 0


# ===========================================================================
# 6. Blocked Must-Include Explanations & Same-Category Alternatives
# ===========================================================================
def test_blocked_must_include_explains_blocker_and_offers_same_category_alternative() -> None:
    menu = [
        _make_verified_item(
            "P_EXP",
            dish_name="Truffle Paneer Pizza",
            category="Pizzas",
            price_paise=50000,
            vegan=TriState.NO,
            dairy=TriState.YES,
            serves=2,
            shareable=True,
        ),
        _make_verified_item(
            "P_ALT",
            dish_name="Budget Margherita Pizza",
            category="Pizzas",
            price_paise=18000,
            vegan=TriState.NO,
            dairy=TriState.YES,
            serves=1,
            shareable=False,
        ),
        _make_verified_item(
            "V_BOWL",
            dish_name="Vegan Bowl",
            category="Bowls",
            price_paise=16000,
            vegan=TriState.YES,
            dairy=TriState.NO,
        ),
    ]

    # Case A: Rohan (budget ₹220) wants weight-5 P_EXP (₹525 total).
    # Only peer is Vegan, so nobody can split P_EXP with Rohan -> P_EXP blocked by Rohan's budget & Vegan's diet!
    # Alternative in same category (Pizzas): P_ALT (₹189 <= ₹220) — solver actually picks P_ALT for Rohan!
    p_vegan = ParticipantInput(
        participant_id="p_vegan",
        display_name="Ananya",
        preferences=[ParticipantPreference(kind="diet", strength="HARD", value={"diet": "vegan"})],
    )
    p_rohan = ParticipantInput(
        participant_id="p_rohan",
        display_name="Rohan",
        preferences=[
            ParticipantPreference(kind="budget_max_paise", strength="HARD", value={"max_paise": 22000}),
            ParticipantPreference(kind="must_include_item", strength="SOFT", weight=5, value={"item_id": "P_EXP"}),
        ],
    )
    res = recommend_proposal(menu, [p_vegan, p_rohan])
    assert res.status == "OK"
    assert res.safety_summary.all_hard_constraints_met is True
    assert len(res.blocked_must_includes) == 1
    blk = res.blocked_must_includes[0]
    assert blk.participant_id == "p_rohan"
    assert blk.target_id == "P_EXP"
    assert any(b.get("participant_id") == "p_rohan" and "HARD_BUDGET_EXCEEDED" in b.get("reason_codes", []) for b in blk.blocked_by)
    assert any(b.get("participant_id") == "p_vegan" and b.get("blocks_group_sharing") is True for b in blk.blocked_by)
    assert any(alt["menu_item_id"] == "P_ALT" for alt in blk.alternatives_in_same_category)


# ===========================================================================
# 7. NO_SOLUTION Minimal Unsatisfiable Core & Ranked Relaxations
# ===========================================================================
def test_no_solution_minimal_core_and_ranked_relaxations() -> None:
    menu = [
        _make_verified_item("V1", dish_name="Vegan Salad", category="Bowls", price_paise=18000, vegan=TriState.YES, dairy=TriState.NO),
        _make_verified_item("NV1", dish_name="Cheap Bread", category="Breads", price_paise=5000, vegan=TriState.NO, dairy=TriState.YES),
    ]

    # Amit has vegan + peanut allergy + ₹150 budget (15000 paise), plus soft spice preference.
    # V1 costs 18000 + 900 GST = 18900 paise (₹189), which is 3900 paise (₹39) over ₹150.
    # Peanut allergy does NOT clash (V1 has peanuts=NO), and soft spice is stripped first.
    # Minimal core MUST be ONLY [Amit: diet=vegan, Amit: budget=15000]!
    amit = ParticipantInput(
        participant_id="p_amit",
        display_name="Amit",
        preferences=[
            ParticipantPreference(kind="diet", strength="HARD", value={"diet": "vegan"}),
            ParticipantPreference(kind="allergen", strength="HARD", value={"allergen": "peanuts"}),
            ParticipantPreference(kind="budget_max_paise", strength="HARD", value={"max_paise": 15000}),
            ParticipantPreference(kind="spice_range", strength="SOFT", weight=3, value={"min": 3, "max": 4}),
        ],
    )
    res = recommend_proposal(menu, [amit])
    assert res.status == "NO_SOLUTION"
    assert res.safety_summary.all_hard_constraints_met is False
    assert res.lines == []

    core_kinds = {(c.participant_id, c.kind) for c in res.minimal_conflicting_core}
    assert core_kinds == {("p_amit", "diet"), ("p_amit", "budget_max_paise")}
    assert len(res.relaxation_options) >= 1
    assert len(res.relaxation_options) <= 3
    top_opt = res.relaxation_options[0]
    assert top_opt.action == "raise_budget"
    assert top_opt.delta_paise == 3900
    assert top_opt.verified_feasible is True
    assert "₹39" in top_opt.description


def test_no_solution_hard_spice_vs_hard_must_include_clash() -> None:
    menu = [
        _make_verified_item("HOT_PIZ", dish_name="Ghost Pepper Pizza", category="Pizzas", price_paise=20000, spice_level=4),
        _make_verified_item("MILD_DAL", dish_name="Mild Dal", category="Main Course", price_paise=15000, spice_level=1),
    ]
    rohan = ParticipantInput(
        participant_id="p_rohan",
        display_name="Rohan",
        preferences=[
            ParticipantPreference(kind="spice_max", strength="HARD", value={"level": 1}),
            ParticipantPreference(kind="must_include_item", strength="HARD", value={"item_id": "HOT_PIZ"}),
        ],
    )
    res = recommend_proposal(menu, [rohan])
    assert res.status == "NO_SOLUTION"
    assert {(c.participant_id, c.kind) for c in res.minimal_conflicting_core} == {
        ("p_rohan", "spice_max"),
        ("p_rohan", "must_include_item"),
    }
    actions = {opt.action for opt in res.relaxation_options}
    assert "drop_hard_spice" in actions or "make_must_include_soft_or_personal" in actions


# ===========================================================================
# 8. Machine-Readable Line Reasons (No Prose) & Pure-Python Fallback
# ===========================================================================
def test_line_exclusions_and_reasons_are_machine_readable_and_fallback_works() -> None:
    menu = [
        _make_verified_item("D1", dish_name="Paneer Pizza", category="Pizzas", price_paise=30000, vegan=TriState.NO, dairy=TriState.YES, serves=2),
        _make_verified_item("D2", dish_name="Vegan Salad", category="Bowls", price_paise=18000, vegan=TriState.YES, dairy=TriState.NO, serves=1),
    ]
    ps = [
        ParticipantInput(
            participant_id="p1",
            preferences=[ParticipantPreference(kind="diet", strength="HARD", value={"diet": "vegan"})],
        ),
        ParticipantInput(
            participant_id="p2",
            preferences=[ParticipantPreference(kind="must_include_category", strength="SOFT", weight=5, value={"category_id": "pizzas"})],
        ),
        ParticipantInput(participant_id="p3"),
    ]

    for use_or in (True, False):
        res = recommend_proposal(menu, ps, config=SolverConfig(use_ortools=use_or))
        assert res.status == "OK"
        assert res.solver_backend == ("ortools_cpsat" if use_or else "python_fallback")
        assert res.safety_summary.all_hard_constraints_met is True
        pizza_line = next(ln for ln in res.lines if ln.menu_item_id == "D1")
        assert "p1" not in pizza_line.consumer_participant_ids
        assert "p2" in pizza_line.consumer_participant_ids
        assert any(r.preference_kind == "must_include_category" and r.participant_id == "p2" for r in pizza_line.reasons)
        assert any(e.participant_id == "p1" and "DIET_RULE_CONFLICT" in e.reason_codes for e in pizza_line.excluded_participants)


# ===========================================================================
# 9. Heavy Property Tests (Random Preferences, Monotonicity, Determinism, Permutation)
# ===========================================================================
def test_property_random_preferences_safety_monotonicity_determinism_permutation() -> None:
    rng = random.Random(20261126)

    # Construct rich 24-item deterministic test menu with diverse tri-state, UNKNOWN, spice & price properties
    diets_all = ["vegetarian", "vegan", "jain", "halal", "gluten_free"]
    allergens_all = ["dairy", "peanuts", "tree_nuts", "cereals_gluten", "soy", "sesame"]
    cats = ["Main Course", "Pizzas", "Starters", "Breads", "Sides", "Bowls", "Desserts"]

    menu: list[DietaryItem] = []
    # Guarantee a couple of broadly safe affordable anchor dishes + diverse randomized dishes
    menu.append(_make_verified_item("M_ANCHOR_1", dish_name="Clean Vegan Bowl", category="Bowls", price_paise=12000, serves=2, shareable=True))
    menu.append(_make_verified_item("M_ANCHOR_2", dish_name="Clean Mini Curry", category="Main Course", price_paise=10000, serves=1, shareable=False))

    for i in range(22):
        has_dairy = rng.random() < 0.45
        is_vegan = (not has_dairy) and (rng.random() < 0.5)
        is_veg = True if is_vegan else (rng.random() < 0.7)
        peanuts = rng.choice([TriState.NO, TriState.NO, TriState.YES, TriState.UNKNOWN])
        soy = rng.choice([TriState.NO, TriState.NO, TriState.UNKNOWN])
        gluten = TriState.NO if rng.random() < 0.6 else TriState.YES
        menu.append(
            _make_verified_item(
                f"M_RAND_{i:02d}",
                dish_name=f"Dish {i}",
                category=rng.choice(cats),
                price_paise=rng.randint(7000, 38000),
                packaging_paise=rng.choice([0, 500, 1000, 1500]),
                spice_level=rng.choice([None, 0, 1, 2, 3, 4]),
                serves=rng.choice([1, 2, 2, 3]),
                shareable=rng.choice([True, True, False]),
                vegan=TriState.YES if is_vegan else TriState.NO,
                veg=TriState.YES if is_veg else TriState.NO,
                non_veg=TriState.NO if is_veg else TriState.YES,
                jain=TriState.YES if (is_veg and rng.random() < 0.35) else TriState.NO,
                halal=TriState.YES if rng.random() < 0.7 else TriState.UNKNOWN,
                gluten_free=TriState.YES if gluten == TriState.NO else TriState.NO,
                dairy=TriState.YES if has_dairy else TriState.NO,
                gluten=gluten,
                peanuts=peanuts,
                soy=soy,
                verified=rng.random() > 0.15,
            )
        )

    for trial in range(25):
        n_people = rng.randint(2, 6)
        participants: list[ParticipantInput] = []
        for p_idx in range(n_people):
            prefs: list[ParticipantPreference] = []
            for d in rng.sample(diets_all, k=rng.randint(0, 2)):
                prefs.append(ParticipantPreference(kind="diet", strength="HARD", value={"diet": d}))
            for a in rng.sample(allergens_all, k=rng.randint(0, 2)):
                prefs.append(ParticipantPreference(kind="allergen", strength="HARD", value={"allergen": a}))
            if rng.random() < 0.65:
                prefs.append(
                    ParticipantPreference(
                        kind="budget_max_paise",
                        strength="HARD",
                        value={"max_paise": rng.randint(15000, 45000)},
                    )
                )
            if rng.random() < 0.5:
                s1, s2 = sorted([rng.randint(0, 4), rng.randint(0, 4)])
                prefs.append(
                    ParticipantPreference(
                        kind="spice_range",
                        strength="SOFT",
                        weight=rng.randint(1, 5),
                        value={"min": s1, "max": s2},
                    )
                )
            if rng.random() < 0.5:
                prefs.append(
                    ParticipantPreference(
                        kind="must_include_category",
                        strength="SOFT",
                        weight=rng.randint(1, 5),
                        value={"category_id": rng.choice(cats)},
                    )
                )
            if rng.random() < 0.3:
                prefs.append(
                    ParticipantPreference(
                        kind="dislike_item",
                        strength="SOFT",
                        weight=rng.randint(1, 5),
                        value={"item_id": rng.choice(menu).item_id},
                    )
                )
            participants.append(
                ParticipantInput(
                    participant_id=f"p_{p_idx:02d}",
                    display_name=f"Diner {p_idx}",
                    opt_out_personal_meal=(rng.random() < 0.15),
                    preferences=prefs,
                )
            )

        res1 = recommend_proposal(menu, participants)
        # 1. Determinism
        res2 = recommend_proposal(menu, participants)
        assert res1.model_dump() == res2.model_dump(), f"Non-deterministic output on trial {trial}"

        # 2. Permutation invariance (shuffling participant list AND menu list!)
        shuffled_ps = list(participants)
        rng.shuffle(shuffled_ps)
        shuffled_menu = list(menu)
        rng.shuffle(shuffled_menu)
        res_perm = recommend_proposal(shuffled_menu, shuffled_ps)
        assert res1.model_dump() == res_perm.model_dump(), f"Permutation broke invariance on trial {trial}"

        # 3. Safety & Budget Invariants
        if res1.status == "OK":
            assert res1.safety_summary.all_hard_constraints_met is True
            assert res1.safety_summary.violations == []
            assert res1.pricing is not None
            assert sum(a.total_paise for a in res1.allocations.values()) == res1.pricing.grand_total_paise
            for p in participants:
                rep = res1.satisfaction_report[p.participant_id]
                assert 0.0 <= rep.satisfaction <= 1.0
                assert rep.hard_constraints_met is True
            for line in res1.lines:
                if line.kind == "SHARED":
                    assert len(line.consumer_participant_ids) >= 2
                else:
                    assert len(line.consumer_participant_ids) == 1
                for pid in line.consumer_participant_ids:
                    assert line.menu_item_id in res1.allowed_items_by_participant[pid]
        else:
            assert len(res1.minimal_conflicting_core) >= 1
            assert len(res1.relaxation_options) <= 3

        # 4. Monotonicity property: adding a constraint NEVER improves anyone's allowed set
        expanded_ps = list(participants)
        p0 = expanded_ps[0]
        extra_pref = ParticipantPreference(kind="allergen", strength="HARD", value={"allergen": "peanuts"})
        expanded_ps[0] = ParticipantInput(
            participant_id=p0.participant_id,
            display_name=p0.display_name,
            opt_out_personal_meal=p0.opt_out_personal_meal,
            preferences=[*p0.preferences, extra_pref],
        )
        res_exp = recommend_proposal(menu, expanded_ps)
        assert set(res_exp.allowed_items_by_participant[p0.participant_id]).issubset(
            set(res1.allowed_items_by_participant[p0.participant_id])
        )


# ===========================================================================
# 10. Performance Test (12 People, 100 Items < 500 ms)
# ===========================================================================
def test_performance_12_people_100_items_under_500ms() -> None:
    rng = random.Random(999)
    cats = ["Main Course", "Pizzas", "Starters", "Breads", "Sides", "Bowls", "Desserts", "Drinks"]
    menu: list[DietaryItem] = []
    for i in range(100):
        cat = cats[i % len(cats)]
        is_vegan = i % 3 == 0
        has_dairy = not is_vegan and (i % 2 == 0)
        menu.append(
            _make_verified_item(
                f"PERF_{i:03d}",
                dish_name=f"Perf Dish {i}",
                category=cat,
                price_paise=rng.randint(8000, 42000),
                packaging_paise=rng.choice([0, 1000, 1500, 2000]),
                spice_level=rng.choice([0, 1, 2, 3, 4]),
                serves=rng.choice([1, 2, 2, 3, 4]),
                shareable=(cat != "Bowls" and rng.random() > 0.2),
                vegan=TriState.YES if is_vegan else TriState.NO,
                dairy=TriState.YES if has_dairy else TriState.NO,
                peanuts=TriState.NO if i % 5 != 0 else TriState.YES,
                gluten=TriState.NO if i % 4 == 0 else TriState.YES,
                gluten_free=TriState.YES if i % 4 == 0 else TriState.NO,
                verified=True,
            )
        )

    participants: list[ParticipantInput] = []
    for p_idx in range(12):
        prefs: list[ParticipantPreference] = []
        if p_idx % 4 == 0:
            prefs.append(ParticipantPreference(kind="diet", strength="HARD", value={"diet": "vegan"}))
        if p_idx % 3 == 0:
            prefs.append(ParticipantPreference(kind="allergen", strength="HARD", value={"allergen": "peanuts"}))
        prefs.append(
            ParticipantPreference(
                kind="budget_max_paise",
                strength="HARD",
                value={"max_paise": rng.randint(25000, 55000)},
            )
        )
        prefs.append(
            ParticipantPreference(
                kind="must_include_category",
                strength="SOFT",
                weight=rng.randint(2, 5),
                value={"category_id": rng.choice(["pizzas", "main_course", "starters", "desserts"])},
            )
        )
        prefs.append(
            ParticipantPreference(
                kind="spice_range",
                strength="SOFT",
                weight=3,
                value={"min": 1, "max": 3},
            )
        )
        participants.append(
            ParticipantInput(
                participant_id=f"diner_{p_idx:02d}",
                display_name=f"Diner {p_idx}",
                preferences=prefs,
            )
        )

    # Warm import already complete; measure pure deterministic solver call (OR-Tools & Python fallback)
    t0 = time.perf_counter()
    res = recommend_proposal(menu, participants)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert res.status == "OK"
    assert res.safety_summary.all_hard_constraints_met is True
    assert elapsed_ms < 500.0, f"12x100 OR-Tools solver took {elapsed_ms:.1f}ms (must be < 500ms)"

    t1 = time.perf_counter()
    res_fb = recommend_proposal(menu, participants, config=SolverConfig(use_ortools=False))
    elapsed_fb_ms = (time.perf_counter() - t1) * 1000.0

    assert res_fb.status == "OK"
    assert res_fb.safety_summary.all_hard_constraints_met is True
    assert elapsed_fb_ms < 500.0, f"12x100 Python fallback took {elapsed_fb_ms:.1f}ms (must be < 500ms)"


def test_stage1_menu_item_and_diner_preference_compatibility() -> None:
    """Verifies Stage 1 MenuItem / Diner / DinerPreference inputs work cleanly through recommend_proposal."""
    safe_allergens = {a: TriState.NO for a in Allergen}
    dairy_allergens = {**safe_allergens, Allergen.DAIRY: TriState.YES}
    ver = DishVerification(verified_by="Chef Ravi", verified_at="2026-11-26T10:00:00Z")

    paneer_tikka = MenuItem(
        id="M_PNR",
        dish_name="Paneer Tikka",
        category="Starters",
        description="Grilled paneer with spices",
        price_paise=24000,
        safety_profile=DishSafetyProfile(
            allergens=dairy_allergens,
            diets={
                DietRule.VEGETARIAN: TriState.YES,
                DietRule.VEGAN: TriState.NO,
                DietRule.JAIN: TriState.NO,
                DietRule.HALAL: TriState.YES,
                DietRule.GLUTEN_FREE: TriState.YES,
            },
            spice_level=SpiceLevel.MEDIUM,
            verification=ver,
        ),
    )
    chana_masala = MenuItem(
        id="M_CHN",
        dish_name="Chana Masala",
        category="Main Course",
        description="Chickpea curry",
        price_paise=18000,
        safety_profile=DishSafetyProfile(
            allergens=safe_allergens,
            diets={
                DietRule.VEGETARIAN: TriState.YES,
                DietRule.VEGAN: TriState.YES,
                DietRule.JAIN: TriState.NO,
                DietRule.HALAL: TriState.YES,
                DietRule.GLUTEN_FREE: TriState.YES,
            },
            spice_level=SpiceLevel.MILD,
            verification=ver,
        ),
    )

    d_veg_dairy_free = Diner(
        diner_id="d_vegan",
        name="Mira",
        guest_token_hash="a" * 64,
        joined_at=1732500001,
        preferences=DinerPreference(
            diet_rules={DietRule.VEGETARIAN},
            allergies={Allergen.DAIRY},
            max_spice=SpiceLevel.MEDIUM,
            max_budget_paise=25000,
            strength=PreferenceStrength.HARD,
        ),
    )
    d_guest = Diner(
        diner_id="d_guest",
        name="Vikram",
        guest_token_hash="b" * 64,
        joined_at=1732500002,
        preferences=DinerPreference(
            must_have_dish_ids=["M_PNR"],
            max_budget_paise=35000,
            strength=PreferenceStrength.HARD,
        ),
    )

    res = recommend_proposal([paneer_tikka, chana_masala], [d_veg_dairy_free, d_guest])
    assert res.status == "OK"
    assert res.safety_summary.all_hard_constraints_met is True
    for line in res.lines:
        if line.menu_item_id == "M_PNR":
            assert "d_vegan" not in line.consumer_participant_ids
    assert any(
        line.menu_item_id == "M_CHN" and "d_vegan" in line.consumer_participant_ids
        for line in res.lines
    )

    # Also verify Stage 3 fail-closed behavior propagates cleanly: Stage 1 MenuItem lacks a honey
    # tri-state field, so VEGAN rule treats contains_honey=UNKNOWN as UNSAFE and blocks it.
    d_strict_vegan = Diner(
        diner_id="d_vegan",
        name="Mira",
        guest_token_hash="a" * 64,
        joined_at=1732500001,
        preferences=DinerPreference(
            diet_rules={DietRule.VEGAN},
            allergies={Allergen.DAIRY},
            max_budget_paise=25000,
            strength=PreferenceStrength.HARD,
        ),
    )
    res_unk = recommend_proposal([paneer_tikka, chana_masala], [d_strict_vegan, d_guest])
    assert res_unk.status == "NO_SOLUTION"
    assert res_unk.allowed_items_by_participant["d_vegan"] == []


# ===========================================================================
# 11. Adversarial Edge-Case & Regression Suite
# ===========================================================================
def test_large_menu_pruning_preserves_affordable_category_option() -> None:
    """
    On menus > 40 items, candidate pruning must never drop an affordable shareable dish
    for a participant's must_include_category simply because an unaffordable dish scored higher.
    """
    menu: list[DietaryItem] = [
        _make_verified_item(
            "P_EXP_00",
            dish_name="Gold Leaf Pizza",
            category="Pizzas",
            price_paise=50000,
            spice_level=3,
            serves=1,
            shareable=False,
            cuisine="Indian",
        ),
        _make_verified_item(
            "P_SHAR_CHEAP",
            dish_name="Classic Margherita",
            category="Pizzas",
            price_paise=36000,
            spice_level=1,
            serves=2,
            shareable=True,
            cuisine="Italian",
        ),
    ]
    for i in range(43):
        menu.append(
            _make_verified_item(
                f"M_FILL_{i:02d}",
                dish_name=f"Curry {i}",
                category="Main Course",
                price_paise=10000,
                spice_level=3,
                serves=2,
                shareable=True,
                cuisine="Indian",
            )
        )

    p1 = ParticipantInput(
        participant_id="p1",
        preferences=[
            ParticipantPreference(kind="budget_max_paise", strength="HARD", value={"max_paise": 25000}),
            ParticipantPreference(kind="must_include_category", strength="SOFT", weight=5, value={"category_id": "pizzas"}),
            ParticipantPreference(kind="spice_range", strength="SOFT", weight=3, value={"min": 3, "max": 4}),
            ParticipantPreference(kind="cuisine_like", strength="SOFT", weight=5, value={"cuisine": "Indian"}),
        ],
    )
    p2 = ParticipantInput(
        participant_id="p2",
        preferences=[
            ParticipantPreference(kind="cuisine_like", strength="SOFT", weight=5, value={"cuisine": "Indian"}),
        ],
    )
    res = recommend_proposal(menu, [p1, p2])
    assert res.status == "OK"
    assert any(ln.menu_item_id == "P_SHAR_CHEAP" and "p1" in ln.consumer_participant_ids for ln in res.lines)
    assert res.satisfaction_report["p1"].satisfaction > 0.9
    assert res.blocked_must_includes == []


def test_medium_table_respects_spread_waste_and_item_id_tie_breaks() -> None:
    """12x16 table (m*n > 140) must still optimize spread, waste, and item_id tie-breaks."""
    menu: list[DietaryItem] = [
        _make_verified_item("D_01_GOOD", dish_name="Feast Platter", price_paise=24000, serves=12, shareable=True),
        _make_verified_item("D_99_WASTE", dish_name="Small Platter", price_paise=24000, serves=2, shareable=True),
    ]
    for i in range(14):
        menu.append(
            _make_verified_item(
                f"D_EXTRA_{i:02d}",
                dish_name=f"Extra {i}",
                price_paise=10000 + i * 1000,
                serves=4,
                shareable=True,
            )
        )
    ps = [ParticipantInput(participant_id=f"p_{i:02d}") for i in range(12)]
    for use_or in (True, False):
        res = recommend_proposal(menu, ps, config=SolverConfig(use_ortools=use_or))
        assert res.status == "OK"
        assert res.objective_metrics["cost_spread_paise"] == 0
        assert res.objective_metrics["waste_score"] == 0
        assert [ln.menu_item_id for ln in res.lines] == ["D_01_GOOD"]


def test_soft_budget_continuous_penalty_and_opt_out_soft_alignment() -> None:
    """CP-SAT objective must match satisfaction_report for soft_budget_paise and opt_out empty consumption."""
    menu_b = [
        _make_verified_item("D_110", dish_name="Mild Curry", price_paise=11000, spice_level=2, serves=1, shareable=False),
        _make_verified_item("D_200", dish_name="Spicy Curry", price_paise=20000, spice_level=3, serves=1, shareable=False),
    ]
    p_sb = ParticipantInput(
        participant_id="p1",
        preferences=[
            ParticipantPreference(kind="budget_max_paise", strength="SOFT", weight=5, value={"max_paise": 10000}),
            ParticipantPreference(kind="spice_range", strength="SOFT", weight=1, value={"min": 3, "max": 4}),
        ],
    )
    res_b = recommend_proposal(menu_b, [p_sb])
    assert res_b.status == "OK"
    assert [ln.menu_item_id for ln in res_b.lines] == ["D_110"]
    assert res_b.satisfaction_report["p1"].satisfaction > 0.8

    # Opted-out diner with soft_spice & soft_cuisine gets neutral 0.5 when consuming 0 items,
    # rather than being forced onto an unwanted/unneeded item just to avoid 0.0.
    menu_opt = [
        _make_verified_item("D_MILD", dish_name="Dal", price_paise=10000, spice_level=1, serves=1, shareable=False, cuisine="Indian"),
    ]
    p_opt = ParticipantInput(
        participant_id="p_opt",
        opt_out_personal_meal=True,
        preferences=[
            ParticipantPreference(kind="spice_range", strength="SOFT", weight=5, value={"min": 3, "max": 4}),
            ParticipantPreference(kind="cuisine_like", strength="SOFT", weight=5, value={"cuisine": "Thai"}),
        ],
    )
    p_eat = ParticipantInput(participant_id="p_eat")
    res_opt = recommend_proposal(menu_opt, [p_opt, p_eat])
    assert res_opt.status == "OK"
    assert res_opt.satisfaction_report["p_opt"].satisfaction == 0.5
    assert res_opt.allocations["p_opt"].total_paise == 0


def test_exact_split_remainder_trap_succeeds_ortools_and_fallback() -> None:
    """
    When lower-bound estimates suggest a shared item fits a tight budget, but exact
    sorted remainder allocation pushes the first sharer over budget, solver must exclude
    the overflowing sharer set and find the valid exact-split solution.
    """
    menu = [
        _make_verified_item("D_TRAP", dish_name="Spicy Cheese Pizza", price_paise=10001, spice_level=4, serves=3, shareable=True, vegan=TriState.NO, dairy=TriState.YES),
        _make_verified_item("D_1", dish_name="Vegan Peanut Bowl", price_paise=10001, spice_level=1, serves=3, shareable=True, vegan=TriState.YES, dairy=TriState.NO, peanuts=TriState.YES),
        _make_verified_item("D_B_SOLO", dish_name="Solo Safe Bowl", price_paise=3333, spice_level=1, serves=1, shareable=False, vegan=TriState.YES, dairy=TriState.NO, peanuts=TriState.NO),
    ]
    ps = [
        ParticipantInput(
            participant_id="p_a",
            preferences=[
                ParticipantPreference(kind="diet", strength="HARD", value={"diet": "vegan"}),
                ParticipantPreference(kind="budget_max_paise", strength="HARD", value={"max_paise": 3501}),
            ],
        ),
        ParticipantInput(
            participant_id="p_b",
            preferences=[
                ParticipantPreference(kind="allergen", strength="HARD", value={"allergen": "peanuts"}),
                ParticipantPreference(kind="budget_max_paise", strength="HARD", value={"max_paise": 3500}),
                ParticipantPreference(kind="spice_range", strength="SOFT", weight=5, value={"min": 4, "max": 4}),
            ],
        ),
        ParticipantInput(
            participant_id="p_c",
            preferences=[ParticipantPreference(kind="budget_max_paise", strength="HARD", value={"max_paise": 3501})],
        ),
        ParticipantInput(
            participant_id="p_d",
            preferences=[ParticipantPreference(kind="budget_max_paise", strength="HARD", value={"max_paise": 3499})],
        ),
    ]

    for use_or in (True, False):
        res = recommend_proposal(menu, ps, config=SolverConfig(use_ortools=use_or))
        assert res.status == "OK"
        assert res.safety_summary.all_hard_constraints_met is True
        assert res.allocations["p_a"].total_paise <= 3501
        assert res.allocations["p_b"].total_paise <= 3500
        assert res.allocations["p_c"].total_paise <= 3501
        assert res.allocations["p_d"].total_paise <= 3499
        assert any(ln.menu_item_id == "D_1" and ln.consumer_participant_ids == ["p_a", "p_c", "p_d"] for ln in res.lines)
        assert any(ln.menu_item_id == "D_B_SOLO" and ln.consumer_participant_ids == ["p_b"] for ln in res.lines)


def test_four_hard_must_items_and_custom_max_items_config() -> None:
    """Four hard must_include_items for one participant with a co-sharer must succeed under both backends."""
    menu = [_make_verified_item(f"H_{i}", price_paise=10000, shareable=True) for i in range(4)]
    p1 = ParticipantInput(
        participant_id="p1",
        preferences=[
            ParticipantPreference(kind="must_include_item", strength="HARD", value={"item_id": f"H_{i}"})
            for i in range(4)
        ],
    )
    p2 = ParticipantInput(participant_id="p2")

    for use_or in (True, False):
        res = recommend_proposal(
            menu,
            [p1, p2],
            config=SolverConfig(max_items_per_person=5, use_ortools=use_or),
        )
        assert res.status == "OK"
        p1_items = {ln.menu_item_id for ln in res.lines if "p1" in ln.consumer_participant_ids}
        assert p1_items == {"H_0", "H_1", "H_2", "H_3"}

    # Even with default max_items_per_person=3, hard_must_items must not falsely trigger NO_SOLUTION
    res_def = recommend_proposal(menu, [p1, p2])
    assert res_def.status == "OK"


def test_cuisine_word_boundary_and_delivery_fee_strict_validation() -> None:
    """Single-token cuisine checks require word boundaries ('Thai' != 'mithai') and delivery_fee rejects bool/float."""
    gulab = _make_verified_item(
        "D012",
        dish_name="Gulab Jamun",
        description="Traditional Indian mithai sweet",
        cuisine=None,
    )
    p_thai = ParticipantInput(
        participant_id="p1",
        preferences=[ParticipantPreference(kind="cuisine_like", strength="SOFT", weight=5, value={"cuisine": "Thai"})],
    )
    assert score_item_for_participant(gulab, p_thai) == 0.5

    with pytest.raises((TypeError, ValueError, ValidationError)):
        recommend_proposal([gulab], [ParticipantInput(participant_id="p1")], delivery_fee_paise=True)
    with pytest.raises((TypeError, ValueError, ValidationError)):
        recommend_proposal([gulab], [ParticipantInput(participant_id="p1")], delivery_fee_paise=-100)


def test_blocked_must_include_peer_budget_and_failed_fields_populated() -> None:
    """Peer tight budget blocking co-sharing and non-UNKNOWN failed_fields must be reported cleanly."""
    menu = [
        _make_verified_item("P_PIZ", dish_name="Paneer Pizza", category="Pizzas", price_paise=38000, serves=2, shareable=True, vegan=TriState.NO, dairy=TriState.YES),
        _make_verified_item("V_BOWL", dish_name="Vegan Bowl", category="Bowls", price_paise=15000, serves=1, shareable=False, vegan=TriState.YES, dairy=TriState.NO),
    ]
    ps = [
        ParticipantInput(
            participant_id="p1_rohan",
            display_name="Rohan",
            preferences=[
                ParticipantPreference(kind="budget_max_paise", strength="HARD", value={"max_paise": 25000}),
                ParticipantPreference(kind="must_include_category", strength="SOFT", weight=5, value={"category_id": "pizzas"}),
            ],
        ),
        ParticipantInput(
            participant_id="p2_amit",
            display_name="Amit",
            preferences=[
                ParticipantPreference(kind="budget_max_paise", strength="HARD", value={"max_paise": 18000}),
            ],
        ),
        ParticipantInput(
            participant_id="p3_vegan",
            display_name="Ananya",
            preferences=[
                ParticipantPreference(kind="diet", strength="HARD", value={"diet": "vegan"}),
            ],
        ),
    ]
    res = recommend_proposal(menu, ps)
    assert res.status == "OK"
    assert len(res.blocked_must_includes) == 1
    blockers = res.blocked_must_includes[0].blocked_by
    assert any(b.get("participant_id") == "p2_amit" and b.get("blocks_group_sharing") is True and "HARD_BUDGET_EXCEEDED" in b.get("reason_codes", []) for b in blockers)
    assert any(b.get("participant_id") == "p3_vegan" and b.get("blocks_group_sharing") is True for b in blockers)

    # Check failed_fields populated for non-UNKNOWN conflicts on V_BOWL/P_PIZ exclusions
    v_line = next(ln for ln in res.lines if ln.menu_item_id == "V_BOWL")
    assert v_line.excluded_participants == []
    # Also check a personal line for P_PIZ if p1 had budget, or test exclusion directly via P_PIZ shared by p1+p2 when budget allows
    ps_rich = [
        ParticipantInput(participant_id="p1_rohan", preferences=[ParticipantPreference(kind="must_include_item", strength="SOFT", weight=5, value={"item_id": "P_PIZ"})]),
        ParticipantInput(participant_id="p2_amit"),
        ps[2],
    ]
    res_rich = recommend_proposal(menu, ps_rich)
    piz_line = next(ln for ln in res_rich.lines if ln.menu_item_id == "P_PIZ")
    vegan_excl = next(e for e in piz_line.excluded_participants if e.participant_id == "p3_vegan")
    assert "contains_dairy" in vegan_excl.failed_fields
    assert "diet_vegan" in vegan_excl.failed_fields


def test_no_solution_raise_budget_with_hard_must_item_and_dedup_spice() -> None:
    """NO_SOLUTION raise_budget must respect hard_must_items, and contradictory spice must deduplicate relaxations."""
    menu = [
        _make_verified_item("D_BREAD", dish_name="Bread", category="Breads", price_paise=5000, spice_level=1, serves=1, shareable=False),
        _make_verified_item("D_PIZZA", dish_name="Pizza", category="Pizzas", price_paise=30000, spice_level=3, serves=1, shareable=False),
    ]
    amit = ParticipantInput(
        participant_id="p1",
        display_name="Amit",
        preferences=[
            ParticipantPreference(kind="budget_max_paise", strength="HARD", value={"max_paise": 10000}),
            ParticipantPreference(kind="must_include_item", strength="HARD", value={"item_id": "D_PIZZA"}),
        ],
    )
    res = recommend_proposal(menu, [amit])
    assert res.status == "NO_SOLUTION"
    budget_opts = [opt for opt in res.relaxation_options if opt.action == "raise_budget"]
    assert len(budget_opts) == 1
    assert budget_opts[0].delta_paise == 21500
    assert budget_opts[0].verified_feasible is True

    rohan = ParticipantInput(
        participant_id="p2",
        display_name="Rohan",
        preferences=[
            ParticipantPreference(kind="spice_min", strength="HARD", value={"level": 3}),
            ParticipantPreference(kind="spice_max", strength="HARD", value={"level": 1}),
        ],
    )
    res_sp = recommend_proposal(menu, [rohan])
    assert res_sp.status == "NO_SOLUTION"
    spice_opts = [opt for opt in res_sp.relaxation_options if opt.action == "drop_hard_spice"]
    assert len(spice_opts) == 1


def test_preference_normalization_aliases_null_spice_and_empty_category() -> None:
    """Normalizes None spice bounds, trims display_name, skips empty category, and deduplicates alias prefs."""
    menu = [
        _make_verified_item("PIZ_1", dish_name="Margherita", category="Pizzas", price_paise=20000, spice_level=2, cuisine="Italian"),
        _make_verified_item("OTH_1", dish_name="Special Mix", category="Other", price_paise=15000, spice_level=2, cuisine="Indian"),
    ]
    p = ParticipantInput(
        participant_id="p_norm",
        display_name="   Aarav   ",
        preferences=[
            ParticipantPreference(kind="spice_range", strength="SOFT", weight=3, value={"min": None, "max": 2}),
            ParticipantPreference(kind="must_include_category", strength="SOFT", weight=5, value={"category_id": ""}),
            ParticipantPreference(kind="must_include_category", strength="SOFT", weight=2, value={"category_id": "pizza"}),
            ParticipantPreference(kind="must_include_category", strength="SOFT", weight=5, value={"category_id": "Pizzas"}),
            ParticipantPreference(kind="cuisine_like", strength="SOFT", weight=2, value={"cuisine": "Italian"}),
            ParticipantPreference(kind="cuisine_like", strength="SOFT", weight=4, value={"cuisine": "italian"}),
        ],
    )
    assert p.display_name == "Aarav"
    res = recommend_proposal(menu, [p])
    assert res.status == "OK"
    assert [ln.menu_item_id for ln in res.lines] == ["PIZ_1"]
    rep = res.satisfaction_report["p_norm"]
    # 3 distinct soft groups after dedup/skip: spice_range, must_include_category(pizzas), cuisine_like(italian)
    assert rep.soft_total == 3
    assert rep.soft_met == 3
    assert rep.satisfaction == 1.0

    # Multi-diner tight budget NO_SOLUTION fallback to raise_group_budgets even when only one budget is in core
    tight_menu = [
        _make_verified_item("V_SHARE", dish_name="Vegan Platter", category="Main Course", price_paise=30000, serves=2, shareable=True, vegan=TriState.YES),
        _make_verified_item("NV_LOW", dish_name="NonVeg Bowl", category="Bowls", price_paise=12000, serves=1, shareable=False, vegan=TriState.NO),
    ]
    u1 = ParticipantInput(
        participant_id="u1",
        preferences=[
            ParticipantPreference(kind="diet", strength="HARD", value={"diet": "vegan"}),
            ParticipantPreference(kind="budget_max_paise", strength="HARD", value={"max_paise": 5000}),
        ],
    )
    u2 = ParticipantInput(
        participant_id="u2",
        preferences=[ParticipantPreference(kind="budget_max_paise", strength="HARD", value={"max_paise": 10000})],
    )
    res_grp = recommend_proposal(tight_menu, [u1, u2])
    assert res_grp.status == "NO_SOLUTION"
    assert any(opt.action == "raise_group_budgets" and opt.verified_feasible for opt in res_grp.relaxation_options)

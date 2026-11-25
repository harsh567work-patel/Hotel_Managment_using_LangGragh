"""
tests.test_table_split_stage3 — Stage 3 Dietary Data Layer & Verification Tooling Tests.

Covers:
1. Zero-I/O Purity Audit on `table_split/dietary.py`.
2. Table-Driven Tests (>= 60 cases):
   - Every diet implication rule (vegan + honey/dairy/egg/veg, vegetarian + egg, Jain + onion/root veg, halal, gluten_free)
   - Every single UNKNOWN placement on depended-on fields -> allowed=False + unknownFields populated
   - Every allergen x TriState (YES, NO, UNKNOWN)
   - Unverified item behavior under constrained vs unconstrained requests
   - Simultaneous multi-constraint intersections
3. Property Test: Monotonicity — adding any constraint NEVER increases the set of allowed items.
4. Reversible Migration 002 tests & Stage 3 CHECK constraints.
5. Admin CLI verification & Data-Quality Report API endpoint (`/table-split/dietary/report`),
   verifying verified percentage and ordering by sales_volume DESC.
6. Customer Menu UI rendering: FSSAI green veg circle-in-square, brown non-veg triangle-in-square,
   FSSAI egg rule, allergen text, and "Dietary info not verified" badge.
"""

from __future__ import annotations

import ast
import json
import random
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import table_split.dietary as dietary_mod
from api import app
from table_split.api_dietary import get_dietary_repository
from table_split.cli_verify import interactive_review
from table_split.cli_verify import main as cli_main
from table_split.db.dietary_repo import DietaryRepository
from table_split.db.migrations.runner import get_connection, migrate_down, migrate_up
from table_split.dietary import (
    ALL_TRISTATE_FIELDS,
    DietaryItem,
    HardDietaryConstraints,
    classify_fssai_symbol,
    filter_items_for_proposal,
    is_item_allowed,
    isItemAllowed,
)
from table_split.enums import TriState
from table_split.ui_components import (
    FSSAI_NON_VEG_SVG,
    FSSAI_VEG_SVG,
    MENU_CARD_CSS,
    render_menu_item_card_html,
)


def _make_fully_safe_base_item(**overrides: Any) -> DietaryItem:
    """
    Constructs a completely verified, allergen-free vegan/jain/halal base dish,
    then applies field overrides to test individual violations or UNKNOWN placements.
    """
    base: dict[str, Any] = {
        "item_id": "BASE_01",
        "dish_name": "Clean Dish",
        "category": "Main Course",
        "price_paise": 20000,
        "is_available": True,
        "verified_by": "Chef QA",
        "verified_at": 1732500000,
        "diet_veg": TriState.YES,
        "diet_non_veg": TriState.NO,
        "diet_egg": TriState.NO,
        "diet_vegan": TriState.YES,
        "diet_jain": TriState.YES,
        "diet_halal": TriState.YES,
        "diet_gluten_free": TriState.YES,
        "contains_onion_garlic": TriState.NO,
        "contains_root_veg": TriState.NO,
        "contains_honey": TriState.NO,
        "allergen_cereals_gluten": TriState.NO,
        "allergen_crustaceans": TriState.NO,
        "allergen_milk": TriState.NO,
        "allergen_eggs": TriState.NO,
        "allergen_fish": TriState.NO,
        "allergen_peanuts": TriState.NO,
        "allergen_tree_nuts": TriState.NO,
        "allergen_soy": TriState.NO,
        "allergen_sulphite": TriState.NO,
        "allergen_sesame": TriState.NO,
        "allergen_mustard": TriState.NO,
        "contains_dairy": TriState.NO,
    }
    base.update(overrides)
    return DietaryItem.model_validate(base)


# ===========================================================================
# 1. Pure Module (No I/O) Verification
# ===========================================================================
def test_dietary_module_has_zero_io_imports() -> None:
    src_path = Path(dietary_mod.__file__)
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    forbidden_modules = {"sqlite3", "os", "io", "pathlib", "urllib", "requests", "httpx", "sqlalchemy"}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint(forbidden_modules), f"Forbidden I/O import(s) found: {imported & forbidden_modules}"


def test_spec_signature_and_dict_access() -> None:
    item = _make_fully_safe_base_item(allergen_peanuts=TriState.UNKNOWN)
    res = isItemAllowed(item, {"allergens": ["peanuts"]})
    assert res["allowed"] is False
    assert "allergen_peanuts" in res["unknownFields"]
    assert res.allowed is False
    assert isinstance(res.reasons, list) and len(res.reasons) >= 1


# ===========================================================================
# 2. Table-Driven Tests (>= 60 Cases)
# ===========================================================================
# Tuple format: (case_name, item_overrides, constraints_dict, expected_allowed, expected_unknown_fields)
DIETARY_TABLE_CASES: list[tuple[str, dict[str, Any], dict[str, Any], bool, list[str]]] = [
    # --- A. Vegan Implication Rules & Every UNKNOWN Placement (17 cases) ---
    ("vegan_fully_verified_clean", {}, {"diets": ["vegan"]}, True, []),
    ("vegan_with_honey_fails", {"contains_honey": TriState.YES}, {"diets": ["vegan"]}, False, []),
    ("vegan_unknown_honey_fails", {"contains_honey": TriState.UNKNOWN}, {"diets": ["vegan"]}, False, ["contains_honey"]),
    ("vegan_with_dairy_fails", {"contains_dairy": TriState.YES}, {"diets": ["vegan"]}, False, []),
    ("vegan_unknown_dairy_fails", {"contains_dairy": TriState.UNKNOWN}, {"diets": ["vegan"]}, False, ["contains_dairy"]),
    ("vegan_with_milk_fails", {"allergen_milk": TriState.YES}, {"diets": ["vegan"]}, False, []),
    ("vegan_unknown_milk_fails", {"allergen_milk": TriState.UNKNOWN}, {"diets": ["vegan"]}, False, ["allergen_milk"]),
    ("vegan_with_diet_egg_fails", {"diet_egg": TriState.YES}, {"diets": ["vegan"]}, False, []),
    ("vegan_unknown_diet_egg_fails", {"diet_egg": TriState.UNKNOWN}, {"diets": ["vegan"]}, False, ["diet_egg"]),
    ("vegan_with_allergen_eggs_fails", {"allergen_eggs": TriState.YES}, {"diets": ["vegan"]}, False, []),
    ("vegan_unknown_allergen_eggs_fails", {"allergen_eggs": TriState.UNKNOWN}, {"diets": ["vegan"]}, False, ["allergen_eggs"]),
    ("vegan_with_diet_veg_no_fails", {"diet_veg": TriState.NO}, {"diets": ["vegan"]}, False, []),
    ("vegan_unknown_diet_veg_fails", {"diet_veg": TriState.UNKNOWN}, {"diets": ["vegan"]}, False, ["diet_veg"]),
    ("vegan_with_non_veg_yes_fails", {"diet_non_veg": TriState.YES}, {"diets": ["vegan"]}, False, []),
    ("vegan_unknown_non_veg_fails", {"diet_non_veg": TriState.UNKNOWN}, {"diets": ["vegan"]}, False, ["diet_non_veg"]),
    ("vegan_flag_no_fails", {"diet_vegan": TriState.NO}, {"diets": ["vegan"]}, False, []),
    ("vegan_flag_unknown_fails", {"diet_vegan": TriState.UNKNOWN}, {"diets": ["vegan"]}, False, ["diet_vegan"]),

    # --- B. Vegetarian & Egg Rule Cases (9 cases) ---
    ("veg_verified_clean", {}, {"diets": ["vegetarian"]}, True, []),
    ("vegetarian_with_egg_excluded_default", {"diet_egg": TriState.YES}, {"diets": ["vegetarian"]}, False, []),
    ("vegetarian_with_egg_allergen_excluded_default", {"allergen_eggs": TriState.YES}, {"diets": ["vegetarian"]}, False, []),
    (
        "vegetarian_with_egg_allowed_when_configured",
        {"diet_egg": TriState.YES, "allergen_eggs": TriState.YES, "diet_vegan": TriState.NO},
        {"diets": ["vegetarian"], "exclude_egg_for_vegetarian": False},
        True,
        [],
    ),
    ("vegetarian_non_veg_yes_fails", {"diet_non_veg": TriState.YES}, {"diets": ["vegetarian"]}, False, []),
    ("vegetarian_non_veg_unknown_fails", {"diet_non_veg": TriState.UNKNOWN}, {"diets": ["vegetarian"]}, False, ["diet_non_veg"]),
    ("vegetarian_veg_unknown_fails", {"diet_veg": TriState.UNKNOWN}, {"diets": ["vegetarian"]}, False, ["diet_veg"]),
    ("vegetarian_egg_unknown_fails", {"diet_egg": TriState.UNKNOWN}, {"diets": ["vegetarian"]}, False, ["diet_egg"]),
    ("vegetarian_allergen_eggs_unknown_fails", {"allergen_eggs": TriState.UNKNOWN}, {"diets": ["vegetarian"]}, False, ["allergen_eggs"]),

    # --- C. Jain Rules (9 cases) ---
    ("jain_verified_clean", {}, {"diets": ["jain"]}, True, []),
    ("jain_with_onion_garlic_fails", {"contains_onion_garlic": TriState.YES}, {"diets": ["jain"]}, False, []),
    ("jain_unknown_onion_garlic_fails", {"contains_onion_garlic": TriState.UNKNOWN}, {"diets": ["jain"]}, False, ["contains_onion_garlic"]),
    ("jain_with_root_veg_fails", {"contains_root_veg": TriState.YES}, {"diets": ["jain"]}, False, []),
    ("jain_unknown_root_veg_fails", {"contains_root_veg": TriState.UNKNOWN}, {"diets": ["jain"]}, False, ["contains_root_veg"]),
    ("jain_flag_no_fails", {"diet_jain": TriState.NO}, {"diets": ["jain"]}, False, []),
    ("jain_flag_unknown_fails", {"diet_jain": TriState.UNKNOWN}, {"diets": ["jain"]}, False, ["diet_jain"]),
    ("jain_non_veg_yes_fails", {"diet_non_veg": TriState.YES}, {"diets": ["jain"]}, False, []),
    ("jain_veg_unknown_fails", {"diet_veg": TriState.UNKNOWN}, {"diets": ["jain"]}, False, ["diet_veg"]),

    # --- D. Halal Rules (3 cases) ---
    ("halal_verified_yes_passes", {}, {"diets": ["halal"]}, True, []),
    ("halal_no_fails", {"diet_halal": TriState.NO}, {"diets": ["halal"]}, False, []),
    ("halal_unknown_fails", {"diet_halal": TriState.UNKNOWN}, {"diets": ["halal"]}, False, ["diet_halal"]),

    # --- E. Gluten-Free Diet Rules (4 cases) ---
    ("gluten_free_verified_clean", {}, {"diets": ["gluten_free"]}, True, []),
    ("gluten_free_with_gluten_yes_fails", {"allergen_cereals_gluten": TriState.YES}, {"diets": ["gluten_free"]}, False, []),
    (
        "gluten_free_with_unknown_gluten_fails",
        {"allergen_cereals_gluten": TriState.UNKNOWN},
        {"diets": ["gluten_free"]},
        False,
        ["allergen_cereals_gluten"],
    ),
    ("gluten_free_diet_flag_no_fails", {"diet_gluten_free": TriState.NO}, {"diets": ["gluten_free"]}, False, []),

    # --- F. Multiple Simultaneous Constraints & Unverified Item Gate (6 cases) ---
    ("multi_vegan_jain_halal_gf_clean", {}, {"diets": ["vegan", "jain", "halal", "gluten_free"]}, True, []),
    (
        "multi_vegan_plus_jain_fails_onion",
        {"contains_onion_garlic": TriState.YES, "diet_jain": TriState.NO},
        {"diets": ["vegan", "jain"]},
        False,
        [],
    ),
    (
        "multi_halal_plus_milk_soy_unknown_soy",
        {"allergen_soy": TriState.UNKNOWN},
        {"diets": ["halal"], "allergens": ["milk", "soy"]},
        False,
        ["allergen_soy"],
    ),
    (
        "multi_vegan_plus_peanuts_and_sesame_fails_sesame",
        {"allergen_sesame": TriState.YES},
        {"diets": ["vegan"], "allergens": ["peanuts", "sesame"]},
        False,
        [],
    ),
    (
        "unverified_item_excluded_when_constrained",
        {"verified_by": None, "verified_at": None},
        {"diets": ["vegan"]},
        False,
        [],
    ),
    (
        "unverified_item_allowed_when_unconstrained",
        {"verified_by": None, "verified_at": None, "allergen_peanuts": TriState.UNKNOWN},
        {"diets": [], "allergens": []},
        True,
        [],
    ),
]

# --- G. Systematic Allergen x 3 Tri-State Cases (12 allergens x 3 = 36 cases) ---
_ALLERGEN_MATRIX: list[tuple[str, str]] = [
    ("cereals_gluten", "allergen_cereals_gluten"),
    ("crustaceans", "allergen_crustaceans"),
    ("milk", "allergen_milk"),
    ("dairy", "contains_dairy"),
    ("eggs", "allergen_eggs"),
    ("fish", "allergen_fish"),
    ("peanuts", "allergen_peanuts"),
    ("tree_nuts", "allergen_tree_nuts"),
    ("soy", "allergen_soy"),
    ("sulphite", "allergen_sulphite"),
    ("sesame", "allergen_sesame"),
    ("mustard", "allergen_mustard"),
]

for allergen_key, field_name in _ALLERGEN_MATRIX:
    DIETARY_TABLE_CASES.append((
        f"allergen_{allergen_key}_present_fails",
        {field_name: TriState.YES},
        {"allergens": [allergen_key]},
        False,
        [],
    ))
    DIETARY_TABLE_CASES.append((
        f"allergen_{allergen_key}_absent_passes",
        {field_name: TriState.NO},
        {"allergens": [allergen_key]},
        True,
        [],
    ))
    DIETARY_TABLE_CASES.append((
        f"allergen_{allergen_key}_unknown_fails",
        {field_name: TriState.UNKNOWN},
        {"allergens": [allergen_key]},
        False,
        [field_name],
    ))


def test_minimum_60_table_cases() -> None:
    assert len(DIETARY_TABLE_CASES) >= 60, f"Expected >= 60 table cases, got {len(DIETARY_TABLE_CASES)}"


@pytest.mark.parametrize(
    "case_name,overrides,constraints_dict,expected_allowed,expected_unknowns",
    DIETARY_TABLE_CASES,
)
def test_dietary_table_driven(
    case_name: str,
    overrides: dict[str, Any],
    constraints_dict: dict[str, Any],
    expected_allowed: bool,
    expected_unknowns: list[str],
) -> None:
    item = _make_fully_safe_base_item(**overrides)
    result = isItemAllowed(item, constraints_dict)
    assert result.allowed == expected_allowed, (
        f"[{case_name}] expected allowed={expected_allowed}, got {result.allowed} (reasons={result.reasons})"
    )
    for unk in expected_unknowns:
        assert unk in result.unknownFields, (
            f"[{case_name}] expected '{unk}' in unknownFields, got {result.unknownFields}"
        )


# ===========================================================================
# 3. Monotonicity Property Test
# ===========================================================================
def test_monotonicity_property_exhaustive_and_fuzzed() -> None:
    """
    Property: Adding any dietary or allergen constraint NEVER increases the set of allowed items.
    If C1 <= C2 then allowed(C2) <= allowed(C1).
    """
    # Construct diverse catalog including verified, partially UNKNOWN, honey, egg, onion, and unverified items
    rng = random.Random(42)
    catalog: list[DietaryItem] = [
        _make_fully_safe_base_item(item_id="PURE_CLEAN"),
        _make_fully_safe_base_item(item_id="VEGAN_HONEY", contains_honey=TriState.YES),
        _make_fully_safe_base_item(item_id="VEG_PANEER", diet_vegan=TriState.NO, contains_dairy=TriState.YES, allergen_milk=TriState.YES),
        _make_fully_safe_base_item(item_id="EGG_CURRY", diet_veg=TriState.NO, diet_non_veg=TriState.YES, diet_egg=TriState.YES, allergen_eggs=TriState.YES, diet_vegan=TriState.NO, diet_jain=TriState.NO),
        _make_fully_safe_base_item(item_id="ONION_DAL", diet_jain=TriState.NO, contains_onion_garlic=TriState.YES),
        _make_fully_safe_base_item(item_id="GF_UNKNOWN", allergen_cereals_gluten=TriState.UNKNOWN),
        _make_fully_safe_base_item(item_id="HALAL_UNKNOWN", diet_halal=TriState.UNKNOWN),
        _make_fully_safe_base_item(item_id="UNVERIFIED", verified_by=None, verified_at=None),
    ]
    # Add 25 randomized items across valid & partially unknown combinations
    tristates = [TriState.YES, TriState.NO, TriState.UNKNOWN]
    for i in range(25):
        rand_fields: dict[str, Any] = {f: rng.choice(tristates) for f in ALL_TRISTATE_FIELDS}
        verified = rng.choice([True, False])
        catalog.append(
            DietaryItem(
                item_id=f"RAND_{i}",
                verified_by="Staff" if verified else None,
                verified_at=1732500000 if verified else None,
                **rand_fields,
            )
        )

    all_diets = ["vegetarian", "vegan", "jain", "halal", "gluten_free"]
    all_allergens = ["cereals_gluten", "crustaceans", "milk", "dairy", "eggs", "fish", "peanuts", "tree_nuts", "soy", "sulphite", "sesame", "mustard"]

    def allowed_set(c: HardDietaryConstraints) -> set[str]:
        return {it.item_id for it in catalog if is_item_allowed(it, c).allowed}

    # Test 100 random monotonic expansions C1 -> C2
    for _ in range(100):
        d1 = set(rng.sample(all_diets, k=rng.randint(0, 3)))
        a1 = set(rng.sample(all_allergens, k=rng.randint(0, 4)))
        c1 = HardDietaryConstraints(diets=d1, allergens=a1, exclude_egg_for_vegetarian=rng.choice([False, True]))

        # C2 strictly expands C1 (superset of diets & allergens, and egg exclusion at least as strict)
        d2 = d1 | set(rng.sample(all_diets, k=rng.randint(0, 2)))
        a2 = a1 | set(rng.sample(all_allergens, k=rng.randint(0, 3)))
        egg_strict2 = True if c1.exclude_egg_for_vegetarian else rng.choice([False, True])
        c2 = HardDietaryConstraints(diets=d2, allergens=a2, exclude_egg_for_vegetarian=egg_strict2)

        s1 = allowed_set(c1)
        s2 = allowed_set(c2)
        assert s2.issubset(s1), f"Monotonicity violated! Adding constraints added items {s2 - s1}"
        assert len(s2) <= len(s1)


# ===========================================================================
# 4. Migration 002 & DB Constraint Tests
# ===========================================================================
def test_migration_002_up_and_down_reversibility(tmp_path: Path) -> None:
    db_path = tmp_path / "stage3_mig.db"
    applied = migrate_up(db_path)
    assert applied == ["001", "002"]

    conn = get_connection(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(menu_item_extensions);").fetchall()}
    assert {"contains_honey", "allergen_mustard", "sales_volume"}.issubset(cols)

    # Verify CHECK constraints on new columns
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO menu_item_extensions (item_id, price_paise, contains_honey) VALUES ('T1', 1000, 'INVALID');"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO menu_item_extensions (item_id, price_paise, sales_volume) VALUES ('T2', 1000, -1);"
        )

    # Insert valid row and then test downgrade of 002 only
    conn.execute(
        "INSERT INTO menu_item_extensions (item_id, price_paise, contains_honey, sales_volume) VALUES ('T3', 5000, 'NO', 42);"
    )
    conn.commit()
    conn.close()

    rolled_back = migrate_down(db_path, steps=1)
    assert rolled_back == ["002"]

    conn2 = get_connection(db_path)
    cols_after = {r[1] for r in conn2.execute("PRAGMA table_info(menu_item_extensions);").fetchall()}
    assert "contains_honey" not in cols_after
    assert "sales_volume" not in cols_after
    # Existing data in T3 survived clean rebuild!
    row = conn2.execute("SELECT item_id, price_paise FROM menu_item_extensions WHERE item_id='T3';").fetchone()
    assert row is not None and row["price_paise"] == 5000
    conn2.close()


# ===========================================================================
# 5. CLI Verification, Repository & Report Endpoint Tests
# ===========================================================================
def _create_temp_menu_json(tmp_path: Path) -> Path:
    menu_file = tmp_path / "menu.json"
    menu_file.write_text(
        json.dumps({
            "menu": [
                {"id": "M01", "dish_name": "Top Seller Biryani", "category": "Mains", "price": 300, "sales_volume": 500},
                {"id": "M02", "dish_name": "Low Seller Salad", "category": "Starters", "price": 150, "sales_volume": 10},
                {"id": "M03", "dish_name": "Mid Seller Curry", "category": "Mains", "price": 220, "sales_volume": 120},
            ]
        }),
        encoding="utf-8",
    )
    return menu_file


def test_cli_verification_and_report_endpoint(tmp_path: Path) -> None:
    db_file = tmp_path / "dietary_test.db"
    menu_file = _create_temp_menu_json(tmp_path)

    # 1. Verify M02 completely using CLI flags
    cli_args = [
        "--db-path", str(db_file),
        "--menu-path", str(menu_file),
        "--item", "M02",
        "--verified-by", "Chef Priya",
        "--verified-at", "1732550000",
    ]
    for field_name in ALL_TRISTATE_FIELDS:
        val = "YES" if field_name in ("diet_veg", "diet_vegan", "diet_jain", "diet_halal", "diet_gluten_free") else "NO"
        cli_args.extend(["--set", f"{field_name}={val}"])

    rc = cli_main(cli_args)
    assert rc == 0

    # 2. Wire FastAPI endpoint to test DB + catalog via dependency override
    test_repo = DietaryRepository(db_path=db_file, menu_path=menu_file)
    app.dependency_overrides[get_dietary_repository] = lambda: test_repo
    try:
        client = TestClient(app)

        # Hit Data Quality Report endpoint
        res = client.get("/table-split/dietary/report")
        assert res.status_code == 200
        report = res.json()

        assert report["total_items"] == 3
        assert report["fully_verified_count"] == 1  # M02 verified cleanly
        assert report["fully_verified_percentage"] == 33.33
        assert report["unverified_or_incomplete_count"] == 2

        # Blocking items MUST be ordered by sales_volume DESC: M01 (500) before M03 (120)
        blocking = report["blocking_items"]
        assert [b["item_id"] for b in blocking] == ["M01", "M03"]
        assert blocking[0]["sales_volume"] == 500
        assert blocking[1]["sales_volume"] == 120

        # Verify M03 via PUT REST endpoint
        full_updates = {
            f: ("YES" if f in ("diet_veg", "diet_halal") else "NO")
            for f in ALL_TRISTATE_FIELDS
        }
        put_res = client.put(
            "/table-split/dietary/items/M03/verify",
            json={"verified_by": "Chef Ravi", "verified_at": 1732550100, "updates": full_updates},
        )
        assert put_res.status_code == 200
        assert put_res.json()["item"]["is_fully_verified"] is True

        # Report should now show 2/3 (66.67%) verified and M01 alone blocking
        res2 = client.get("/table-split/dietary/report")
        report2 = res2.json()
        assert report2["fully_verified_count"] == 2
        assert report2["fully_verified_percentage"] == 66.67
        assert [b["item_id"] for b in report2["blocking_items"]] == ["M01"]
    finally:
        app.dependency_overrides.clear()


def test_interactive_cli_review(tmp_path: Path) -> None:
    db_file = tmp_path / "cli_interactive.db"
    menu_file = _create_temp_menu_json(tmp_path)
    repo = DietaryRepository(db_path=db_file, menu_path=menu_file)

    answers = iter([
        "YES",  # diet_veg
        "NO",   # diet_non_veg
        "NO",   # diet_egg
        "NO",   # diet_vegan
        "NO",   # diet_jain
        "YES",  # diet_halal
        "YES",  # diet_gluten_free
        "YES",  # contains_onion_garlic
        "YES",  # contains_root_veg
        "NO",   # contains_honey
        "NO",   # allergen_cereals_gluten
        "NO",   # allergen_crustaceans
        "NO",   # allergen_milk
        "NO",   # allergen_eggs
        "NO",   # allergen_fish
        "NO",   # allergen_peanuts
        "NO",   # allergen_tree_nuts
        "NO",   # allergen_soy
        "NO",   # allergen_sulphite
        "NO",   # allergen_sesame
        "NO",   # allergen_mustard
        "NO",   # contains_dairy
        "Shared fryer warning",  # cross_contact_note
        "y",    # confirm sign off
    ])
    logs: list[str] = []
    saved_count = interactive_review(
        repo,
        reviewer="Chef Anita",
        input_fn=lambda _: next(answers),
        output_fn=logs.append,
        item_id_filter="M01",
    )
    assert saved_count == 1
    m01 = repo.get_item("M01")
    assert m01 is not None
    assert m01.is_fully_verified is True
    assert m01.verified_by == "Chef Anita"
    assert m01.cross_contact_note == "Shared fryer warning"


# ===========================================================================
# 6. Proposal Exclusion & FSSAI Customer Menu Card Tests
# ===========================================================================
def test_proposal_unverified_exclusion_rule() -> None:
    verified_vegan = _make_fully_safe_base_item(item_id="VER_VEGAN", dish_name="Verified Bowl")
    unverified_draft = _make_fully_safe_base_item(
        item_id="UNVER_DRAFT",
        dish_name="Unverified Draft Bowl",
        verified_by=None,
        verified_at=None,
    )

    # 1. When table has NO hard constraints, unverified dish IS included
    allowed_free, _ = filter_items_for_proposal(
        [verified_vegan, unverified_draft],
        [{"diets": [], "allergens": []}],
    )
    assert {it.item_id for it in allowed_free} == {"VER_VEGAN", "UNVER_DRAFT"}

    # 2. When ANY diner activates a hard constraint (even unrelated allergen), unverified dish MUST be excluded
    allowed_constrained, rejections = filter_items_for_proposal(
        [verified_vegan, unverified_draft],
        [
            {"diets": [], "allergens": []},
            {"allergens": ["peanuts"]},
        ],
    )
    assert [it.item_id for it in allowed_constrained] == ["VER_VEGAN"]
    assert "UNVER_DRAFT" in rejections
    assert any("Dietary info not verified" in r for r in rejections["UNVER_DRAFT"].reasons)


def test_fssai_symbols_egg_rule_and_unverified_card_rendering() -> None:
    # 1. Verified veg (egg=NO, non_veg=NO) -> Green circle in green square
    veg_item = _make_fully_safe_base_item(item_id="VEG_1", dish_name="Dal Tadka", allergen_tree_nuts=TriState.YES)
    assert classify_fssai_symbol(veg_item) == "veg"
    veg_html = render_menu_item_card_html(veg_item)
    assert FSSAI_VEG_SVG in veg_html
    assert "Tree nuts" in veg_html
    assert "Dietary info not verified" not in veg_html
    assert all(not ln.startswith("    ") for ln in veg_html.splitlines()), "HTML card must have no 4-space CommonMark indentation"
    assert all(not ln.startswith("    ") for ln in MENU_CARD_CSS.splitlines()), "CSS must have no 4-space CommonMark indentation"

    # 2. Egg-containing item (even if diet_veg was wrongly set or UNKNOWN) -> Brown triangle in brown square!
    egg_item = _make_fully_safe_base_item(
        item_id="EGG_1",
        dish_name="Egg Fried Rice",
        diet_egg=TriState.YES,
        allergen_eggs=TriState.YES,
        diet_vegan=TriState.NO,
        diet_jain=TriState.NO,
    )
    assert classify_fssai_symbol(egg_item) == "non_veg"
    egg_html = render_menu_item_card_html(egg_item)
    assert FSSAI_NON_VEG_SVG in egg_html
    assert FSSAI_VEG_SVG not in egg_html
    assert "Egg rule" in egg_html

    # 3. Unverified item -> Displays "Dietary info not verified", never invents a symbol or shows veg green dot
    unverified_item = DietaryItem(
        item_id="UNVER_1",
        dish_name="Mystery Curry",
        category="Mains",
        price_paise=25000,
    )
    assert classify_fssai_symbol(unverified_item) == "unverified"
    unver_html = render_menu_item_card_html(unverified_item)
    assert "Dietary info not verified" in unver_html
    assert FSSAI_VEG_SVG not in unver_html
    assert FSSAI_NON_VEG_SVG not in unver_html

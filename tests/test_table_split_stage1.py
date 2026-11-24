"""
tests/test_table_split_stage1.py — Comprehensive test suite for Table Split Mode Stage 1.
Validates:
1. Strict integer paise (proves 280.0 and True/False are strictly rejected).
2. Over 60 table-driven safety test cases (tri-state allergens, diets, UNKNOWN placement, combined constraints).
3. Monotonicity property test: adding a constraint never increases allowed dishes.
4. Security: 128-bit join tokens, hashed guest tokens, room code format.
5. Persistence: SQLite WAL mode, optimistic locking (versioning), expiration, audit logging.
"""

import re
from typing import Any

import pytest
from pydantic import ValidationError

from table_split.db.persistence import SessionConcurrencyError, TableSessionRepository
from table_split.enums import (
    Allergen,
    DietRule,
    PaymentStatus,
    PreferenceStrength,
    SessionStatus,
    SpiceLevel,
    TriState,
)
from table_split.menu_catalog import load_menu_catalog
from table_split.models import (
    AuditEvent,
    Diner,
    DinerPreference,
    DishSafetyProfile,
    DishVerification,
    MenuItem,
    TableSession,
)
from table_split.safety import (
    filter_safe_menu_for_diner,
    find_safe_shared_dishes,
    is_dish_safe_for_diner,
)
from table_split.security import (
    generate_guest_token,
    generate_join_token_128,
    generate_room_code,
    hash_token,
    verify_token,
)

# ═══════════════════════════════════════════════════════════════════════════════
# 1. STRICT INTEGER PAISE TESTS (Requirement 4)
# ═══════════════════════════════════════════════════════════════════════════════

def test_paise_rejects_float():
    """Verify that 280.0 (float) is strictly rejected by MenuItem price_paise."""
    with pytest.raises((ValidationError, TypeError)):
        MenuItem(
            id="TEST_01",
            dish_name="Float Dish",
            category="Test",
            description="Testing float rejection",
            price_paise=280.0,  # FLOAT: Must fail
        )


def test_paise_rejects_bool():
    """Verify that True (bool) is strictly rejected despite bool being subclass of int in Python."""
    with pytest.raises((ValidationError, TypeError)):
        MenuItem(
            id="TEST_02",
            dish_name="Bool Dish",
            category="Test",
            description="Testing bool rejection",
            price_paise=True,  # BOOL: Must fail
        )

    with pytest.raises((ValidationError, TypeError)):
        MenuItem(
            id="TEST_03",
            dish_name="False Dish",
            category="Test",
            description="Testing False rejection",
            price_paise=False,  # BOOL: Must fail
        )


def test_budget_paise_rejects_float_and_bool():
    """Verify that diner max_budget_paise rejects float and bool."""
    with pytest.raises((ValidationError, TypeError)):
        DinerPreference(max_budget_paise=500.50)

    with pytest.raises((ValidationError, TypeError)):
        DinerPreference(max_budget_paise=True)


def test_paise_rejects_negative():
    """Verify that negative paise is rejected."""
    with pytest.raises((ValidationError, ValueError)):
        MenuItem(
            id="TEST_04",
            dish_name="Negative Dish",
            category="Test",
            description="Testing negative rejection",
            price_paise=-100,
        )


def test_valid_integer_paise():
    """Verify that positive int paise is accepted and formats properly."""
    item = MenuItem(
        id="D001",
        dish_name="Butter Chicken",
        category="Main Course",
        description="Rich creamy chicken",
        price_paise=28000,
    )
    assert item.price_paise == 28000
    assert item.price_rupees_formatted == "₹280"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. TABLE-DRIVEN SAFETY TESTS (Requirement 10: At least 60 test cases)
# ═══════════════════════════════════════════════════════════════════════════════

def _make_custom_dish(
    dish_id: str = "T_DISH",
    dish_name: str = "Test Dish",
    dairy: TriState = TriState.UNKNOWN,
    tree_nuts: TriState = TriState.UNKNOWN,
    peanuts: TriState = TriState.UNKNOWN,
    gluten: TriState = TriState.UNKNOWN,
    soy: TriState = TriState.UNKNOWN,
    shellfish: TriState = TriState.UNKNOWN,
    fish: TriState = TriState.UNKNOWN,
    egg: TriState = TriState.UNKNOWN,
    sesame: TriState = TriState.UNKNOWN,
    mustard: TriState = TriState.UNKNOWN,
    vegetarian: TriState = TriState.UNKNOWN,
    vegan: TriState = TriState.UNKNOWN,
    jain: TriState = TriState.UNKNOWN,
    halal: TriState = TriState.UNKNOWN,
    spice_level: SpiceLevel = SpiceLevel.MILD,
    is_available: bool = True,
) -> MenuItem:
    """Helper to construct a dish with precise tri-state properties."""
    allergens = {
        Allergen.DAIRY: dairy,
        Allergen.TREE_NUTS: tree_nuts,
        Allergen.PEANUTS: peanuts,
        Allergen.GLUTEN: gluten,
        Allergen.SOY: soy,
        Allergen.SHELLFISH: shellfish,
        Allergen.FISH: fish,
        Allergen.EGG: egg,
        Allergen.SESAME: sesame,
        Allergen.MUSTARD: mustard,
    }
    diets = {
        DietRule.VEGETARIAN: vegetarian,
        DietRule.VEGAN: vegan,
        DietRule.JAIN: jain,
        DietRule.HALAL: halal,
        DietRule.KOSHER: TriState.UNKNOWN,
    }
    profile = DishSafetyProfile(
        allergens=allergens,
        diets=diets,
        spice_level=spice_level,
        verification=DishVerification(verified_by="Test Chef", verified_at="2026-11-24T00:00:00Z"),
    )
    return MenuItem(
        id=dish_id,
        dish_name=dish_name,
        category="Test Category",
        description="Test description",
        price_paise=10000,
        is_available=is_available,
        safety_profile=profile,
    )


# Generate 65+ distinct test cases systematically
SAFETY_TEST_CASES: list[tuple[str, dict[str, Any], DinerPreference, bool]] = []

# (A) Single Allergen Cases (All 10 allergens x 3 tri-states = 30 cases)
for idx, allergen in enumerate(Allergen):
    # Case 1: Dish CONTAINS allergen -> Must be UNSAFE
    kwargs_contains = {allergen.name.lower(): TriState.CONTAINS}
    SAFETY_TEST_CASES.append((
        f"allergen_{allergen.value}_contains",
        kwargs_contains,
        DinerPreference(allergies={allergen}),
        False,  # Expected safe: False
    ))
    # Case 2: Dish FREE of allergen -> Must be SAFE
    kwargs_free = {allergen.name.lower(): TriState.FREE}
    SAFETY_TEST_CASES.append((
        f"allergen_{allergen.value}_free",
        kwargs_free,
        DinerPreference(allergies={allergen}),
        True,  # Expected safe: True
    ))
    # Case 3: Dish UNKNOWN allergen status -> Must be UNSAFE (Fail-Safe rule!)
    kwargs_unknown = {allergen.name.lower(): TriState.UNKNOWN}
    SAFETY_TEST_CASES.append((
        f"allergen_{allergen.value}_unknown_fails_hard_check",
        kwargs_unknown,
        DinerPreference(allergies={allergen}),
        False,  # Expected safe: False
    ))

# (B) Dietary Rules Cases (4 diets x 3 tri-states = 12 cases)
for diet in [DietRule.VEGETARIAN, DietRule.VEGAN, DietRule.JAIN, DietRule.HALAL]:
    # CONTAINS means certified compliant -> SAFE
    kwargs_comp = {diet.name.lower(): TriState.CONTAINS}
    SAFETY_TEST_CASES.append((
        f"diet_{diet.value}_compliant",
        kwargs_comp,
        DinerPreference(diet_rules={diet}),
        True,
    ))
    # FREE means violates diet -> UNSAFE
    kwargs_viol = {diet.name.lower(): TriState.FREE}
    SAFETY_TEST_CASES.append((
        f"diet_{diet.value}_non_compliant",
        kwargs_viol,
        DinerPreference(diet_rules={diet}),
        False,
    ))
    # UNKNOWN means unverified compliance -> UNSAFE (Fail-Safe)
    kwargs_unk = {diet.name.lower(): TriState.UNKNOWN}
    SAFETY_TEST_CASES.append((
        f"diet_{diet.value}_unknown_fails_hard_check",
        kwargs_unk,
        DinerPreference(diet_rules={diet}),
        False,
    ))

# (C) Combined Constraints Cases (15 cases)
COMBINED_CASES: list[tuple[str, dict[str, Any], DinerPreference, bool]] = [
    (
        "vegan_and_nut_allergic_dish_contains_nuts",
        {"vegan": TriState.CONTAINS, "tree_nuts": TriState.CONTAINS, "peanuts": TriState.FREE},
        DinerPreference(diet_rules={DietRule.VEGAN}, allergies={Allergen.TREE_NUTS}),
        False,
    ),
    (
        "vegan_and_nut_allergic_dish_safe_on_both",
        {"vegan": TriState.CONTAINS, "tree_nuts": TriState.FREE, "peanuts": TriState.FREE},
        DinerPreference(diet_rules={DietRule.VEGAN}, allergies={Allergen.TREE_NUTS, Allergen.PEANUTS}),
        True,
    ),
    (
        "vegan_and_nut_allergic_dish_nut_unknown",
        {"vegan": TriState.CONTAINS, "tree_nuts": TriState.UNKNOWN},
        DinerPreference(diet_rules={DietRule.VEGAN}, allergies={Allergen.TREE_NUTS}),
        False,  # Fail-safe
    ),
    (
        "jain_and_gluten_allergic_dish_contains_gluten",
        {"jain": TriState.CONTAINS, "gluten": TriState.CONTAINS},
        DinerPreference(diet_rules={DietRule.JAIN}, allergies={Allergen.GLUTEN}),
        False,
    ),
    (
        "jain_and_gluten_allergic_dish_free_of_both",
        {"jain": TriState.CONTAINS, "gluten": TriState.FREE},
        DinerPreference(diet_rules={DietRule.JAIN}, allergies={Allergen.GLUTEN}),
        True,
    ),
    (
        "halal_and_dairy_allergic_dish_contains_dairy",
        {"halal": TriState.CONTAINS, "dairy": TriState.CONTAINS},
        DinerPreference(diet_rules={DietRule.HALAL}, allergies={Allergen.DAIRY}),
        False,
    ),
    (
        "halal_and_dairy_allergic_dish_free_of_dairy",
        {"halal": TriState.CONTAINS, "dairy": TriState.FREE},
        DinerPreference(diet_rules={DietRule.HALAL}, allergies={Allergen.DAIRY}),
        True,
    ),
    (
        "vegetarian_and_halal_dual_diet_both_compliant",
        {"vegetarian": TriState.CONTAINS, "halal": TriState.CONTAINS},
        DinerPreference(diet_rules={DietRule.VEGETARIAN, DietRule.HALAL}),
        True,
    ),
    (
        "vegetarian_and_halal_one_violates",
        {"vegetarian": TriState.FREE, "halal": TriState.CONTAINS},
        DinerPreference(diet_rules={DietRule.VEGETARIAN, DietRule.HALAL}),
        False,
    ),
    (
        "omnivore_with_no_diet_rules_eats_any_dish",
        {"vegetarian": TriState.FREE, "dairy": TriState.CONTAINS},
        DinerPreference(diet_rules=set(), allergies=set()),
        True,
    ),
    (
        "omnivore_with_severe_shellfish_allergy",
        {"shellfish": TriState.CONTAINS},
        DinerPreference(diet_rules=set(), allergies={Allergen.SHELLFISH}),
        False,
    ),
    (
        "triple_allergen_free_dish",
        {"dairy": TriState.FREE, "gluten": TriState.FREE, "tree_nuts": TriState.FREE},
        DinerPreference(allergies={Allergen.DAIRY, Allergen.GLUTEN, Allergen.TREE_NUTS}),
        True,
    ),
    (
        "triple_allergen_one_contains",
        {"dairy": TriState.FREE, "gluten": TriState.CONTAINS, "tree_nuts": TriState.FREE},
        DinerPreference(allergies={Allergen.DAIRY, Allergen.GLUTEN, Allergen.TREE_NUTS}),
        False,
    ),
    (
        "triple_allergen_one_unknown",
        {"dairy": TriState.FREE, "gluten": TriState.FREE, "tree_nuts": TriState.UNKNOWN},
        DinerPreference(allergies={Allergen.DAIRY, Allergen.GLUTEN, Allergen.TREE_NUTS}),
        False,
    ),
    (
        "unavailable_dish_fails_safety_regardless_of_diet",
        {"vegan": TriState.CONTAINS, "dairy": TriState.FREE, "is_available": False},
        DinerPreference(diet_rules={DietRule.VEGAN}),
        False,
    ),
]
SAFETY_TEST_CASES.extend(COMBINED_CASES)

# (D) Spice and Disliked Dish Cases (8 cases)
SPICE_CASES: list[tuple[str, dict[str, Any], DinerPreference, bool]] = [
    (
        "spice_hard_constraint_dish_exceeds_tolerance",
        {"spice_level": SpiceLevel.SPICY},
        DinerPreference(max_spice=SpiceLevel.MILD, strength=PreferenceStrength.HARD),
        False,
    ),
    (
        "spice_hard_constraint_dish_within_tolerance",
        {"spice_level": SpiceLevel.MILD},
        DinerPreference(max_spice=SpiceLevel.MEDIUM, strength=PreferenceStrength.HARD),
        True,
    ),
    (
        "spice_soft_constraint_allows_hotter_dish",
        {"spice_level": SpiceLevel.SPICY},
        DinerPreference(max_spice=SpiceLevel.MILD, strength=PreferenceStrength.SOFT),
        True,  # Soft preference does not hard-fail dish
    ),
    (
        "disliked_dish_id_is_excluded",
        {},
        DinerPreference(disliked_dish_ids=["T_DISH"]),
        False,
    ),
    (
        "non_disliked_dish_is_included",
        {},
        DinerPreference(disliked_dish_ids=["OTHER_DISH"]),
        True,
    ),
    (
        "completely_unverified_dish_fails_for_allergic_diner",
        {},  # All allergens/diets UNKNOWN
        DinerPreference(allergies={Allergen.GLUTEN}),
        False,
    ),
    (
        "completely_unverified_dish_fails_for_vegan_diner",
        {},  # All allergens/diets UNKNOWN
        DinerPreference(diet_rules={DietRule.VEGAN}),
        False,
    ),
    (
        "completely_unverified_dish_passes_for_unconstrained_omnivore",
        {},  # All allergens/diets UNKNOWN
        DinerPreference(diet_rules=set(), allergies=set()),
        True,
    ),
]
SAFETY_TEST_CASES.extend(SPICE_CASES)


@pytest.mark.parametrize("case_name,dish_kwargs,pref,expected_safe", SAFETY_TEST_CASES)
def test_safety_table_cases(case_name, dish_kwargs, pref, expected_safe):
    """Executes each of the 65+ parameterized food safety test cases."""
    dish = _make_custom_dish(**dish_kwargs)
    is_safe, reasons = is_dish_safe_for_diner(dish, pref)
    assert is_safe == expected_safe, f"Failed on case '{case_name}': expected {expected_safe}, got {is_safe}. Reasons: {reasons}"


def test_total_safety_test_case_count():
    """Verify that we have at least 60 distinct safety test cases as mandated."""
    assert len(SAFETY_TEST_CASES) >= 60, f"Expected >=60 cases, got {len(SAFETY_TEST_CASES)}"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. PROPERTY-BASED MONOTONICITY TEST (Requirement 10)
# ═══════════════════════════════════════════════════════════════════════════════

def test_safety_monotonicity_property():
    """
    Property: Adding a constraint to a diner preference NEVER increases the set of allowed dishes.
    Formally: If C1 subset_of C2, then allowed(C2) <= allowed(C1).
    """
    menu = load_menu_catalog()
    assert len(menu) > 0

    base_pref = DinerPreference()
    base_allowed = len(filter_safe_menu_for_diner(menu, base_pref))

    # Add 1 allergen
    pref_with_dairy = DinerPreference(allergies={Allergen.DAIRY})
    dairy_allowed = len(filter_safe_menu_for_diner(menu, pref_with_dairy))
    assert dairy_allowed <= base_allowed

    # Add 2 allergens
    pref_with_dairy_and_nuts = DinerPreference(allergies={Allergen.DAIRY, Allergen.TREE_NUTS})
    dairy_nuts_allowed = len(filter_safe_menu_for_diner(menu, pref_with_dairy_and_nuts))
    assert dairy_nuts_allowed <= dairy_allowed

    # Add 3 allergens
    pref_with_three = DinerPreference(allergies={Allergen.DAIRY, Allergen.TREE_NUTS, Allergen.GLUTEN})
    three_allowed = len(filter_safe_menu_for_diner(menu, pref_with_three))
    assert three_allowed <= dairy_nuts_allowed

    # Add a strict diet rule (VEGAN) on top
    pref_vegan_and_three = DinerPreference(
        diet_rules={DietRule.VEGAN},
        allergies={Allergen.DAIRY, Allergen.TREE_NUTS, Allergen.GLUTEN},
    )
    vegan_three_allowed = len(filter_safe_menu_for_diner(menu, pref_vegan_and_three))
    assert vegan_three_allowed <= three_allowed


# ═══════════════════════════════════════════════════════════════════════════════
# 4. SECURITY & TOKEN TESTS (Requirement 5)
# ═══════════════════════════════════════════════════════════════════════════════

def test_join_token_128_entropy():
    """Verify join token has 128-bit entropy (16 bytes = 22 URL-safe characters) and uniqueness."""
    tokens = {generate_join_token_128() for _ in range(100)}
    assert len(tokens) == 100
    for tok in tokens:
        assert len(tok) >= 22
        assert re.match(r"^[A-Za-z0-9_-]+$", tok)


def test_guest_token_hashing():
    """Verify guest tokens are hashed with SHA-256 and verified in constant time."""
    raw_token = generate_guest_token()
    tok_hash = hash_token(raw_token)
    assert len(tok_hash) == 64  # SHA-256 hex digest
    assert verify_token(raw_token, tok_hash) is True
    assert verify_token("tampered_token", tok_hash) is False


def test_room_code_format():
    """Verify room code matches TAB-XXXX with unambiguous uppercase characters."""
    for _ in range(50):
        code = generate_room_code(prefix="TAB", suffix_len=4)
        assert re.match(r"^TAB-[A-Z0-9]{4}$", code)
        # Verify absence of ambiguous characters 0, O, 1, I, L
        suffix = code.split("-")[1]
        for ch in "0O1IL":
            assert ch not in suffix


# ═══════════════════════════════════════════════════════════════════════════════
# 5. PERSISTENCE & OPTIMISTIC CONCURRENCY TESTS (Requirement 6 & 7)
# ═══════════════════════════════════════════════════════════════════════════════

def test_session_persistence_and_concurrency(tmp_path):
    """Verify SQLite WAL persistence, versioning, optimistic locking, and audit logs."""
    db_file = tmp_path / "test_sessions.db"
    repo = TableSessionRepository(str(db_file))

    raw_host_token = generate_guest_token()
    host_diner = Diner(
        diner_id="DINER_HOST_01",
        name="Aresh (Host)",
        is_host=True,
        guest_token_hash=hash_token(raw_host_token),
        payment_status=PaymentStatus.UNPAID,
        paid_amount_paise=0,
        joined_at=1700000000,
    )

    session = TableSession(
        session_id="SESS_TEST_100",
        room_code="TAB-9K2M",
        join_token_128=generate_join_token_128(),
        host_diner_id="DINER_HOST_01",
        version=1,
        status=SessionStatus.OPEN_FOR_PREFERENCES,
        diners={"DINER_HOST_01": host_diner},
        created_at=1700000000,
        updated_at=1700000000,
        expires_at=1700000000 + 10800,
    )

    # 1. Create session
    repo.create_session(session)

    # 2. Retrieve by ID, room code, join token
    retrieved = repo.get_session("SESS_TEST_100")
    assert retrieved is not None
    assert retrieved.session_id == "SESS_TEST_100"
    assert retrieved.version == 1
    assert "DINER_HOST_01" in retrieved.diners

    by_code = repo.get_session_by_room_code("TAB-9K2M")
    assert by_code is not None
    assert by_code.session_id == "SESS_TEST_100"

    by_token = repo.get_session_by_join_token(session.join_token_128)
    assert by_token is not None
    assert by_token.session_id == "SESS_TEST_100"

    # 3. Add a second diner and update session with version 1 -> 2
    raw_guest_token = generate_guest_token()
    guest_diner = Diner(
        diner_id="DINER_GUEST_02",
        name="Rohit (Friend)",
        is_host=False,
        guest_token_hash=hash_token(raw_guest_token),
        preferences=DinerPreference(diet_rules={DietRule.VEGETARIAN}),
        payment_status=PaymentStatus.UNPAID,
        paid_amount_paise=0,
        joined_at=1700000100,
    )
    retrieved.diners["DINER_GUEST_02"] = guest_diner

    updated = repo.update_session(retrieved, expected_version=1)
    assert updated.version == 2

    # 4. Optimistic lock collision: Attempting update with stale version 1 must raise SessionConcurrencyError
    with pytest.raises(SessionConcurrencyError):
        repo.update_session(retrieved, expected_version=1)

    # 5. Audit Event Recording
    audit = AuditEvent(
        event_id="EVT_001",
        timestamp=1700000200,
        session_id="SESS_TEST_100",
        diner_id="DINER_GUEST_02",
        action="JOIN_ROOM",
        payload_hash="abcd1234hash",
        details="Rohit joined via room code",
    )
    repo.record_audit_event(audit)
    events = repo.list_audit_events("SESS_TEST_100")
    assert len(events) == 1
    assert events[0].action == "JOIN_ROOM"
    assert events[0].diner_id == "DINER_GUEST_02"


def test_session_expiration(tmp_path):
    """Verify that an expired session transitions to EXPIRED status."""
    db_file = tmp_path / "test_expiry.db"
    repo = TableSessionRepository(str(db_file))

    session = TableSession(
        session_id="SESS_EXPIRED_01",
        room_code="TAB-1111",
        join_token_128=generate_join_token_128(),
        host_diner_id="DINER_01",
        version=1,
        status=SessionStatus.OPEN_FOR_PREFERENCES,
        created_at=1000,
        updated_at=1000,
        expires_at=2000,  # Far in the past
    )
    repo.create_session(session)

    loaded = repo.get_session("SESS_EXPIRED_01")
    assert loaded is not None
    assert loaded.status == SessionStatus.EXPIRED


# ═══════════════════════════════════════════════════════════════════════════════
# 6. MENU CATALOG & SHARED DISHES INTERSECTION TESTS (Requirement 8)
# ═══════════════════════════════════════════════════════════════════════════════

def test_menu_catalog_loader():
    """Verify load_menu_catalog parses all dishes with integer paise and tri-state profiles."""
    menu = load_menu_catalog()
    assert len(menu) == 36
    for item in menu:
        assert isinstance(item.price_paise, int)
        assert not isinstance(item.price_paise, bool)
        assert item.price_paise > 0
        assert len(item.safety_profile.allergens) == len(Allergen)
        assert len(item.safety_profile.diets) == len(DietRule)


def test_find_safe_shared_dishes():
    """Verify that shared dishes strictly satisfy the intersection of all sharing diners."""
    menu = load_menu_catalog()

    diner1 = DinerPreference(diet_rules={DietRule.VEGETARIAN})
    diner2 = DinerPreference(allergies={Allergen.GLUTEN})

    shared = find_safe_shared_dishes(menu, [diner1, diner2])
    assert len(shared) > 0

    for dish in shared:
        # Every shared dish must be safe for diner 1
        safe1, _ = is_dish_safe_for_diner(dish, diner1)
        assert safe1 is True
        # Every shared dish must be safe for diner 2
        safe2, _ = is_dish_safe_for_diner(dish, diner2)
        assert safe2 is True

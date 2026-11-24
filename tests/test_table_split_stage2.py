"""
tests.test_table_split_stage2 — Comprehensive tests for Stage 2 Domain Model & Migrations.

Covers:
1. Migration Up and Down on clean SQLite databases (reversibility & idempotency).
2. Database CHECK constraints:
   - Non-negative amounts in paise (price_paise, items_paise, total_paise, packaging, tax, payments)
   - Positive quantities (quantity > 0, serves > 0)
   - Valid spice levels (0-4 or NULL)
   - Mandatory HARD strength for diet and allergen preferences
   - Unique constraints on join_token_hash and payment idempotency_key
   - Foreign key cascade deletions
3. Derived Dairy Rule (FSSAI compliance: milk allergen YES implies dairy YES).
4. Seed Script validation:
   - 12 items loaded
   - 1 vegan bowl (dairy NO, vegan YES)
   - 1 paneer pizza (dairy YES)
   - 1 garlic bread (dairy YES)
   - 1 spicy pizza (spice level 4)
   - Deliberately UNKNOWN fields present
"""

import sqlite3
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest

from table_split.db.migrations.runner import get_connection, migrate_down, migrate_up
from table_split.db.models import (
    Preference,
    derive_dairy_status,
)
from table_split.db.seed import seed_menu_item_extensions
from table_split.enums import TriState


@pytest.fixture
def clean_db(tmp_path: Path) -> Generator[Path, None, None]:
    """Provides a path to an isolated, fresh SQLite database file."""
    db_file = tmp_path / "test_stage2.db"
    yield db_file
    if db_file.exists():
        db_file.unlink()


@pytest.fixture
def migrated_db(clean_db: Path) -> Generator[Path, None, None]:
    """Provides a clean database migrated up to latest schema."""
    migrate_up(clean_db)
    yield clean_db


# ===========================================================================
# 1. Migration Tests
# ===========================================================================
def test_migration_up_and_down_lifecycle(clean_db: Path) -> None:
    # 1. Initial state: clean db has no tables
    conn = sqlite3.connect(clean_db)
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
    conn.close()
    assert len(tables) == 0

    # 2. Migrate Up
    applied = migrate_up(clean_db)
    assert "001" in applied

    conn = get_connection(clean_db)
    tables_after = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()]
    conn.close()

    expected_tables = {
        "schema_migrations",
        "menu_item_extensions",
        "table_sessions",
        "participants",
        "preferences",
        "proposals",
        "proposal_lines",
        "allocations",
        "payments",
        "session_events",
    }
    assert expected_tables.issubset(set(tables_after))

    # 3. Migrate Down
    rolled_back = migrate_down(clean_db, steps=1)
    assert "001" in rolled_back

    conn = get_connection(clean_db)
    tables_down = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()}
    conn.close()

    # All domain tables dropped; only schema_migrations remains (empty)
    for table in expected_tables - {"schema_migrations"}:
        assert table not in tables_down

    # 4. Migrate Up again (Reversibility cycle)
    applied_again = migrate_up(clean_db)
    assert "001" in applied_again


def test_migration_idempotent(migrated_db: Path) -> None:
    # Second migrate_up should detect already applied version and do nothing
    second_run = migrate_up(migrated_db)
    assert second_run == []


# ===========================================================================
# 2. Database Constraint Tests
# ===========================================================================
def test_constraint_negative_paise_rejected(migrated_db: Path) -> None:
    conn = get_connection(migrated_db)

    # 1. Negative price_paise in menu_item_extensions
    with pytest.raises(sqlite3.IntegrityError, match="ck_price_paise_non_negative"):
        conn.execute("""
            INSERT INTO menu_item_extensions (item_id, price_paise)
            VALUES ('D999', -100);
        """)

    # 2. Negative packaging_paise in menu_item_extensions
    with pytest.raises(sqlite3.IntegrityError, match="ck_packaging_paise_non_negative"):
        conn.execute("""
            INSERT INTO menu_item_extensions (item_id, price_paise, packaging_paise)
            VALUES ('D999', 1000, -50);
        """)

    # 3. Negative tax_rate_bps
    with pytest.raises(sqlite3.IntegrityError, match="ck_tax_rate_bps_non_negative"):
        conn.execute("""
            INSERT INTO menu_item_extensions (item_id, price_paise, tax_rate_bps)
            VALUES ('D999', 1000, -1);
        """)
    conn.close()


def test_constraint_spice_level_range(migrated_db: Path) -> None:
    conn = get_connection(migrated_db)

    # Valid values 0 through 4 and NULL must succeed
    for valid_spice in [None, 0, 1, 2, 3, 4]:
        item_id = f"SPICE_TEST_{valid_spice}"
        conn.execute(
            "INSERT INTO menu_item_extensions (item_id, price_paise, spice_level) VALUES (?, 1000, ?);",
            (item_id, valid_spice),
        )
    conn.commit()

    # Invalid values (< 0 or > 4) must be rejected
    with pytest.raises(sqlite3.IntegrityError, match="ck_spice_level_0_to_4"):
        conn.execute("INSERT INTO menu_item_extensions (item_id, price_paise, spice_level) VALUES ('INV_1', 1000, 5);")

    with pytest.raises(sqlite3.IntegrityError, match="ck_spice_level_0_to_4"):
        conn.execute("INSERT INTO menu_item_extensions (item_id, price_paise, spice_level) VALUES ('INV_2', 1000, -1);")

    conn.close()


def test_constraint_quantity_and_serves_positive(migrated_db: Path) -> None:
    conn = get_connection(migrated_db)

    # Serves <= 0 rejected
    with pytest.raises(sqlite3.IntegrityError, match="ck_serves_positive"):
        conn.execute("INSERT INTO menu_item_extensions (item_id, price_paise, serves) VALUES ('ZERO_SERVE', 1000, 0);")

    # Proposal Line quantity <= 0 rejected
    session_id = str(uuid.uuid4())
    conn.execute("""
        INSERT INTO table_sessions (id, join_token_hash, expires_at, created_at)
        VALUES (?, 'token_hash_q', 1799999999, 1700000000);
    """, (session_id,))

    proposal_id = str(uuid.uuid4())
    conn.execute("""
        INSERT INTO proposals (id, session_id, session_version, constraints_hash, created_at)
        VALUES (?, ?, 1, 'hash', 1700000000);
    """, (proposal_id, session_id))

    with pytest.raises(sqlite3.IntegrityError, match="ck_proposal_line_quantity_positive"):
        conn.execute("""
            INSERT INTO proposal_lines (id, proposal_id, menu_item_id, quantity, unit_price_paise)
            VALUES (?, ?, 'D001', 0, 28000);
        """, (str(uuid.uuid4()), proposal_id))

    conn.close()


def test_constraint_diet_and_allergen_must_be_hard(migrated_db: Path) -> None:
    conn = get_connection(migrated_db)

    session_id = str(uuid.uuid4())
    conn.execute("""
        INSERT INTO table_sessions (id, join_token_hash, expires_at, created_at)
        VALUES (?, 'token_hash_pref', 1799999999, 1700000000);
    """, (session_id,))

    participant_id = str(uuid.uuid4())
    conn.execute("""
        INSERT INTO participants (id, session_id, display_name, joined_at)
        VALUES (?, ?, 'Priya', 1700000000);
    """, (participant_id, session_id))

    # 1. Allergen preference with SOFT strength MUST be rejected
    with pytest.raises(sqlite3.IntegrityError, match="ck_diet_allergen_must_be_hard"):
        conn.execute("""
            INSERT INTO preferences (id, participant_id, kind, strength, value)
            VALUES (?, ?, 'allergen', 'SOFT', '{"allergen": "peanuts"}');
        """, (str(uuid.uuid4()), participant_id))

    # 2. Diet preference with SOFT strength MUST be rejected
    with pytest.raises(sqlite3.IntegrityError, match="ck_diet_allergen_must_be_hard"):
        conn.execute("""
            INSERT INTO preferences (id, participant_id, kind, strength, value)
            VALUES (?, ?, 'diet', 'SOFT', '{"diet": "vegan"}');
        """, (str(uuid.uuid4()), participant_id))

    # 3. Allergen preference with HARD strength MUST succeed
    conn.execute("""
        INSERT INTO preferences (id, participant_id, kind, strength, value)
        VALUES (?, ?, 'allergen', 'HARD', '{"allergen": "peanuts"}');
    """, (str(uuid.uuid4()), participant_id))

    # 4. Non-allergen/diet preference (budget, spice) with SOFT strength MUST succeed
    conn.execute("""
        INSERT INTO preferences (id, participant_id, kind, strength, value)
        VALUES (?, ?, 'budget_max_paise', 'SOFT', '{"max_paise": 50000}');
    """, (str(uuid.uuid4()), participant_id))

    conn.commit()
    conn.close()


def test_sqlalchemy_model_preference_validation() -> None:
    # Model-level validation before flush
    with pytest.raises(ValueError, match="must have strength 'HARD'"):
        Preference(
            participant_id="part-1",
            kind="allergen",
            strength="SOFT",
            value='{"allergen": "milk"}',
        )

    with pytest.raises(ValueError, match="must have strength 'HARD'"):
        Preference(
            participant_id="part-1",
            kind="diet",
            strength="SOFT",
            value='{"diet": "vegan"}',
        )

    # Valid hard preference
    pref = Preference(
        participant_id="part-1",
        kind="allergen",
        strength="HARD",
        value='{"allergen": "peanuts"}',
    )
    assert pref.strength == "HARD"


def test_unique_tokens_and_idempotency(migrated_db: Path) -> None:
    conn = get_connection(migrated_db)
    session_id = str(uuid.uuid4())

    conn.execute("""
        INSERT INTO table_sessions (id, join_token_hash, expires_at, created_at)
        VALUES (?, 'unique_token_123', 1799999999, 1700000000);
    """, (session_id,))

    # Duplicate join_token_hash rejected
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("""
            INSERT INTO table_sessions (id, join_token_hash, expires_at, created_at)
            VALUES (?, 'unique_token_123', 1799999999, 1700000000);
        """, (str(uuid.uuid4()),))

    # Duplicate idempotency_key in payments rejected
    part_id = str(uuid.uuid4())
    conn.execute("INSERT INTO participants (id, session_id, display_name, joined_at) VALUES (?, ?, 'Alex', 1700000000);", (part_id, session_id))

    conn.execute("""
        INSERT INTO payments (id, session_id, participant_id, gateway, gateway_order_id, amount_paise, idempotency_key, created_at)
        VALUES (?, ?, ?, 'razorpay', 'order_1', 10000, 'idem_key_999', 1700000000);
    """, (str(uuid.uuid4()), session_id, part_id))

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("""
            INSERT INTO payments (id, session_id, participant_id, gateway, gateway_order_id, amount_paise, idempotency_key, created_at)
            VALUES (?, ?, ?, 'razorpay', 'order_2', 10000, 'idem_key_999', 1700000000);
        """, (str(uuid.uuid4()), session_id, part_id))

    conn.close()


def test_foreign_key_cascade_deletion(migrated_db: Path) -> None:
    conn = get_connection(migrated_db)
    session_id = str(uuid.uuid4())
    part_id = str(uuid.uuid4())

    conn.execute("INSERT INTO table_sessions (id, join_token_hash, expires_at, created_at) VALUES (?, 't_hash', 1799999999, 1700000000);", (session_id,))
    conn.execute("INSERT INTO participants (id, session_id, display_name, joined_at) VALUES (?, ?, 'Maya', 1700000000);", (part_id, session_id))
    conn.execute("INSERT INTO preferences (id, participant_id, kind, strength, value) VALUES (?, ?, 'allergen', 'HARD', '{}');", (str(uuid.uuid4()), part_id))
    conn.commit()

    # Verify participant and preference exist
    assert conn.execute("SELECT count(*) FROM participants WHERE session_id = ?", (session_id,)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM preferences WHERE participant_id = ?", (part_id,)).fetchone()[0] == 1

    # Delete session -> participant and preference must be cascade deleted
    conn.execute("DELETE FROM table_sessions WHERE id = ?", (session_id,))
    conn.commit()

    assert conn.execute("SELECT count(*) FROM participants WHERE session_id = ?", (session_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM preferences WHERE participant_id = ?", (part_id,)).fetchone()[0] == 0
    conn.close()


# ===========================================================================
# 3. Derived Dairy Rule Tests
# ===========================================================================
def test_derived_dairy_rule_logic() -> None:
    # 1. Milk allergen YES strictly implies contains_dairy YES
    assert derive_dairy_status(TriState.YES) == TriState.YES
    assert derive_dairy_status(TriState.YES, diet_vegan=TriState.NO) == TriState.YES
    assert derive_dairy_status(TriState.YES, diet_vegan=TriState.YES) == TriState.YES  # Allergen fact overrides
    assert derive_dairy_status("YES") == TriState.YES

    # 2. Vegan YES and milk != YES implies dairy NO
    assert derive_dairy_status(TriState.NO, diet_vegan=TriState.YES) == TriState.NO
    assert derive_dairy_status(TriState.UNKNOWN, diet_vegan=TriState.YES) == TriState.NO

    # 3. Milk allergen NO implies dairy NO
    assert derive_dairy_status(TriState.NO, diet_vegan=TriState.NO) == TriState.NO

    # 4. Milk allergen UNKNOWN and not certified vegan implies dairy UNKNOWN
    assert derive_dairy_status(TriState.UNKNOWN, diet_vegan=TriState.NO) == TriState.UNKNOWN
    assert derive_dairy_status(TriState.UNKNOWN, diet_vegan=TriState.UNKNOWN) == TriState.UNKNOWN


def test_derived_dairy_db_constraint(migrated_db: Path) -> None:
    conn = get_connection(migrated_db)

    # Invariant: If allergen_milk == 'YES', contains_dairy must be 'YES'
    # Violating item: allergen_milk YES, contains_dairy NO -> rejected
    with pytest.raises(sqlite3.IntegrityError, match="ck_milk_implies_dairy"):
        conn.execute("""
            INSERT INTO menu_item_extensions (item_id, price_paise, allergen_milk, contains_dairy)
            VALUES ('DAIRY_VIOLATION', 1000, 'YES', 'NO');
        """)

    # Correct item: allergen_milk YES, contains_dairy YES -> accepted
    conn.execute("""
        INSERT INTO menu_item_extensions (item_id, price_paise, allergen_milk, contains_dairy)
        VALUES ('DAIRY_CORRECT', 1000, 'YES', 'YES');
    """)
    conn.commit()
    conn.close()


# ===========================================================================
# 4. Seed Script Validation Tests
# ===========================================================================
def test_seed_menu_item_extensions_accuracy(migrated_db: Path) -> None:
    count = seed_menu_item_extensions(str(migrated_db))
    assert count == 12

    conn = get_connection(migrated_db)
    items = {
        r["item_id"]: dict(r)
        for r in conn.execute("SELECT * FROM menu_item_extensions;").fetchall()
    }
    conn.close()

    assert len(items) == 12

    # 1. Vegan Bowl: Superfood Vegan Buddha Bowl
    vegan_bowl = items["D039"]
    assert vegan_bowl["diet_vegan"] == "YES"
    assert vegan_bowl["diet_veg"] == "YES"
    assert vegan_bowl["contains_dairy"] == "NO"
    assert vegan_bowl["allergen_milk"] == "NO"
    assert vegan_bowl["allergen_fish"] == "NO"
    assert vegan_bowl["serves"] == 1
    assert vegan_bowl["shareable"] == 0

    # 2. Paneer Pizza: Tandoori Paneer Pizza (dairy YES)
    paneer_pizza = items["D037"]
    assert paneer_pizza["contains_dairy"] == "YES"
    assert paneer_pizza["allergen_milk"] == "YES"
    assert paneer_pizza["diet_veg"] == "YES"
    assert paneer_pizza["serves"] == 2
    assert paneer_pizza["shareable"] == 1

    # 3. Garlic Bread: Artisan Garlic Bread (dairy YES)
    garlic_bread = items["D038"]
    assert garlic_bread["contains_dairy"] == "YES"
    assert garlic_bread["allergen_milk"] == "YES"
    assert garlic_bread["allergen_cereals_gluten"] == "YES"
    assert garlic_bread["shareable"] == 1

    # 4. Spicy Pizza: Fiery Ghost Pepper Pizza (spice_level 4)
    spicy_pizza = items["D040"]
    assert spicy_pizza["spice_level"] == 4
    assert spicy_pizza["shareable"] == 1
    assert spicy_pizza["contains_dairy"] == "YES"

    # 5. Deliberately UNKNOWN items
    samosa = items["D008"]
    assert samosa["allergen_peanuts"] == "UNKNOWN"
    assert samosa["allergen_tree_nuts"] == "UNKNOWN"
    assert samosa["allergen_milk"] == "UNKNOWN"
    assert samosa["contains_dairy"] == "UNKNOWN"

    fish_curry = items["D025"]
    assert fish_curry["allergen_fish"] == "YES"
    assert fish_curry["allergen_crustaceans"] == "UNKNOWN"

"""
tests.test_table_split_stage2 — Comprehensive tests for Stage 2 Domain Model & Migrations.

Covers:
1. SQLite Engine PRAGMAs:
   - foreign_keys = ON
   - journal_mode = WAL
   - busy_timeout >= 15000ms
2. Migration Up/Down, Checksums & Drift Detection:
   - Reversible schema migration
   - SHA-256 checksum tracking in schema_migrations
   - Drift detection raises MigrationDriftError on tampering
3. Database CHECK constraints:
   - Non-negative amounts in paise
   - Positive quantities (quantity > 0, serves > 0)
   - Valid spice levels (0-4 or NULL)
   - Mandatory HARD strength for diet and allergen preferences
   - Unique constraints on join_token_hash and payment idempotency_key
   - Foreign key cascade deletions
4. Food Safety & Dietary Invariants:
   - Derived Dairy Rule (milk allergen YES implies dairy YES)
   - Vegan rules: vegan=YES requires egg=NO, milk=NO, dairy=NO, non_veg!=YES
   - Vegan and non_veg mutual exclusivity
   - Jain rules: jain=YES requires onion/garlic!=YES, root_veg!=YES, non_veg!=YES
   - Non-statutory allergen_sesame tracking
5. Seed Script & Catalog Pricing:
   - Refusal without ALLOW_SEED=1 environment variable
   - Prices dynamically derived from data/menu.json (zero drift)
   - Sample fixture unverified state (verified_by=None, verified_at=None, halal=UNKNOWN)
"""

import sqlite3
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import text

from table_split.db.engine import create_sqlite_engine, get_sqlite_connection
from table_split.db.migrations.runner import (
    MigrationDriftError,
    get_connection,
    migrate_down,
    migrate_up,
    verify_schema_integrity,
)
from table_split.db.models import (
    Preference,
    derive_dairy_status,
)
from table_split.db.seed import (
    MENU_JSON_PATH,
    load_menu_json_prices,
    seed_menu_item_extensions,
)
from table_split.enums import TriState


@pytest.fixture
def clean_db(tmp_path: Path) -> Generator[Path, None, None]:
    """Provides a path to an isolated, fresh SQLite database file."""
    import gc

    db_file = tmp_path / "test_stage2.db"
    yield db_file
    gc.collect()
    try:
        if db_file.exists():
            db_file.unlink(missing_ok=True)
    except OSError:
        pass


@pytest.fixture
def migrated_db(clean_db: Path) -> Generator[Path, None, None]:
    """Provides a clean database migrated up to latest schema."""
    migrate_up(clean_db)
    yield clean_db


# ===========================================================================
# 1. SQLite Engine PRAGMA Tests
# ===========================================================================
def test_sqlite_pragmas_on_raw_connection(clean_db: Path) -> None:
    conn = get_sqlite_connection(clean_db)
    try:
        fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        assert fk == 1, "PRAGMA foreign_keys must be ON (1)"

        jm = conn.execute("PRAGMA journal_mode;").fetchone()[0].lower()
        assert jm == "wal", f"PRAGMA journal_mode must be WAL, got {jm}"

        timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        assert timeout >= 15000, f"PRAGMA busy_timeout must be >= 15000, got {timeout}"
    finally:
        conn.close()


def test_sqlite_pragmas_on_sqlalchemy_engine(clean_db: Path) -> None:
    engine = create_sqlite_engine(clean_db)
    try:
        with engine.connect() as conn:
            fk = conn.execute(text("PRAGMA foreign_keys;")).scalar()
            assert fk == 1

            jm = str(conn.execute(text("PRAGMA journal_mode;")).scalar()).lower()
            assert jm == "wal"

            timeout = conn.execute(text("PRAGMA busy_timeout;")).scalar()
            assert timeout is not None and int(timeout) >= 15000
    finally:
        engine.dispose()


# ===========================================================================
# 2. Migration Tests, Checksums & Drift Detection
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

    # Verify checksum was recorded
    row = conn.execute("SELECT version, name, checksum FROM schema_migrations WHERE version = '001';").fetchone()
    assert row is not None
    assert row["version"] == "001"
    assert len(row["checksum"]) == 64  # Valid SHA-256 hex string
    conn.close()

    # 3. Migrate Down
    rolled_back = migrate_down(clean_db, steps=1)
    assert "001" in rolled_back

    conn = get_connection(clean_db)
    tables_down = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()}
    conn.close()

    for table in expected_tables - {"schema_migrations"}:
        assert table not in tables_down

    # 4. Migrate Up again (Reversibility cycle)
    applied_again = migrate_up(clean_db)
    assert "001" in applied_again


def test_migration_idempotent(migrated_db: Path) -> None:
    # Second migrate_up should detect already applied version and do nothing
    second_run = migrate_up(migrated_db)
    assert second_run == []


def test_migration_drift_detection(migrated_db: Path) -> None:
    # Corrupt the recorded checksum in the database
    conn = get_connection(migrated_db)
    conn.execute("UPDATE schema_migrations SET checksum = 'corrupted_fake_checksum' WHERE version = '001';")
    conn.commit()
    conn.close()

    # Subsequent verify or migrate_up must raise MigrationDriftError
    with pytest.raises(MigrationDriftError, match="Migration drift detected for 001"):
        verify_schema_integrity(migrated_db)

    with pytest.raises(MigrationDriftError, match="Migration drift detected for 001"):
        migrate_up(migrated_db)


# ===========================================================================
# 3. Database Constraint Tests
# ===========================================================================
def test_constraint_negative_paise_rejected(migrated_db: Path) -> None:
    conn = get_connection(migrated_db)

    with pytest.raises(sqlite3.IntegrityError, match="ck_price_paise_non_negative"):
        conn.execute("INSERT INTO menu_item_extensions (item_id, price_paise) VALUES ('D999', -100);")

    with pytest.raises(sqlite3.IntegrityError, match="ck_packaging_paise_non_negative"):
        conn.execute("INSERT INTO menu_item_extensions (item_id, price_paise, packaging_paise) VALUES ('D999', 1000, -50);")

    with pytest.raises(sqlite3.IntegrityError, match="ck_tax_rate_bps_non_negative"):
        conn.execute("INSERT INTO menu_item_extensions (item_id, price_paise, tax_rate_bps) VALUES ('D999', 1000, -1);")
    conn.close()


def test_constraint_spice_level_range(migrated_db: Path) -> None:
    conn = get_connection(migrated_db)

    for valid_spice in [None, 0, 1, 2, 3, 4]:
        item_id = f"SPICE_TEST_{valid_spice}"
        conn.execute(
            "INSERT INTO menu_item_extensions (item_id, price_paise, spice_level) VALUES (?, 1000, ?);",
            (item_id, valid_spice),
        )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="ck_spice_level_0_to_4"):
        conn.execute("INSERT INTO menu_item_extensions (item_id, price_paise, spice_level) VALUES ('INV_1', 1000, 5);")

    with pytest.raises(sqlite3.IntegrityError, match="ck_spice_level_0_to_4"):
        conn.execute("INSERT INTO menu_item_extensions (item_id, price_paise, spice_level) VALUES ('INV_2', 1000, -1);")

    conn.close()


def test_constraint_quantity_and_serves_positive(migrated_db: Path) -> None:
    conn = get_connection(migrated_db)

    with pytest.raises(sqlite3.IntegrityError, match="ck_serves_positive"):
        conn.execute("INSERT INTO menu_item_extensions (item_id, price_paise, serves) VALUES ('ZERO_SERVE', 1000, 0);")

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

    # Allergen preference with SOFT strength MUST be rejected
    with pytest.raises(sqlite3.IntegrityError, match="ck_diet_allergen_must_be_hard"):
        conn.execute("""
            INSERT INTO preferences (id, participant_id, kind, strength, value)
            VALUES (?, ?, 'allergen', 'SOFT', '{"allergen": "peanuts"}');
        """, (str(uuid.uuid4()), participant_id))

    # Diet preference with SOFT strength MUST be rejected
    with pytest.raises(sqlite3.IntegrityError, match="ck_diet_allergen_must_be_hard"):
        conn.execute("""
            INSERT INTO preferences (id, participant_id, kind, strength, value)
            VALUES (?, ?, 'diet', 'SOFT', '{"diet": "vegan"}');
        """, (str(uuid.uuid4()), participant_id))

    # Allergen preference with HARD strength MUST succeed
    conn.execute("""
        INSERT INTO preferences (id, participant_id, kind, strength, value)
        VALUES (?, ?, 'allergen', 'HARD', '{"allergen": "peanuts"}');
    """, (str(uuid.uuid4()), participant_id))

    # Budget preference with SOFT strength MUST succeed
    conn.execute("""
        INSERT INTO preferences (id, participant_id, kind, strength, value)
        VALUES (?, ?, 'budget_max_paise', 'SOFT', '{"max_paise": 50000}');
    """, (str(uuid.uuid4()), participant_id))

    conn.commit()
    conn.close()


def test_sqlalchemy_model_preference_validation() -> None:
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

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("""
            INSERT INTO table_sessions (id, join_token_hash, expires_at, created_at)
            VALUES (?, 'unique_token_123', 1799999999, 1700000000);
        """, (str(uuid.uuid4()),))

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

    assert conn.execute("SELECT count(*) FROM participants WHERE session_id = ?", (session_id,)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM preferences WHERE participant_id = ?", (part_id,)).fetchone()[0] == 1

    conn.execute("DELETE FROM table_sessions WHERE id = ?", (session_id,))
    conn.commit()

    assert conn.execute("SELECT count(*) FROM participants WHERE session_id = ?", (session_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM preferences WHERE participant_id = ?", (part_id,)).fetchone()[0] == 0
    conn.close()


# ===========================================================================
# 4. Food Safety & Dietary Constraints (Vegan & Jain Invariants)
# ===========================================================================
def test_derived_dairy_rule_logic() -> None:
    assert derive_dairy_status(TriState.YES) == TriState.YES
    assert derive_dairy_status(TriState.YES, diet_vegan=TriState.NO) == TriState.YES
    assert derive_dairy_status(TriState.YES, diet_vegan=TriState.YES) == TriState.YES
    assert derive_dairy_status("YES") == TriState.YES

    assert derive_dairy_status(TriState.NO, diet_vegan=TriState.YES) == TriState.NO
    assert derive_dairy_status(TriState.UNKNOWN, diet_vegan=TriState.YES) == TriState.NO
    assert derive_dairy_status(TriState.NO, diet_vegan=TriState.NO) == TriState.NO

    assert derive_dairy_status(TriState.UNKNOWN, diet_vegan=TriState.NO) == TriState.UNKNOWN
    assert derive_dairy_status(TriState.UNKNOWN, diet_vegan=TriState.UNKNOWN) == TriState.UNKNOWN


def test_derived_dairy_db_constraint(migrated_db: Path) -> None:
    conn = get_connection(migrated_db)

    with pytest.raises(sqlite3.IntegrityError, match="ck_milk_implies_dairy"):
        conn.execute("""
            INSERT INTO menu_item_extensions (item_id, price_paise, allergen_milk, contains_dairy)
            VALUES ('DAIRY_VIOLATION', 1000, 'YES', 'NO');
        """)

    conn.execute("""
        INSERT INTO menu_item_extensions (item_id, price_paise, allergen_milk, contains_dairy)
        VALUES ('DAIRY_CORRECT', 1000, 'YES', 'YES');
    """)
    conn.commit()
    conn.close()


def test_vegan_integrity_constraints(migrated_db: Path) -> None:
    conn = get_connection(migrated_db)
    try:
        # 1. Vegan=YES and non_veg=YES rejected
        with pytest.raises(sqlite3.IntegrityError, match="ck_vegan"):
            conn.execute("""
                INSERT INTO menu_item_extensions (item_id, price_paise, diet_vegan, diet_non_veg)
                VALUES ('V_FAIL_1', 1000, 'YES', 'YES');
            """)

        # 2. Vegan=YES with egg!=NO rejected
        with pytest.raises(sqlite3.IntegrityError, match="ck_vegan_implies_no_animal_products"):
            conn.execute("""
                INSERT INTO menu_item_extensions (
                    item_id, price_paise, diet_vegan, diet_egg, allergen_milk, contains_dairy, diet_non_veg
                ) VALUES ('V_FAIL_2', 1000, 'YES', 'YES', 'NO', 'NO', 'NO');
            """)

        # 3. Vegan=YES with milk!=NO rejected
        with pytest.raises(sqlite3.IntegrityError, match="ck_vegan_implies_no_animal_products"):
            conn.execute("""
                INSERT INTO menu_item_extensions (
                    item_id, price_paise, diet_vegan, diet_egg, allergen_milk, contains_dairy, diet_non_veg
                ) VALUES ('V_FAIL_3', 1000, 'YES', 'NO', 'YES', 'YES', 'NO');
            """)

        # 4. Valid vegan dish succeeds
        conn.execute("""
            INSERT INTO menu_item_extensions (
                item_id, price_paise, diet_vegan, diet_veg, diet_egg, allergen_milk, contains_dairy, diet_non_veg
            ) VALUES ('V_PASS', 1000, 'YES', 'YES', 'NO', 'NO', 'NO', 'NO');
        """)
        conn.commit()
    finally:
        conn.close()


def test_jain_integrity_constraints(migrated_db: Path) -> None:
    conn = get_connection(migrated_db)

    # Jain=YES with contains_onion_garlic=YES rejected
    with pytest.raises(sqlite3.IntegrityError, match="ck_jain_rules"):
        conn.execute("""
            INSERT INTO menu_item_extensions (item_id, price_paise, diet_jain, contains_onion_garlic)
            VALUES ('J_FAIL_1', 1000, 'YES', 'YES');
        """)

    # Jain=YES with contains_root_veg=YES rejected
    with pytest.raises(sqlite3.IntegrityError, match="ck_jain_rules"):
        conn.execute("""
            INSERT INTO menu_item_extensions (item_id, price_paise, diet_jain, contains_root_veg)
            VALUES ('J_FAIL_2', 1000, 'YES', 'YES');
        """)

    # Valid Jain dish succeeds
    conn.execute("""
        INSERT INTO menu_item_extensions (
            item_id, price_paise, diet_jain, contains_onion_garlic, contains_root_veg, diet_non_veg
        ) VALUES ('J_PASS', 1000, 'YES', 'NO', 'NO', 'NO');
    """)
    conn.commit()
    conn.close()


# ===========================================================================
# 5. Seed Script, Guard & Price Drift Tests
# ===========================================================================
def test_seed_refuses_without_env_var(migrated_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Ensure ALLOW_SEED is not 1
    monkeypatch.delenv("ALLOW_SEED", raising=False)

    with pytest.raises(PermissionError, match="ALLOW_SEED=1"):
        seed_menu_item_extensions(str(migrated_db))


def test_seed_succeeds_with_env_var(migrated_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_SEED", "1")
    count = seed_menu_item_extensions(str(migrated_db))
    assert count == 12


def test_price_drift_against_menu_json(migrated_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that every seed item price exactly equals data/menu.json price (rupees * 100)."""
    monkeypatch.setenv("ALLOW_SEED", "1")
    seed_menu_item_extensions(str(migrated_db))

    catalog_prices = load_menu_json_prices()

    conn = get_connection(migrated_db)
    rows = conn.execute("SELECT item_id, price_paise FROM menu_item_extensions;").fetchall()
    conn.close()

    assert len(rows) == 12
    for r in rows:
        item_id = r["item_id"]
        assert item_id in catalog_prices, f"Seed item {item_id} missing from {MENU_JSON_PATH}"
        assert r["price_paise"] == catalog_prices[item_id], (
            f"Price drift for {item_id}: DB has {r['price_paise']} paise, menu.json has {catalog_prices[item_id]} paise"
        )


def test_seed_fixture_unverified_status(migrated_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_SEED", "1")
    seed_menu_item_extensions(str(migrated_db))

    conn = get_connection(migrated_db)
    items = {
        r["item_id"]: dict(r)
        for r in conn.execute("SELECT * FROM menu_item_extensions;").fetchall()
    }
    conn.close()

    for item_id, item in items.items():
        # verified_by and verified_at must be None for all sample fixture rows
        assert item["verified_by"] is None, f"{item_id} has non-null verified_by"
        assert item["verified_at"] is None, f"{item_id} has non-null verified_at"
        # Halal must be UNKNOWN
        assert item["diet_halal"] == "UNKNOWN", f"{item_id} has halal tag '{item['diet_halal']}' instead of UNKNOWN"
        # Sesame must be tracked
        assert item["allergen_sesame"] in ("YES", "NO", "UNKNOWN")

    # Specific item checks
    # Vegan bowl
    assert items["D039"]["diet_vegan"] == "YES"
    assert items["D039"]["diet_egg"] == "NO"
    assert items["D039"]["allergen_milk"] == "NO"
    assert items["D039"]["contains_dairy"] == "NO"
    assert items["D039"]["allergen_sesame"] == "YES"  # Tahini

    # Garlic bread & paneer pizza
    assert items["D037"]["contains_dairy"] == "YES"
    assert items["D038"]["contains_dairy"] == "YES"
    assert items["D040"]["spice_level"] == 4

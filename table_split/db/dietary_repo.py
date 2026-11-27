"""
table_split.db.dietary_repo — Persistence repository and data-quality reporting
for SpiceRoute Kitchen's dietary and allergen verification workflow.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from config import DATA_DIR, TABLE_SPLIT_DB_PATH
from table_split.db.engine import get_sqlite_connection
from table_split.db.migrations.runner import migrate_up
from table_split.dietary import (
    ALL_TRISTATE_FIELDS,
    DietaryItem,
    normalize_tristate,
)
from table_split.enums import TriState

DEFAULT_MENU_JSON_PATH = DATA_DIR / "menu.json"


def _load_menu_json(menu_path: Path | None = None) -> list[dict[str, Any]]:
    path = menu_path or DEFAULT_MENU_JSON_PATH
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return list(data.get("menu", []))


class DietaryRepository:
    """
    Repository joining master catalog (data/menu.json) with SQLite menu_item_extensions.
    Handles migrations, staff verification sign-off, and Table Split safety quality reports.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        menu_path: Path | None = None,
    ) -> None:
        self.db_path = Path(db_path or TABLE_SPLIT_DB_PATH)
        self.menu_path = menu_path or DEFAULT_MENU_JSON_PATH
        migrate_up(self.db_path)

    def _fetch_extensions_map(self) -> dict[str, dict[str, Any]]:
        conn = get_sqlite_connection(self.db_path)
        try:
            rows = conn.execute("SELECT * FROM menu_item_extensions;").fetchall()
            return {r["item_id"]: dict(r) for r in rows}
        finally:
            conn.close()

    def list_items(self) -> list[DietaryItem]:
        """
        Returns normalized DietaryItems combining data/menu.json catalog metadata
        with persisted verification & dietary attributes from SQLite.
        """
        raw_menu = _load_menu_json(self.menu_path)
        ext_map = self._fetch_extensions_map()

        items: list[DietaryItem] = []
        seen_ids: set[str] = set()

        for raw in raw_menu:
            item_id = str(raw["id"])
            seen_ids.add(item_id)
            ext = ext_map.get(item_id)
            price_paise = int(raw["price"]) * 100
            raw_sales = int(raw.get("sales_volume", 0) or 0)

            payload: dict[str, Any] = {
                "item_id": item_id,
                "dish_name": raw.get("dish_name", ""),
                "category": raw.get("category", ""),
                "description": raw.get("description", ""),
                "price_paise": price_paise,
                "is_available": bool(raw.get("is_available", True)),
                "sales_volume": raw_sales,
                "tags": list(raw.get("tags", [])),
                "cuisine": raw.get("cuisine"),
            }
            if ext:
                for field_name in ALL_TRISTATE_FIELDS:
                    if field_name in ext:
                        payload[field_name] = ext[field_name]
                payload["spice_level"] = ext.get("spice_level")
                payload["serves"] = ext.get("serves", 1)
                payload["shareable"] = bool(ext.get("shareable", 1))
                payload["tax_rate_bps"] = int(ext.get("tax_rate_bps", 500))
                payload["packaging_paise"] = int(ext.get("packaging_paise", 0))
                payload["verified_by"] = ext.get("verified_by")
                payload["verified_at"] = ext.get("verified_at")
                payload["cross_contact_note"] = ext.get("cross_contact_note")
                if ext.get("sales_volume") is not None and int(ext["sales_volume"]) > 0:
                    payload["sales_volume"] = int(ext["sales_volume"])

            items.append(DietaryItem.from_record(payload))

        # Include any DB extension rows added directly without menu.json entries
        for item_id, ext in sorted(ext_map.items()):
            if item_id in seen_ids:
                continue
            payload = dict(ext)
            payload.setdefault("dish_name", item_id)
            payload["shareable"] = bool(ext.get("shareable", 1))
            payload["sales_volume"] = int(ext.get("sales_volume", 0) or 0)
            items.append(DietaryItem.from_record(payload))

        return items

    def get_item(self, item_id: str) -> DietaryItem | None:
        for item in self.list_items():
            if item.item_id == item_id:
                return item
        return None

    def verify_item(
        self,
        item_id: str,
        updates: Mapping[str, Any],
        verified_by: str,
        verified_at: int | None = None,
    ) -> DietaryItem:
        """
        Updates dietary/allergen fields for an item and records staff verification sign-off.
        Validates vegan, jain, and derived milk->dairy invariants prior to DB commit.
        """
        reviewer = (verified_by or "").strip()
        if not reviewer:
            raise ValueError("verified_by must be a non-empty staff name or identifier.")

        current = self.get_item(item_id)
        if current is None:
            raise KeyError(f"Menu item '{item_id}' not found in catalog or database.")

        timestamp = int(time.time()) if verified_at is None else int(verified_at)

        merged: dict[str, Any] = current.model_dump()
        for key, val in updates.items():
            if key in ALL_TRISTATE_FIELDS:
                merged[key] = normalize_tristate(val).value
            elif key in ("spice_level", "serves", "shareable", "cross_contact_note", "sales_volume"):
                merged[key] = val

        # Convert TriState enums in merged dict to uppercase string values
        for field_name in ALL_TRISTATE_FIELDS:
            val = merged.get(field_name, TriState.UNKNOWN.value)
            merged[field_name] = val.value if isinstance(val, TriState) else str(val).upper()

        # Apply derived dairy invariant: milk YES implies contains_dairy YES
        if merged["allergen_milk"] == "YES":
            merged["contains_dairy"] = "YES"

        # Validate domain invariants clearly before SQLite CHECK execution
        if merged["diet_vegan"] == "YES":
            if merged["diet_non_veg"] == "YES":
                raise ValueError("Invalid combination: diet_vegan=YES and diet_non_veg=YES cannot both be YES.")
            if merged["diet_egg"] != "NO" or merged["allergen_milk"] != "NO" or merged["contains_dairy"] != "NO":
                raise ValueError(
                    "Invalid vegan item: diet_vegan=YES requires diet_egg='NO', "
                    "allergen_milk='NO', and contains_dairy='NO'."
                )
            if merged["contains_honey"] == "YES":
                raise ValueError("Invalid vegan item: diet_vegan=YES cannot have contains_honey='YES'.")

        if merged["diet_jain"] == "YES" and (
            merged["contains_onion_garlic"] == "YES"
            or merged["contains_root_veg"] == "YES"
            or merged["diet_non_veg"] == "YES"
        ):
            raise ValueError(
                "Invalid Jain item: diet_jain=YES requires contains_onion_garlic!='YES', "
                "contains_root_veg!='YES', and diet_non_veg!='YES'."
            )

        spice = merged.get("spice_level")
        if spice is not None and (int(spice) < 0 or int(spice) > 4):
            raise ValueError("spice_level must be between 0 and 4 or None.")

        serves = int(merged.get("serves", 1) or 1)
        if serves <= 0:
            raise ValueError("serves must be a positive integer.")

        sales_volume = int(merged.get("sales_volume", 0) or 0)
        if sales_volume < 0:
            raise ValueError("sales_volume cannot be negative.")

        db_params = {
            "item_id": item_id,
            **{f: merged[f] for f in ALL_TRISTATE_FIELDS},
            "spice_level": int(spice) if spice is not None else None,
            "serves": serves,
            "shareable": 1 if bool(merged.get("shareable", True)) else 0,
            "price_paise": int(current.price_paise),
            "tax_rate_bps": int(updates.get("tax_rate_bps", 500)),
            "packaging_paise": int(updates.get("packaging_paise", 0)),
            "verified_by": reviewer,
            "verified_at": timestamp,
            "cross_contact_note": merged.get("cross_contact_note"),
            "sales_volume": sales_volume,
        }

        conn = get_sqlite_connection(self.db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO menu_item_extensions (
                        item_id, diet_veg, diet_non_veg, diet_egg, diet_vegan, diet_jain, diet_halal, diet_gluten_free,
                        contains_onion_garlic, contains_root_veg, contains_honey,
                        allergen_cereals_gluten, allergen_crustaceans, allergen_milk, allergen_eggs, allergen_fish,
                        allergen_peanuts, allergen_tree_nuts, allergen_soy, allergen_sulphite, allergen_sesame,
                        allergen_mustard, contains_dairy,
                        spice_level, serves, shareable, price_paise, tax_rate_bps, packaging_paise,
                        verified_by, verified_at, cross_contact_note, sales_volume
                    ) VALUES (
                        :item_id, :diet_veg, :diet_non_veg, :diet_egg, :diet_vegan, :diet_jain, :diet_halal, :diet_gluten_free,
                        :contains_onion_garlic, :contains_root_veg, :contains_honey,
                        :allergen_cereals_gluten, :allergen_crustaceans, :allergen_milk, :allergen_eggs, :allergen_fish,
                        :allergen_peanuts, :allergen_tree_nuts, :allergen_soy, :allergen_sulphite, :allergen_sesame,
                        :allergen_mustard, :contains_dairy,
                        :spice_level, :serves, :shareable, :price_paise, :tax_rate_bps, :packaging_paise,
                        :verified_by, :verified_at, :cross_contact_note, :sales_volume
                    )
                    ON CONFLICT(item_id) DO UPDATE SET
                        diet_veg=excluded.diet_veg,
                        diet_non_veg=excluded.diet_non_veg,
                        diet_egg=excluded.diet_egg,
                        diet_vegan=excluded.diet_vegan,
                        diet_jain=excluded.diet_jain,
                        diet_halal=excluded.diet_halal,
                        diet_gluten_free=excluded.diet_gluten_free,
                        contains_onion_garlic=excluded.contains_onion_garlic,
                        contains_root_veg=excluded.contains_root_veg,
                        contains_honey=excluded.contains_honey,
                        allergen_cereals_gluten=excluded.allergen_cereals_gluten,
                        allergen_crustaceans=excluded.allergen_crustaceans,
                        allergen_milk=excluded.allergen_milk,
                        allergen_eggs=excluded.allergen_eggs,
                        allergen_fish=excluded.allergen_fish,
                        allergen_peanuts=excluded.allergen_peanuts,
                        allergen_tree_nuts=excluded.allergen_tree_nuts,
                        allergen_soy=excluded.allergen_soy,
                        allergen_sulphite=excluded.allergen_sulphite,
                        allergen_sesame=excluded.allergen_sesame,
                        allergen_mustard=excluded.allergen_mustard,
                        contains_dairy=excluded.contains_dairy,
                        spice_level=excluded.spice_level,
                        serves=excluded.serves,
                        shareable=excluded.shareable,
                        price_paise=excluded.price_paise,
                        verified_by=excluded.verified_by,
                        verified_at=excluded.verified_at,
                        cross_contact_note=excluded.cross_contact_note,
                        sales_volume=excluded.sales_volume;
                    """,
                    db_params,
                )
        finally:
            conn.close()

        refreshed = self.get_item(item_id)
        if refreshed is None:  # pragma: no cover - invariant after insert
            raise RuntimeError(f"Failed reloading verified item {item_id}")
        return refreshed

    def generate_data_quality_report(self) -> dict[str, Any]:
        """
        Generates the Stage 3 Data-Quality Report:
        - Percentage of menu items fully verified (signed off AND 0 UNKNOWN fields)
        - List of items blocking Table Split safety, ordered by sales_volume DESC, item_id ASC
        """
        ext_map = self._fetch_extensions_map()
        items = self.list_items()

        total_items = len(items)
        fully_verified_count = 0
        staff_signed_off_count = 0
        blocking_items: list[dict[str, Any]] = []

        for item in items:
            if item.is_verified:
                staff_signed_off_count += 1
            unknowns = item.get_unknown_fields()
            if item.is_fully_verified:
                fully_verified_count += 1
            else:
                reason_parts: list[str] = []
                if not item.is_verified:
                    reason_parts.append("missing staff sign-off (verified_by/verified_at)")
                if unknowns:
                    reason_parts.append(f"{len(unknowns)} UNKNOWN field(s)")
                blocking_items.append({
                    "item_id": item.item_id,
                    "dish_name": item.dish_name,
                    "category": item.category,
                    "sales_volume": int(item.sales_volume),
                    "has_extension_row": item.item_id in ext_map,
                    "is_verified": item.is_verified,
                    "is_fully_verified": False,
                    "verified_by": item.verified_by,
                    "verified_at": item.verified_at,
                    "unknown_fields": unknowns,
                    "unknown_count": len(unknowns),
                    "blocking_reason": "; ".join(reason_parts),
                })

        # Order blocking items strictly by sales_volume DESC, then item_id ASC
        blocking_items.sort(key=lambda entry: (-int(entry["sales_volume"]), str(entry["item_id"])))

        pct = round((fully_verified_count / total_items) * 100.0, 2) if total_items > 0 else 100.0
        return {
            "total_items": total_items,
            "fully_verified_count": fully_verified_count,
            "fully_verified_percentage": pct,
            "staff_signed_off_count": staff_signed_off_count,
            "unverified_or_incomplete_count": len(blocking_items),
            "blocking_items": blocking_items,
        }

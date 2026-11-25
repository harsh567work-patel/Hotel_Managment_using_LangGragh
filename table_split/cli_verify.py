"""
table_split.cli_verify — Staff CLI utility for reviewing UNKNOWN dietary/allergen
fields, recording staff sign-off (verified_by & verified_at), and printing data-quality reports.

Usage examples:
  python -m table_split.cli_verify --report
  python -m table_split.cli_verify --list-unknown
  python -m table_split.cli_verify --item D008 --set allergen_milk=NO --set allergen_peanuts=NO --verified-by "Chef Priya"
  python -m table_split.cli_verify --interactive --verified-by "Chef Priya"
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from config import TABLE_SPLIT_DB_PATH
from table_split.db.dietary_repo import DEFAULT_MENU_JSON_PATH, DietaryRepository
from table_split.dietary import ALL_TRISTATE_FIELDS


def parse_key_value_updates(pairs: Sequence[str]) -> dict[str, Any]:
    """Parses CLI `--set FIELD=VALUE` pairs into validated update dictionary."""
    updates: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"Invalid --set argument '{pair}'. Expected FIELD=VALUE.")
        key, raw_val = pair.split("=", 1)
        key = key.strip()
        val = raw_val.strip()
        if key in ALL_TRISTATE_FIELDS:
            updates[key] = val.upper()
        elif key in ("spice_level", "serves", "sales_volume"):
            updates[key] = None if val.lower() in ("none", "null", "") else int(val)
        elif key == "shareable":
            updates[key] = val.lower() in ("true", "1", "yes")
        elif key == "cross_contact_note":
            updates[key] = val
        else:
            raise ValueError(f"Unsupported dietary field '{key}'.")
    return updates


def interactive_review(
    repo: DietaryRepository,
    reviewer: str,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    item_id_filter: str | None = None,
) -> int:
    """
    Steps through items with UNKNOWN fields or missing verification, letting staff
    review/edit each field interactively and record verified_by + verified_at.
    Returns count of items saved.
    """
    items = [it for it in repo.list_items() if not it.is_fully_verified]
    if item_id_filter:
        items = [it for it in items if it.item_id == item_id_filter]

    items.sort(key=lambda it: (-it.sales_volume, it.item_id))

    if not items:
        output_fn("All matching items are already fully verified with 0 UNKNOWN fields.")
        return 0

    verified_count = 0
    for item in items:
        unknowns = item.get_unknown_fields()
        output_fn(f"\n=== [{item.item_id}] {item.dish_name} (Sales: {item.sales_volume}) ===")
        output_fn(f"Verified: {item.is_verified} | UNKNOWN fields ({len(unknowns)}): {', '.join(unknowns) or 'none'}")

        updates: dict[str, Any] = {}
        fields_to_review = unknowns if unknowns else list(ALL_TRISTATE_FIELDS)
        for field_name in fields_to_review:
            current_val = getattr(item, field_name).value
            raw = input_fn(f"  {field_name} [{current_val}] (YES/NO/UNKNOWN/skip): ").strip()
            if not raw or raw.lower() in ("skip", "s"):
                continue
            updates[field_name] = raw.upper()

        note = input_fn(f"  cross_contact_note [{item.cross_contact_note or ''}]: ").strip()
        if note:
            updates["cross_contact_note"] = note

        confirm = input_fn(f"  Sign off '{item.item_id}' as '{reviewer}'? [y/N]: ").strip().lower()
        if confirm in ("y", "yes"):
            try:
                saved = repo.verify_item(item.item_id, updates=updates, verified_by=reviewer)
                verified_count += 1
                output_fn(
                    f"  Saved {saved.item_id} (verified_by={saved.verified_by}, "
                    f"remaining UNKNOWN={len(saved.get_unknown_fields())})."
                )
            except ValueError as err:
                output_fn(f"  Error saving {item.item_id}: {err}")
        else:
            output_fn(f"  Skipped sign-off for {item.item_id}.")

    return verified_count


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SpiceRoute Kitchen — Staff Dietary Review & Verification CLI",
    )
    parser.add_argument("--db-path", default=TABLE_SPLIT_DB_PATH, help="SQLite DB path")
    parser.add_argument("--menu-path", default=str(DEFAULT_MENU_JSON_PATH), help="Master menu.json path")
    parser.add_argument("--report", action="store_true", help="Print JSON data-quality report")
    parser.add_argument("--list-unknown", action="store_true", help="List items with UNKNOWN fields or unverified status")
    parser.add_argument("--interactive", action="store_true", help="Review blocking items interactively")
    parser.add_argument("--item", help="Target item_id (e.g. D008)")
    parser.add_argument("--set", dest="sets", action="append", default=[], help="Update field (FIELD=VALUE)")
    parser.add_argument("--verified-by", help="Staff name/identifier signing off verification")
    parser.add_argument("--verified-at", type=int, default=None, help="Explicit Unix epoch timestamp")

    args = parser.parse_args(argv)
    repo = DietaryRepository(db_path=args.db_path, menu_path=Path(args.menu_path))

    if args.report:
        print(json.dumps(repo.generate_data_quality_report(), indent=2))
        return 0

    if args.list_unknown:
        report = repo.generate_data_quality_report()
        for entry in report["blocking_items"]:
            print(
                f"{entry['item_id']:<6} | {entry['dish_name']:<30} | sales={entry['sales_volume']:<4} | "
                f"unknowns={entry['unknown_count']} ({', '.join(entry['unknown_fields'][:5])})"
            )
        print(f"Total blocking: {report['unverified_or_incomplete_count']} / {report['total_items']} "
              f"({report['fully_verified_percentage']}% verified)")
        return 0

    if args.interactive:
        if not args.verified_by:
            print("Error: --verified-by is required for interactive review.", file=sys.stderr)
            return 2
        interactive_review(repo, reviewer=args.verified_by, item_id_filter=args.item)
        return 0

    if args.item:
        if not args.verified_by:
            print("Error: --verified-by is required when verifying an item.", file=sys.stderr)
            return 2
        updates = parse_key_value_updates(args.sets)
        updated = repo.verify_item(
            item_id=args.item,
            updates=updates,
            verified_by=args.verified_by,
            verified_at=args.verified_at,
        )
        print(
            json.dumps(
                {
                    "item_id": updated.item_id,
                    "verified_by": updated.verified_by,
                    "verified_at": updated.verified_at,
                    "unknown_fields": updated.get_unknown_fields(),
                },
                indent=2,
            )
        )
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

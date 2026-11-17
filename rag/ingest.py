"""
ingest.py — Ingest restaurant knowledge base (menu.json + golden_dataset.json) into Qdrant.
Embeddings include comprehensive dish metadata (name, description, category, price,
prep time, chef, dietary classification, tags).
Payload includes indexed fields (is_available, is_veg, category, price) for hard filtering.
"""

import json
import uuid
import sys
from pathlib import Path
from qdrant_client.models import PointStruct

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from config import DATA_DIR, MENU_COLLECTION, QA_COLLECTION
from rag.qdrant_client import (
    get_client, embed_text,
    ensure_collection,
)


def ingest_menu() -> None:
    """Load menu.json and upsert each dish as a Qdrant point with rich embedding and payload."""
    ensure_collection(MENU_COLLECTION)
    client = get_client()

    with open(DATA_DIR / "menu.json", "r", encoding="utf-8") as f:
        data = json.load(f)

    points = []
    for dish in data["menu"]:
        tags = dish.get("tags", [])
        is_veg = "veg" in tags and "non-veg" not in tags

        # Rich embedding text incorporating all searchable attributes
        embed_str = (
            f"{dish['dish_name']} — {dish['description']} "
            f"Category: {dish['category']}. "
            f"Price: ₹{dish['price']}. "
            f"Prep Time: {dish['prep_time_minutes']} minutes. "
            f"Chef / Specialty: {dish['dish_man']}. "
            f"Diet: {'Vegetarian (veg)' if is_veg else 'Non-Vegetarian (non-veg)'}. "
            f"Tags: {', '.join(tags)}."
        )

        vector = embed_text(embed_str)
        points.append(
            PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={
                    "dish_id":           dish["id"],
                    "dish_name":         dish["dish_name"],
                    "category":          dish["category"],
                    "description":       dish["description"],
                    "price":             float(dish["price"]),
                    "prep_time_minutes": int(dish["prep_time_minutes"]),
                    "is_available":      bool(dish["is_available"]),
                    "is_veg":            bool(is_veg),
                    "restaurant_name":   data["restaurant"]["name"],
                    "dish_man":          dish["dish_man"],
                    "tags":              tags,
                },
            )
        )

    # Delete existing points in collection to ensure clean update
    client.delete(
        collection_name=MENU_COLLECTION,
        points_selector=[p.id for p in client.scroll(collection_name=MENU_COLLECTION, limit=100)[0]]
    )
    client.upsert(collection_name=MENU_COLLECTION, points=points)
    print(f"✅  Ingested {len(points)} dishes into '{MENU_COLLECTION}' with rich embeddings & payload filters")


def ingest_golden_dataset() -> None:
    """Load golden_dataset.json into Qdrant for evaluation retrieval."""
    ensure_collection(QA_COLLECTION)
    client = get_client()

    with open(DATA_DIR / "golden_dataset.json", "r", encoding="utf-8") as f:
        qa_pairs = json.load(f)

    points = []
    for qa in qa_pairs:
        vector = embed_text(qa["question"])
        points.append(
            PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload=qa,
            )
        )

    client.delete(
        collection_name=QA_COLLECTION,
        points_selector=[p.id for p in client.scroll(collection_name=QA_COLLECTION, limit=100)[0]]
    )
    client.upsert(collection_name=QA_COLLECTION, points=points)
    print(f"✅  Ingested {len(points)} QA pairs into '{QA_COLLECTION}'")


if __name__ == "__main__":
    print("🚀 Starting ingestion with updated schema...")
    ingest_menu()
    ingest_golden_dataset()
    print("🎉 All data loaded into Qdrant!")

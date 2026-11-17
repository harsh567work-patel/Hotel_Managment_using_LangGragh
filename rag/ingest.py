"""
Ingest restaurant knowledge base (menu.json + golden_dataset.json) into Qdrant.
Run once before starting the app:  python -m rag.ingest
"""

import json
import uuid
import sys
from pathlib import Path
from qdrant_client.models import PointStruct

# Allow running as __main__
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from rag.qdrant_client import (
    get_client, embed_text,
    ensure_collection,
    MENU_COLLECTION, QA_COLLECTION,
)

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def ingest_menu() -> None:
    """Load menu.json and upsert each dish as a Qdrant point."""
    ensure_collection(MENU_COLLECTION)
    client = get_client()

    with open(DATA_DIR / "menu.json", "r", encoding="utf-8") as f:
        data = json.load(f)

    points = []
    for dish in data["menu"]:
        # Text to embed: rich description for semantic search
        embed_str = (
            f"{dish['dish_name']} — {dish['description']} "
            f"Category: {dish['category']}. Tags: {', '.join(dish.get('tags', []))}."
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
                    "price":             dish["price"],
                    "prep_time_minutes": dish["prep_time_minutes"],
                    "is_available":      dish["is_available"],
                    "restaurant_name":   data["restaurant"]["name"],
                    "dish_man":          dish["dish_man"],
                    "tags":              dish.get("tags", []),
                },
            )
        )

    client.upsert(collection_name=MENU_COLLECTION, points=points)
    print(f"✅  Ingested {len(points)} dishes into '{MENU_COLLECTION}'")


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

    client.upsert(collection_name=QA_COLLECTION, points=points)
    print(f"✅  Ingested {len(points)} QA pairs into '{QA_COLLECTION}'")


if __name__ == "__main__":
    print("🚀 Starting ingestion...")
    ingest_menu()
    ingest_golden_dataset()
    print("🎉 All data loaded into Qdrant!")

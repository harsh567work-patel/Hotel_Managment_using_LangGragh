"""
Qdrant client wrapper — handles connection, collection creation, and semantic search.
Uses FastEmbed for local embedding (no extra API key needed).
"""

import os
from typing import List, Optional
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct, Filter,
    FieldCondition, MatchValue
)
from fastembed import TextEmbedding
from dotenv import load_dotenv

load_dotenv()

MENU_COLLECTION = "restaurant_menu"
QA_COLLECTION = "qa_golden_dataset"
VECTOR_SIZE = 384   # all-MiniLM-L6-v2 dimension via FastEmbed

_embedder: Optional[TextEmbedding] = None
_client: Optional[QdrantClient] = None


def get_embedder() -> TextEmbedding:
    global _embedder
    if _embedder is None:
        _embedder = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    return _embedder


def get_client() -> QdrantClient:
    global _client
    if _client is None:
        url = os.getenv("QDRANT_URL")
        api_key = os.getenv("QDRANT_API_KEY")
        if url and api_key:
            _client = QdrantClient(url=url, api_key=api_key)
        else:
            # Fallback: in-memory Qdrant (dev / testing)
            _client = QdrantClient(":memory:")
    return _client


def embed_text(text: str) -> List[float]:
    embedder = get_embedder()
    vectors = list(embedder.embed([text]))
    return vectors[0].tolist()


def ensure_collection(collection_name: str) -> None:
    client = get_client()
    existing = [c.name for c in client.get_collections().collections]
    if collection_name not in existing:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
    # Ensure payload index on is_available for filtering
    if collection_name == MENU_COLLECTION:
        try:
            client.create_payload_index(
                collection_name=collection_name,
                field_name="is_available",
                field_schema="bool",
            )
        except Exception:
            pass
        try:
            client.create_payload_index(
                collection_name=collection_name,
                field_name="category",
                field_schema="keyword",
            )
        except Exception:
            pass


def search_menu(
    query: str,
    top_k: int = 5,
    only_available: bool = False,
    category: Optional[str] = None,
) -> List[dict]:
    """Semantic search over restaurant_menu collection."""
    client = get_client()
    query_vector = embed_text(query)

    # We fetch a larger candidate pool and filter in-memory to guarantee resilience
    limit = top_k * 3 if (only_available or category) else top_k

    if hasattr(client, "query_points"):
        response = client.query_points(
            collection_name=MENU_COLLECTION,
            query=query_vector,
            limit=limit,
            with_payload=True,
        )
        hits = [hit.payload for hit in response.points if hit.payload]
    else:
        results = client.search(
            collection_name=MENU_COLLECTION,
            query_vector=query_vector,
            limit=limit,
            with_payload=True,
        )
        hits = [hit.payload for hit in results if hit.payload]

    # Filter in Python
    filtered = []
    for d in hits:
        if only_available and not d.get("is_available", False):
            continue
        if category and d.get("category", "").lower() != category.lower():
            continue
        filtered.append(d)
        if len(filtered) >= top_k:
            break

    return filtered


def search_qa(query: str, top_k: int = 3) -> List[dict]:
    """Search golden dataset — used by evaluator."""
    client = get_client()
    query_vector = embed_text(query)
    if hasattr(client, "query_points"):
        response = client.query_points(
            collection_name=QA_COLLECTION,
            query=query_vector,
            limit=top_k,
            with_payload=True,
        )
        return [hit.payload for hit in response.points if hit.payload]
    else:
        results = client.search(
            collection_name=QA_COLLECTION,
            query_vector=query_vector,
            limit=top_k,
            with_payload=True,
        )
        return [hit.payload for hit in results if hit.payload]

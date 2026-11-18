"""
qdrant_client.py — Qdrant client connection, embedding, payload indexing, and search.
Uses FastEmbed for local vectorization and enforces strict startup validation.
"""

import time
from typing import List, Optional, Tuple, Dict, Any
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct, Filter,
    FieldCondition, MatchValue
)
from fastembed import TextEmbedding

from config import (
    QDRANT_URL, QDRANT_API_KEY,
    MENU_COLLECTION, QA_COLLECTION,
    VECTOR_SIZE, EMBEDDING_MODEL,
    DISH_MATCH_THRESHOLD
)

_embedder: Optional[TextEmbedding] = None
_client: Optional[QdrantClient] = None
_menu_cache: Optional[List[dict]] = None
_menu_cache_ts: float = 0.0
_MENU_CACHE_TTL: float = 60.0


def get_embedder() -> TextEmbedding:
    """Returns singleton FastEmbed text embedder."""
    global _embedder
    if _embedder is None:
        _embedder = TextEmbedding(model_name=EMBEDDING_MODEL)
    return _embedder


def get_client() -> QdrantClient:
    """
    Returns singleton QdrantClient instance.
    Raises RuntimeError if credentials are unset or invalid.
    """
    global _client
    if _client is None:
        if not QDRANT_URL or not QDRANT_API_KEY:
            raise RuntimeError(
                "Qdrant configuration error: QDRANT_URL and QDRANT_API_KEY must be set in .env. "
                "Silent in-memory fallback is disabled in production."
            )
        try:
            _client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=10)
            # Verify connectivity
            _client.get_collections()
        except Exception as e:
            raise RuntimeError(
                f"Failed to connect to Qdrant cluster at {QDRANT_URL}: {str(e)}. "
                "Please check your network and API key."
            )
    return _client


def verify_collections_ready() -> None:
    """
    Verifies that the menu collection exists and is not empty.
    Raises RuntimeError with clear remediation instructions if not.
    """
    client = get_client()
    collections = [c.name for c in client.get_collections().collections]
    if MENU_COLLECTION not in collections:
        raise RuntimeError(
            f"Collection '{MENU_COLLECTION}' not found in Qdrant. "
            "Please run 'python -m rag.ingest' to load the restaurant knowledge base."
        )
    count = client.count(collection_name=MENU_COLLECTION).count
    if count == 0:
        raise RuntimeError(
            f"Collection '{MENU_COLLECTION}' is empty. "
            "Please run 'python -m rag.ingest' to index menu data."
        )


def embed_text(text: str) -> List[float]:
    """Generates embedding vector for text using FastEmbed."""
    embedder = get_embedder()
    vectors = list(embedder.embed([text]))
    return vectors[0].tolist()


def ensure_collection(collection_name: str) -> None:
    """Ensures collection exists with proper vector size and payload indices."""
    client = get_client()
    existing = [c.name for c in client.get_collections().collections]
    if collection_name not in existing:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )

    # Payload indexes for filtering
    if collection_name == MENU_COLLECTION:
        for field, schema in [
            ("is_available", "bool"),
            ("is_veg", "bool"),
            ("category", "keyword"),
            ("price", "float"),
            ("dish_name", "keyword"),
        ]:
            try:
                client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field,
                    field_schema=schema,
                )
            except Exception:
                pass


def search_menu(
    query: str,
    top_k: int = 5,
    only_available: bool = False,
    category: Optional[str] = None,
    is_veg: Optional[bool] = None,
) -> List[dict]:
    """
    Semantic search over the restaurant_menu collection with payload filters.
    """
    client = get_client()
    query_vector = embed_text(query)

    # Build Qdrant must filters
    must_conditions = []
    if only_available:
        must_conditions.append(FieldCondition(key="is_available", match=MatchValue(value=True)))
    if category:
        must_conditions.append(FieldCondition(key="category", match=MatchValue(value=category)))
    if is_veg is not None:
        must_conditions.append(FieldCondition(key="is_veg", match=MatchValue(value=is_veg)))

    qdrant_filter = Filter(must=must_conditions) if must_conditions else None

    if hasattr(client, "query_points"):
        response = client.query_points(
            collection_name=MENU_COLLECTION,
            query=query_vector,
            query_filter=qdrant_filter,
            limit=top_k,
            with_payload=True,
        )
        return [hit.payload for hit in response.points if hit.payload]
    else:
        results = client.search(
            collection_name=MENU_COLLECTION,
            query_vector=query_vector,
            query_filter=qdrant_filter,
            limit=top_k,
            with_payload=True,
        )
        return [hit.payload for hit in results if hit.payload]


def search_dish_by_name(dish_name: str, threshold: float = DISH_MATCH_THRESHOLD) -> Tuple[Optional[dict], float]:
    """
    Finds the top-1 matching dish from the FULL menu by name embedding.
    Returns (matched_payload, score). If score < threshold, returns (None, score).
    """
    client = get_client()
    query_vector = embed_text(dish_name)

    if hasattr(client, "query_points"):
        response = client.query_points(
            collection_name=MENU_COLLECTION,
            query=query_vector,
            limit=1,
            with_payload=True,
        )
        points = response.points
    else:
        points = client.search(
            collection_name=MENU_COLLECTION,
            query_vector=query_vector,
            limit=1,
            with_payload=True,
        )

    if not points or not points[0].payload:
        return None, 0.0

    best_hit = points[0]
    score = getattr(best_hit, "score", 1.0)
    if score >= threshold:
        return best_hit.payload, score
    return None, score


def get_all_menu_dishes(force_refresh: bool = False) -> List[dict]:
    """
    Returns all dishes in the menu collection (payloads), with a short TTL cache
    so menu lookups and cart resolution stay snappy.
    """
    global _menu_cache, _menu_cache_ts
    now = time.time()
    if not force_refresh and _menu_cache is not None and (now - _menu_cache_ts) < _MENU_CACHE_TTL:
        return _menu_cache

    client = get_client()
    # Scroll through collection
    scroll_res = client.scroll(
        collection_name=MENU_COLLECTION,
        limit=200,
        with_payload=True,
    )
    records = scroll_res[0]
    _menu_cache = [r.payload for r in records if r.payload]
    _menu_cache_ts = now
    return _menu_cache


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

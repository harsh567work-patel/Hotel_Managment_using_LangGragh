"""
api.py — Production-grade FastAPI REST API for SpiceRoute Kitchen.
Exposes the compiled LangGraph food_graph for web apps, mobile clients, WhatsApp/SMS bots,
and microservice integrations.

Endpoints:
  - GET  /health          : Service health & Qdrant/Groq readiness check
  - GET  /menu            : Retrieve full menu with optional category/dietary filtering
  - POST /chat            : Chat turn with in-memory session state persistence
  - GET  /orders/active   : List all currently cooking orders across sessions
  - POST /orders/track    : Query real-time status & remaining preparation time of an order
  - GET  /eval/summary    : Retrieve latest 50-pair Golden Dataset evaluation metrics
"""

import os
import time
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage, AIMessage

from config import (
    GROQ_MODEL,
    GROQ_API_KEY,
    QDRANT_URL,
    SERVICEABLE_CITIES,
    RESTAURANT_LOCATION,
    MAX_ACTIVE_ORDERS,
)
from graph.graph_builder import food_graph
from graph.state import OrderState, CartItem, OrderRecord, DeliveryAddress
from graph.orders import (
    sync_orders,
    get_active_cooking_orders,
    get_remaining_seconds,
    format_remaining,
)
from rag.qdrant_client import get_all_menu_dishes, get_client, MENU_COLLECTION
from table_split.api_dietary import router as dietary_router


# ── FastAPI App Configuration ────────────────────────────────────────────────
app = FastAPI(
    title="SpiceRoute Kitchen — LangGraph Food Ordering API",
    description=(
        "Production AI food-ordering backend powered by LangGraph, Groq LLMs, FastEmbed, "
        "and Qdrant Vector Cloud. Features state-aware intent classification, deterministic "
        "superlative filtering, concurrent kitchen order tracking, and strict single-message turns."
    ),
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Enable CORS for web/mobile frontend deployment
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount Table Split Dietary & Verification endpoints (Stage 3)
app.include_router(dietary_router)

# In-memory session store: session_id -> OrderState
_sessions: Dict[str, OrderState] = {}


def _get_or_create_session(session_id: str) -> OrderState:
    """Retrieves an existing session state or initializes a clean OrderState."""
    if session_id not in _sessions:
        _sessions[session_id] = {
            "messages": [],
            "intent": "",
            "intents": [],
            "user_query": "",
            "retrieved_menu": [],
            "unavailable_items": [],
            "cart": [],
            "total_price": 0.0,
            "max_prep_time": 0,
            "order_type": "delivery",
            "table_number": None,
            "delivery_address": None,
            "pending_action": None,
            "order_confirmed": False,
            "edit_requested": False,
            "order_id": None,
            "estimated_delivery_minutes": 0,
            "orders": [],
            "fallback_count": 0,
            "backtrack_count": 0,
            "error": None,
        }
    return _sessions[session_id]


# ── Pydantic Request & Response Schemas ──────────────────────────────────────
class ChatRequest(BaseModel):
    message: str = Field(..., description="User message text (e.g. 'I want 2 butter chicken', 'checkout')")
    session_id: str = Field(default="default_session", description="Unique conversation or client session ID")
    time_scale: float = Field(default=1.0, ge=1.0, description="Time scale for order timer sync (1.0 = real-time)")


class CartItemResponse(BaseModel):
    dish_id: str
    dish_name: str
    quantity: int
    unit_price: float
    prep_time_minutes: int
    category: str


class OrderRecordResponse(BaseModel):
    order_id: str
    order_type: str
    status: str
    total_price: float
    eta_minutes: int
    remaining_seconds: int
    remaining_formatted: str
    table_number: Optional[str] = None
    placed_at: float


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    intents: List[str]
    primary_intent: str
    order_type: Optional[str]
    pending_action: Optional[str]
    cart: List[CartItemResponse]
    total_price: float
    max_prep_time: int
    active_orders_count: int
    latest_order_id: Optional[str]


class TrackOrderRequest(BaseModel):
    order_id: Optional[str] = Field(default=None, description="Specific Order ID to track (e.g. 'ORD-ABCD1234')")
    session_id: Optional[str] = Field(default="default_session", description="Session ID to track orders for")
    time_scale: float = Field(default=1.0, ge=1.0)


class HealthResponse(BaseModel):
    status: str
    model: str
    qdrant_url: str
    qdrant_connected: bool
    menu_items_indexed: int
    serviceable_cities: List[str]
    restaurant_location: str
    max_active_orders_per_customer: int


# ── REST Endpoints ───────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse, tags=["System"])
def health_check():
    """
    Validates backend connectivity to Groq LLM configuration and Qdrant Cloud.
    """
    qdrant_ok = False
    items_count = 0
    try:
        client = get_client()
        collections = [c.name for c in client.get_collections().collections]
        if MENU_COLLECTION in collections:
            qdrant_ok = True
            items_count = client.count(collection_name=MENU_COLLECTION).count
    except Exception:
        qdrant_ok = False

    return HealthResponse(
        status="healthy" if (qdrant_ok and bool(GROQ_API_KEY)) else "degraded",
        model=GROQ_MODEL,
        qdrant_url=QDRANT_URL or "not_configured",
        qdrant_connected=qdrant_ok,
        menu_items_indexed=items_count,
        serviceable_cities=SERVICEABLE_CITIES,
        restaurant_location=RESTAURANT_LOCATION,
        max_active_orders_per_customer=MAX_ACTIVE_ORDERS,
    )


@app.get("/menu", tags=["Menu"])
def get_menu(
    category: Optional[str] = Query(None, description="Filter by category: Starters, Main Course, Breads, Drinks, Desserts"),
    diet: Optional[str] = Query(None, description="Filter by diet: veg, non-veg, vegan"),
    available_only: bool = Query(False, description="Filter only currently available items"),
):
    """
    Returns the comprehensive restaurant menu with optional category, dietary, and availability filters.
    """
    dishes = get_all_menu_dishes(force_refresh=False)
    if not dishes:
        raise HTTPException(status_code=503, detail="Menu database is currently empty. Run ingest script.")

    filtered = dishes
    if category:
        filtered = [d for d in filtered if d.get("category", "").lower() == category.lower()]
    if diet:
        d_lower = diet.lower()
        if d_lower == "veg":
            filtered = [d for d in filtered if d.get("is_veg", False)]
        elif d_lower in ("non-veg", "nonveg"):
            filtered = [d for d in filtered if not d.get("is_veg", True)]
        elif d_lower == "vegan":
            filtered = [d for d in filtered if "vegan" in d.get("tags", [])]
    if available_only:
        filtered = [d for d in filtered if d.get("is_available", False)]

    return {
        "count": len(filtered),
        "total_dishes_in_menu": len(dishes),
        "dishes": filtered,
    }


@app.post("/chat", response_model=ChatResponse, tags=["Ordering Chat"])
def chat_turn(req: ChatRequest):
    """
    Processes a user turn through the LangGraph StateGraph pipeline.
    Maintains conversation session state, updates cart and order history,
    and returns EXACTLY ONE cohesive assistant message.
    """
    state = _get_or_create_session(req.session_id)

    # Sync order statuses before processing turn
    current_time = time.time()
    state["orders"] = sync_orders(state.get("orders", []), now=current_time, time_scale=req.time_scale)

    prev_msg_count = len(state.get("messages", []))
    state["messages"] = state.get("messages", []) + [HumanMessage(content=req.message.strip())]

    try:
        new_state = food_graph.invoke(state)
        # Update session store with graph results
        _sessions[req.session_id] = new_state
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Graph execution failed: {str(e)}")

    # Extract the single assistant message produced in this turn
    new_ai = [
        m for m in new_state.get("messages", [])[prev_msg_count:]
        if isinstance(m, AIMessage)
    ]
    reply_text = new_ai[-1].content if new_ai else "I am here to help you order food. How may I assist you?"

    # Active cooking orders
    active_orders = get_active_cooking_orders(new_state.get("orders", []), now=current_time, time_scale=req.time_scale)

    cart_items = [
        CartItemResponse(
            dish_id=c.get("dish_id", ""),
            dish_name=c.get("dish_name", ""),
            quantity=int(c.get("quantity", 1)),
            unit_price=float(c.get("unit_price", 0.0)),
            prep_time_minutes=int(c.get("prep_time_minutes", 15)),
            category=c.get("category", "Main Course"),
        )
        for c in new_state.get("cart", [])
    ]

    return ChatResponse(
        session_id=req.session_id,
        reply=reply_text,
        intents=new_state.get("intents", [new_state.get("intent", "")]),
        primary_intent=new_state.get("intent", "menu_inquiry"),
        order_type=new_state.get("order_type", "delivery"),
        pending_action=new_state.get("pending_action"),
        cart=cart_items,
        total_price=float(new_state.get("total_price", 0.0)),
        max_prep_time=int(new_state.get("max_prep_time", 0)),
        active_orders_count=len(active_orders),
        latest_order_id=new_state.get("order_id"),
    )


@app.post("/orders/track", tags=["Order Management"])
def track_orders(req: TrackOrderRequest):
    """
    Returns real-time status, countdown timer, and items for active or served orders.
    """
    state = _sessions.get(req.session_id)
    if not state or not state.get("orders"):
        raise HTTPException(status_code=404, detail="No orders found for this session ID.")

    current_time = time.time()
    synced = sync_orders(state.get("orders", []), now=current_time, time_scale=req.time_scale)
    state["orders"] = synced

    target_orders = synced
    if req.order_id:
        target_orders = [o for o in synced if o.get("order_id") == req.order_id.strip()]
        if not target_orders:
            raise HTTPException(status_code=404, detail=f"Order '{req.order_id}' not found.")

    response_records = []
    for o in target_orders:
        rem_sec = get_remaining_seconds(o, now=current_time, time_scale=req.time_scale)
        response_records.append(
            OrderRecordResponse(
                order_id=o["order_id"],
                order_type=o.get("order_type", "delivery"),
                status=o.get("status", "cooking"),
                total_price=float(o.get("total_price", 0.0)),
                eta_minutes=int(o.get("eta_minutes", 20)),
                remaining_seconds=rem_sec,
                remaining_formatted=format_remaining(rem_sec),
                table_number=o.get("table_number"),
                placed_at=float(o.get("placed_at", current_time)),
            )
        )

    return {
        "session_id": req.session_id,
        "count": len(response_records),
        "orders": response_records,
    }


@app.get("/orders/active", tags=["Order Management"])
def get_all_active_orders(time_scale: float = Query(1.0, ge=1.0)):
    """
    Kitchen dashboard endpoint: lists all cooking orders across all sessions in the system.
    """
    current_time = time.time()
    all_cooking = []

    for session_id, state in _sessions.items():
        synced = sync_orders(state.get("orders", []), now=current_time, time_scale=time_scale)
        state["orders"] = synced
        for o in synced:
            if o.get("status") == "cooking":
                rem_sec = get_remaining_seconds(o, now=current_time, time_scale=time_scale)
                all_cooking.append({
                    "session_id": session_id,
                    "order_id": o["order_id"],
                    "order_type": o.get("order_type", "delivery"),
                    "table_number": o.get("table_number"),
                    "total_price": o.get("total_price"),
                    "remaining_seconds": rem_sec,
                    "remaining_formatted": format_remaining(rem_sec),
                    "items": o.get("items", []),
                })

    return {
        "active_cooking_orders_count": len(all_cooking),
        "orders": all_cooking,
    }


@app.delete("/sessions/{session_id}", tags=["Session Management"])
def reset_session(session_id: str):
    """
    Resets the session state for a given session ID (clears cart, active orders, and messages).
    """
    if session_id in _sessions:
        del _sessions[session_id]
        return {"status": "success", "message": f"Session '{session_id}' cleared."}
    return {"status": "not_found", "message": f"Session '{session_id}' was not active."}

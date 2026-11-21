"""
OrderState — single source of truth flowing through every LangGraph node.
Includes delivery address, pending state tracking, and multi-intent support.
"""

from typing import TypedDict, List, Optional, Dict, Annotated
from langchain_core.messages import BaseMessage
import operator


class CartItem(TypedDict):
    dish_id: str
    dish_name: str
    quantity: int
    unit_price: float
    prep_time_minutes: int
    category: str


class DeliveryAddress(TypedDict, total=False):
    street: str
    city: str
    state: str


class OrderRecord(TypedDict, total=False):
    order_id: str
    items: List[CartItem]
    total_price: float
    order_type: str                 # "delivery" | "dine_in" | "pickup"
    table_number: Optional[str]
    delivery_address: Optional[DeliveryAddress]
    eta_minutes: int
    placed_at: float                # unix timestamp (seconds)
    status: str                     # "cooking" | "served"


class OrderState(TypedDict, total=False):
    # ── Conversation ───────────────────────────────────────────────
    messages: Annotated[List[BaseMessage], operator.add]  # append-only

    # ── Intent ─────────────────────────────────────────────────────
    intent: str                 # primary intent
    intents: List[str]          # all detected intents in order (for multi-intent turns)

    # ── RAG ────────────────────────────────────────────────────────
    user_query: str                     # current user message query text
    retrieved_menu: List[dict]          # top-k docs from Qdrant
    unavailable_items: List[str]        # dish names marked unavailable

    # ── Cart ───────────────────────────────────────────────────────
    cart: List[CartItem]
    total_price: float
    max_prep_time: int          # minutes — kitchen preparation time

    # ── Delivery & Checkout State ──────────────────────────────────
    order_type: Optional[str]      # "delivery" | "dine_in" | "pickup" | None
    table_number: Optional[str]    # optional table number for dine-in
    delivery_address: Optional[DeliveryAddress]
    pending_action: Optional[str]  # e.g., "awaiting_order_confirmation", or None

    # ── Order ──────────────────────────────────────────────────────
    order_confirmed: bool
    edit_requested: bool
    order_id: Optional[str]
    estimated_delivery_minutes: int
    orders: List[OrderRecord]      # persistent history of active (cooking) and served orders

    # ── Control ────────────────────────────────────────────────────
    fallback_count: int
    backtrack_count: int
    error: Optional[str]

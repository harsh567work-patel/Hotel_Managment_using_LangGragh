"""
OrderState — single source of truth flowing through every LangGraph node.
"""

from typing import TypedDict, List, Optional, Annotated
from langchain_core.messages import BaseMessage
import operator


class CartItem(TypedDict):
    dish_id: str
    dish_name: str
    quantity: int
    unit_price: float
    prep_time_minutes: int
    category: str


class OrderState(TypedDict):
    # ── Conversation ───────────────────────────────────────────────
    messages: Annotated[List[BaseMessage], operator.add]  # append-only

    # ── Intent ─────────────────────────────────────────────────────
    intent: str           # greet | order | track | out_of_scope | unclear

    # ── RAG ────────────────────────────────────────────────────────
    user_query: str                     # current dish query text
    retrieved_menu: List[dict]          # top-k docs from Qdrant
    unavailable_items: List[str]        # dish names marked unavailable

    # ── Cart ───────────────────────────────────────────────────────
    cart: List[CartItem]
    total_price: float
    max_prep_time: int   # minutes — determines ETA

    # ── Order ──────────────────────────────────────────────────────
    order_confirmed: bool
    edit_requested: bool
    order_id: Optional[str]
    estimated_delivery_minutes: int

    # ── Control ────────────────────────────────────────────────────
    fallback_count: int   # guard against infinite loops
    backtrack_count: int  # guard against infinite dish-swap loops
    error: Optional[str]

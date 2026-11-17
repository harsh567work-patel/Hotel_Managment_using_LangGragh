"""
cart_builder_node — manages cart modifications (add, remove, update quantity, clear)
using structured output (CartActions) and resolving dish names against the FULL menu
in Qdrant (top-1 vector match with score threshold).
Uses real newlines, markdown lists, with Total, Prep time, and Address each on its own line.
Never crashes on unknown dishes; produces exactly ONE assistant message.
"""

from typing import Literal, Optional, List, Dict, Any, Tuple
from pydantic import BaseModel, Field
from langchain_groq import ChatGroq
from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
from graph.state import OrderState, CartItem
from config import GROQ_MODEL, GROQ_FAST_MODEL, GROQ_API_KEY, DISH_MATCH_THRESHOLD
from rag.qdrant_client import search_dish_by_name, get_all_menu_dishes, search_menu


class CartAction(BaseModel):
    """An action to perform on the cart."""
    action: Literal["add", "remove", "update", "clear"] = Field(
        description="The action: 'add' new dish, 'remove' dish, 'update' quantity, or 'clear' all"
    )
    dish_name: Optional[str] = Field(
        default=None,
        description="Name of the dish (e.g., 'butter chicken', 'garlic naan')"
    )
    quantity: int = Field(
        default=1,
        description="Quantity of the item (positive integer)"
    )


class CartActions(BaseModel):
    """Container for multiple cart actions in one message."""
    actions: List[CartAction] = Field(
        description="List of cart actions to execute"
    )


CART_SYSTEM_PROMPT = """You are the cart assistant for SpiceRoute Kitchen.
Extract all cart actions from the user's message.
Actions can be:
- add: user wants to order or add an item (e.g., "I want 2 butter chicken and 1 lassi")
- remove: user wants to drop or remove an item (e.g., "remove garlic naan", "cancel the samosa")
- update: user wants to change quantity (e.g., "change butter chicken to 3", "make it 1 instead")
- clear: user wants to empty the whole cart (e.g., "clear cart", "empty cart", "remove everything")

Extract the dish names and quantities accurately. Always call the CartActions tool.
"""


def _resolve_dish(dish_name_raw: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Resolves dish against full menu using exact/substring match first,
    then top-1 vector search against Qdrant above threshold.
    Returns (matched_dish_dict, status_message).
    """
    if not dish_name_raw:
        return None, "No dish specified."

    all_dishes = get_all_menu_dishes()
    dish_name_clean = dish_name_raw.strip().lower()

    # 1. Exact or substring match in full menu
    for dish in all_dishes:
        name_lower = dish["dish_name"].lower()
        if dish_name_clean == name_lower or dish_name_clean in name_lower or name_lower in dish_name_clean:
            return dish, "exact_match"

    # 2. Vector search against full Qdrant menu collection
    best_dish, score = search_dish_by_name(dish_name_raw, threshold=DISH_MATCH_THRESHOLD)
    if best_dish:
        return best_dish, f"vector_match (score={score:.2f})"

    return None, f"Not found above threshold (score={score:.2f})"


def apply_cart_actions(current_cart: List[CartItem], actions: List[CartAction]) -> Tuple[List[CartItem], List[str], List[str]]:
    """
    Pure Python reducer that applies CartAction list to current cart.
    Returns (updated_cart, success_notes, error_notes).
    """
    cart = [dict(item) for item in current_cart]
    success_notes = []
    error_notes = []

    for act in actions:
        if act.action == "clear":
            cart.clear()
            success_notes.append("Emptied the cart.")
            continue

        if not act.dish_name:
            error_notes.append(f"Couldn't determine which dish you wanted to {act.action}.")
            continue

        dish, status = _resolve_dish(act.dish_name)
        if not dish:
            error_notes.append(
                f"I couldn't find '{act.dish_name}' on our menu. Please check the menu or ask me for suggestions!"
            )
            continue

        dish_name = dish["dish_name"]
        is_available = dish.get("is_available", True)

        # Check availability for additions
        if act.action == "add" and not is_available:
            alts = search_menu(
                query=f"{dish.get('category')} {dish_name}",
                top_k=2,
                only_available=True,
                category=dish.get("category"),
                is_veg=dish.get("is_veg"),
            )
            alt_names = ", ".join([a["dish_name"] for a in alts if a["dish_name"] != dish_name])
            msg = f"Sorry, **{dish_name}** is currently unavailable today."
            if alt_names:
                msg += f" Can I suggest **{alt_names}** instead?"
            error_notes.append(msg)
            continue

        # Existing item in cart
        existing = next((c for c in cart if c["dish_name"].lower() == dish_name.lower()), None)

        if act.action == "add":
            qty = max(1, act.quantity)
            if existing:
                existing["quantity"] += qty
                success_notes.append(f"Added {qty} more **{dish_name}** (total: {existing['quantity']})")
            else:
                cart.append(CartItem(
                    dish_id=dish["dish_id"],
                    dish_name=dish["dish_name"],
                    quantity=qty,
                    unit_price=float(dish["price"]),
                    prep_time_minutes=int(dish["prep_time_minutes"]),
                    category=dish.get("category", "Main Course"),
                ))
                success_notes.append(f"Added **{dish_name}** x{qty} to your cart")

        elif act.action == "remove":
            if existing:
                cart.remove(existing)
                success_notes.append(f"Removed **{dish_name}** from your cart")
            else:
                error_notes.append(f"**{dish_name}** was not in your cart.")

        elif act.action == "update":
            qty = act.quantity
            if qty <= 0:
                if existing:
                    cart.remove(existing)
                    success_notes.append(f"Removed **{dish_name}** from your cart")
            else:
                if existing:
                    existing["quantity"] = qty
                    success_notes.append(f"Updated **{dish_name}** quantity to {qty}")
                else:
                    if is_available:
                        cart.append(CartItem(
                            dish_id=dish["dish_id"],
                            dish_name=dish["dish_name"],
                            quantity=qty,
                            unit_price=float(dish["price"]),
                            prep_time_minutes=int(dish["prep_time_minutes"]),
                            category=dish.get("category", "Main Course"),
                        ))
                        success_notes.append(f"Added **{dish_name}** x{qty} to your cart")
                    else:
                        error_notes.append(f"Sorry, **{dish_name}** is currently unavailable today.")

    return cart, success_notes, error_notes


def cart_builder_node(state: OrderState) -> dict:
    """
    Extracts cart actions using structured outputs, applies changes to cart in state,
    and returns exactly ONE descriptive AIMessage.
    Uses real newlines, markdown lists, with Total, Prep time, and Address each on its own line.
    """
    last_human = ""
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            last_human = m.content
            break

    llm = ChatGroq(
        model=GROQ_MODEL,
        temperature=0,
        api_key=GROQ_API_KEY,
    )
    structured_llm = llm.with_structured_output(CartActions)

    try:
        extracted = structured_llm.invoke([
            SystemMessage(content=CART_SYSTEM_PROMPT),
            HumanMessage(content=last_human),
        ])
        actions = extracted.actions
    except Exception:
        fallback_llm = ChatGroq(
            model=GROQ_FAST_MODEL,
            temperature=0,
            api_key=GROQ_API_KEY,
        ).with_structured_output(CartActions)
        try:
            extracted = fallback_llm.invoke([
                SystemMessage(content=CART_SYSTEM_PROMPT),
                HumanMessage(content=last_human),
            ])
            actions = extracted.actions
        except Exception:
            actions = []

    current_cart = state.get("cart", [])
    new_cart, success_notes, error_notes = apply_cart_actions(current_cart, actions)

    # Recalculate totals
    total_price = sum(c["unit_price"] * c["quantity"] for c in new_cart)
    max_prep_time = max((c["prep_time_minutes"] for c in new_cart), default=0)

    # Preserve or update pending_action
    pending = state.get("pending_action")
    if not new_cart:
        pending = None
    elif pending == "awaiting_order_confirmation":
        pending = "awaiting_order_confirmation"

    # Build response message
    msg_parts = []
    if success_notes:
        msg_parts.append("✅ " + "\n✅ ".join(success_notes))
    if error_notes:
        msg_parts.append("⚠️ " + "\n⚠️ ".join(error_notes))

    if new_cart:
        cart_lines = [
            f"  • {c['dish_name']} x{c['quantity']} — ₹{c['unit_price'] * c['quantity']:.0f}"
            for c in new_cart
        ]
        address = state.get("delivery_address")
        if address and address.get("street"):
            addr_str = f"{address.get('street')}, {address.get('city')}, {address.get('state', 'Karnataka')}"
        else:
            addr_str = "Not set yet (Provide address at checkout)"

        msg_parts.append(
            "🛒 **Your Cart:**\n\n" +
            "\n".join(cart_lines) + "\n\n" +
            f"💰 **Total:** ₹{total_price:.0f}\n" +
            f"⏱️ **Prep time:** ~{max_prep_time} min\n" +
            f"📍 **Address:** {addr_str}\n\n" +
            "Would you like to add anything else, or say **'checkout'** to place your order?"
        )
    else:
        if not success_notes and not error_notes:
            msg_parts.append("I couldn't identify any dishes to add. What would you like to order?")
        else:
            msg_parts.append("Your cart is currently empty. Tell me what you'd like to eat!")

    return {
        "cart": new_cart,
        "total_price": total_price,
        "max_prep_time": max_prep_time,
        "pending_action": pending,
        "messages": [AIMessage(content="\n\n".join(msg_parts))],
    }

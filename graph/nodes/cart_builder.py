"""
cart_builder_node — parses user selection, adds dishes to cart, computes totals.
Handles duplicate items (increments qty) and empty cart guard.
"""

import os
import re
from langchain_groq import ChatGroq
from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
from dotenv import load_dotenv
from graph.state import OrderState, CartItem

load_dotenv()

PARSE_PROMPT = """You are a cart management assistant for SpiceRoute Kitchen.
Given the user's message and a list of available dishes, extract WHICH dishes the user wants to add and in WHAT QUANTITY.

Reply in this exact format (one dish per line, nothing else):
DISH_NAME | QUANTITY

If no specific dish is mentioned, reply: NONE

Available dishes:
{dish_list}

User message: {user_msg}"""


def cart_builder_node(state: OrderState) -> dict:
    """Add dishes to cart based on user selection from retrieved_menu."""
    llm = ChatGroq(
        model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
        temperature=0,
        api_key=os.getenv("GROQ_API_KEY"),
    )

    retrieved = state.get("retrieved_menu", [])
    if not retrieved:
        msg = "Your cart is empty! Tell me what you'd like to order and I'll find it for you."
        return {"messages": [AIMessage(content=msg)]}

    # Build dish list for LLM context
    dish_list = "\n".join([
        f"- {d['dish_name']} | ₹{d['price']} | {d['prep_time_minutes']} min"
        for d in retrieved
    ])
    last_human = next(
        (m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage)),
        ""
    )

    # Ask LLM to parse the selection
    response = llm.invoke([
        SystemMessage(content=PARSE_PROMPT.format(
            dish_list=dish_list,
            user_msg=last_human,
        )),
    ])

    raw = response.content.strip()
    current_cart: list = list(state.get("cart", []))

    if raw.upper() == "NONE" or not raw:
        msg = "I didn't catch which dish you'd like. Could you specify the dish name?"
        return {"messages": [AIMessage(content=msg)]}

    # Build a lookup dict from retrieved menu
    menu_lookup = {d["dish_name"].lower(): d for d in retrieved}

    added = []
    for line in raw.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 2:
            continue
        dish_name_raw = parts[0].strip()
        try:
            qty = int(re.search(r"\d+", parts[1]).group())
        except (AttributeError, ValueError):
            qty = 1

        # Fuzzy match against retrieved menu
        matched = None
        for key, dish in menu_lookup.items():
            if dish_name_raw.lower() in key or key in dish_name_raw.lower():
                matched = dish
                break

        if not matched:
            continue

        # Check if already in cart — increment
        existing = next((c for c in current_cart if c["dish_name"] == matched["dish_name"]), None)
        if existing:
            existing["quantity"] += qty
        else:
            current_cart.append(CartItem(
                dish_id=matched["dish_id"],
                dish_name=matched["dish_name"],
                quantity=qty,
                unit_price=float(matched["price"]),
                prep_time_minutes=matched["prep_time_minutes"],
                category=matched["category"],
            ))
        added.append(f"{matched['dish_name']} x{qty}")

    if not added:
        msg = "I couldn't match that to any dish. Here are the options:\n" + dish_list
        return {"messages": [AIMessage(content=msg)]}

    # Recalculate totals
    total_price = sum(c["unit_price"] * c["quantity"] for c in current_cart)
    max_prep_time = max((c["prep_time_minutes"] for c in current_cart), default=0)

    # Cart summary
    cart_lines = [
        f"  • {c['dish_name']} x{c['quantity']}  —  ₹{c['unit_price'] * c['quantity']:.0f}"
        for c in current_cart
    ]
    msg = (
        f"🛒 Added: **{', '.join(added)}**\n\n"
        f"**Your Cart:**\n" + "\n".join(cart_lines) +
        f"\n\n**Total: ₹{total_price:.0f}** | ⏱️ Ready in ~{max_prep_time} min\n\n"
        "Would you like to **add more**, **confirm order**, or **clear cart**?"
    )

    return {
        "cart": current_cart,
        "total_price": total_price,
        "max_prep_time": max_prep_time,
        "messages": [AIMessage(content=msg)],
    }

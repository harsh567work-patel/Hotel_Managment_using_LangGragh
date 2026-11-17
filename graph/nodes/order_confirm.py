"""
order_confirm_node — shows final cart summary and asks for explicit confirmation.
Detects: confirm | edit | cancel from user response.
"""

import os
from langchain_groq import ChatGroq
from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
from dotenv import load_dotenv
from graph.state import OrderState

load_dotenv()

CONFIRM_CLASSIFY_PROMPT = """The user is responding to an order confirmation request.
Classify their response as one of:
- confirm   : they agree, say yes, want to place order
- edit      : they want to change, add, or remove items
- cancel    : they want to cancel or don't want to order

Reply with ONLY one word: confirm, edit, or cancel.

User message: {msg}"""


def order_confirm_node(state: OrderState) -> dict:
    """Show cart summary and check if user confirms, edits, or cancels."""
    cart = state.get("cart", [])

    if not cart:
        msg = "Your cart is empty! Add some dishes first. What would you like to order?"
        return {
            "order_confirmed": False,
            "edit_requested": False,
            "messages": [AIMessage(content=msg)],
        }

    # Check if user just sent a confirm/edit/cancel response
    last_human = next(
        (m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage)),
        ""
    )

    # Only classify if there's a user response (not the first time we show summary)
    if last_human.strip():
        llm = ChatGroq(
            model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
            temperature=0,
            api_key=os.getenv("GROQ_API_KEY"),
        )
        response = llm.invoke([
            SystemMessage(content=CONFIRM_CLASSIFY_PROMPT.format(msg=last_human))
        ])
        decision = response.content.strip().lower()

        if decision == "confirm":
            return {
                "order_confirmed": True,
                "edit_requested": False,
                "messages": [AIMessage(content="✅ Confirmed! Placing your order now...")],
            }
        elif decision == "edit":
            return {
                "order_confirmed": False,
                "edit_requested": True,
                "messages": [AIMessage(content="Sure! What changes would you like to make?")],
            }
        elif decision == "cancel":
            return {
                "order_confirmed": False,
                "edit_requested": False,
                "cart": [],
                "total_price": 0.0,
                "messages": [AIMessage(content="❌ Order cancelled. Hope to see you again! Start a new order anytime.")],
            }

    # Show cart summary (first visit to this node)
    total_price = state.get("total_price", 0)
    max_prep = state.get("max_prep_time", 0)
    cart_lines = [
        f"  • {c['dish_name']} x{c['quantity']}  —  ₹{c['unit_price'] * c['quantity']:.0f}"
        for c in cart
    ]

    msg = (
        "📋 **Order Summary:**\n" +
        "\n".join(cart_lines) +
        f"\n\n💰 **Total: ₹{total_price:.0f}**  |  ⏱️ **Ready in ~{max_prep} min**\n\n"
        "Reply **'confirm'** to place order, **'edit'** to change items, or **'cancel'** to abort."
    )

    return {
        "order_confirmed": False,
        "edit_requested": False,
        "messages": [AIMessage(content=msg)],
    }

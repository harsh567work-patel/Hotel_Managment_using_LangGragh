"""
order_confirm_node — presents cart summary, displays delivery address status,
sets pending_action = "awaiting_order_confirmation", and requests confirmation.
Uses real newlines, markdown lists, with Total, Prep time, and Address each on its own line.
"""

from langchain_core.messages import AIMessage
from graph.state import OrderState


def order_confirm_node(state: OrderState) -> dict:
    """Shows checkout review, sets pending_action, and prompts for address/confirmation."""
    cart = state.get("cart", [])

    if not cart:
        msg = (
            "🛒 Your cart is currently empty!\n\n"
            "Please explore our menu or tell me what you'd like to eat before checking out."
        )
        return {
            "order_confirmed": False,
            "edit_requested": False,
            "pending_action": None,
            "messages": [AIMessage(content=msg)],
        }

    total_price = state.get("total_price", sum(c["unit_price"] * c["quantity"] for c in cart))
    max_prep = state.get("max_prep_time", max((c["prep_time_minutes"] for c in cart), default=20))
    address = state.get("delivery_address")

    cart_lines = [
        f"  • {c['dish_name']} x{c['quantity']} — ₹{c['unit_price'] * c['quantity']:.0f} (~{c['prep_time_minutes']} min)"
        for c in cart
    ]

    if address and address.get("street"):
        addr_str = f"{address.get('street')}, {address.get('city', 'Bangalore')}, {address.get('state', 'Karnataka')}"
        action_prompt = "Please reply **'confirm'** to place this order, or let me know if you want to make any changes!"
    else:
        addr_str = "Not provided yet"
        action_prompt = "Please provide your **delivery address** in Bangalore (e.g. *'12 MG Road, Bangalore'*) so we can confirm your order!"

    msg = (
        "📋 **Order Review & Checkout:**\n\n" +
        "\n".join(cart_lines) + "\n\n" +
        f"💰 **Total:** ₹{total_price:.0f}\n" +
        f"⏱️ **Prep time:** ~{max_prep} min (Delivery: +15 min)\n" +
        f"📍 **Address:** {addr_str}\n\n" +
        action_prompt
    )

    return {
        "order_confirmed": False,
        "edit_requested": False,
        "pending_action": "awaiting_order_confirmation",
        "messages": [AIMessage(content=msg)],
    }

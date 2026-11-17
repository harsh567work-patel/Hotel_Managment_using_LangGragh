"""
order_confirm_node — presents cart summary and requests explicit confirmation.
Produces exactly ONE AIMessage and ends the turn to await user confirmation.
"""

from langchain_core.messages import AIMessage
from graph.state import OrderState


def order_confirm_node(state: OrderState) -> dict:
    """Show cart summary and ask for user confirmation."""
    cart = state.get("cart", [])

    if not cart:
        msg = (
            "🛒 Your cart is currently empty! "
            "Please explore our menu or tell me what you'd like to eat before checking out."
        )
        return {
            "order_confirmed": False,
            "edit_requested": False,
            "messages": [AIMessage(content=msg)],
        }

    total_price = state.get("total_price", sum(c["unit_price"] * c["quantity"] for c in cart))
    max_prep = state.get("max_prep_time", max((c["prep_time_minutes"] for c in cart), default=20))

    cart_lines = [
        f"  • {c['dish_name']} x{c['quantity']} — ₹{c['unit_price'] * c['quantity']:.0f} (~{c['prep_time_minutes']} min)"
        for c in cart
    ]

    msg = (
        "📋 **Order Review & Checkout:**\n\n" +
        "\n".join(cart_lines) +
        f"\n\n💰 **Order Total: ₹{total_price:.0f}**\n"
        f"⏱️ **Kitchen Prep Time: ~{max_prep} min** (Delivery: +15 min)\n"
        f"📍 **Delivering from:** SpiceRoute Kitchen, 12 Food Street, Bangalore\n\n"
        "Please reply **'confirm'** to place this order, or tell me any changes you'd like to make!"
    )

    return {
        "order_confirmed": False,
        "edit_requested": False,
        "messages": [AIMessage(content=msg)],
    }

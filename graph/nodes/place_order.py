"""
place_order_node — validates that a serviceable delivery address exists before placing order.
Blocks placement and prompts for address if missing or unserviceable.
When valid address exists: generates order ID, sets delivery ETA, clears cart and pending state,
and formats receipt with Total, Prep time, and Address each on its own line.
"""

import uuid
from langchain_core.messages import AIMessage
from graph.state import OrderState
from config import DELIVERY_BUFFER_MINUTES, SERVICEABLE_CITIES
from graph.nodes.delivery import is_city_serviceable


def place_order_node(state: OrderState) -> dict:
    """Finalizes order placement if valid address exists; blocks if missing."""
    cart = state.get("cart", [])

    if not cart:
        existing_id = state.get("order_id")
        if existing_id:
            msg = (
                f"You already have an active order **`{existing_id}`** being prepared! "
                "Type **'track order'** to check its delivery status."
            )
        else:
            msg = (
                "Your cart is empty! There are no items to place an order for.\n\n"
                "Please tell me what dishes you'd like to order first."
            )
        return {
            "pending_action": None,
            "messages": [AIMessage(content=msg)],
        }

    # 1. Block place_order until a valid serviceable address exists
    address = state.get("delivery_address")
    if not address or not address.get("street") or not is_city_serviceable(address.get("city")):
        serviceable_str = ", ".join(SERVICEABLE_CITIES)
        msg = (
            "📍 **Delivery Address Required:**\n\n"
            f"We need your delivery address in **{serviceable_str}** before we can confirm and place your order!\n\n"
            "Please provide your street or locality (e.g., *'12 MG Road, Bangalore'*)."
        )
        return {
            "pending_action": "awaiting_order_confirmation",
            "messages": [AIMessage(content=msg)],
        }

    # 2. Place Order
    order_id = "ORD-" + str(uuid.uuid4())[:8].upper()
    max_prep = state.get("max_prep_time", max((c["prep_time_minutes"] for c in cart), default=20))
    total_eta = max_prep + DELIVERY_BUFFER_MINUTES
    total_price = state.get("total_price", sum(c["unit_price"] * c["quantity"] for c in cart))

    addr_str = f"{address.get('street')}, {address.get('city')}, {address.get('state', 'Karnataka')}"

    cart_lines = [
        f"  • {c['dish_name']} x{c['quantity']} — ₹{c['unit_price'] * c['quantity']:.0f}"
        for c in cart
    ]

    msg = (
        f"🎉 **Order Placed Successfully!**\n\n"
        f"📦 **Order ID:** `{order_id}`\n\n"
        f"🍽️ **Summary:**\n" +
        "\n".join(cart_lines) + "\n\n" +
        f"💰 **Total:** ₹{total_price:.0f} (Payment on Delivery)\n" +
        f"⏱️ **Prep time:** ~{max_prep} min (Delivery: ~{total_eta} min total)\n" +
        f"📍 **Address:** {addr_str}\n\n" +
        "Our chefs are preparing your food with authentic spices.\n" +
        "You can check your status anytime by saying **'where is my order?'**.\n\n" +
        "Thank you for dining with **SpiceRoute Kitchen**! 🌶️"
    )

    # Return state: clear cart, clear pending state, store order_id
    return {
        "order_id": order_id,
        "estimated_delivery_minutes": total_eta,
        "order_confirmed": True,
        "cart": [],
        "total_price": 0.0,
        "max_prep_time": 0,
        "pending_action": None,
        "messages": [AIMessage(content=msg)],
    }

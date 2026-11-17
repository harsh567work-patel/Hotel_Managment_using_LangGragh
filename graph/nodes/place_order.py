"""
place_order_node — generates the order ID, calculates total delivery ETA,
formats complete receipt confirmation, and clears the cart from state.
Produces exactly ONE AIMessage.
"""

import uuid
from langchain_core.messages import AIMessage
from graph.state import OrderState
from config import DELIVERY_BUFFER_MINUTES


def place_order_node(state: OrderState) -> dict:
    """Finalizes order placement, clears the cart, and outputs full receipt."""
    cart = state.get("cart", [])

    if not cart:
        # Check if already placed
        existing_id = state.get("order_id")
        if existing_id:
            msg = (
                f"You already have an active order **`{existing_id}`** currently being prepared! "
                "Type **'track order'** to check its delivery status."
            )
        else:
            msg = (
                "Your cart is empty! There are no items to place an order for. "
                "Please tell me what dishes you'd like to order first."
            )
        return {"messages": [AIMessage(content=msg)]}

    order_id = "ORD-" + str(uuid.uuid4())[:8].upper()
    max_prep = state.get("max_prep_time", max((c["prep_time_minutes"] for c in cart), default=20))
    total_eta = max_prep + DELIVERY_BUFFER_MINUTES
    total_price = state.get("total_price", sum(c["unit_price"] * c["quantity"] for c in cart))

    cart_lines = [
        f"  • {c['dish_name']} x{c['quantity']} — ₹{c['unit_price'] * c['quantity']:.0f}"
        for c in cart
    ]

    msg = (
        f"🎉 **Order Placed Successfully!**\n\n"
        f"📦 **Order ID:** `{order_id}`\n"
        f"🍽️ **Summary:**\n" + "\n".join(cart_lines) + "\n\n"
        f"💰 **Total Amount:** ₹{total_price:.0f} (Payment on Delivery)\n"
        f"⏱️ **Estimated Delivery Time:** ~{total_eta} minutes\n\n"
        "Our chefs are preparing your food with authentic spices. "
        "You can check your status anytime by saying **'where is my order?'**.\n"
        "Thank you for dining with **SpiceRoute Kitchen**! 🌶️"
    )

    # Return updated state: clear cart, record order ID, set ETA
    return {
        "order_id": order_id,
        "estimated_delivery_minutes": total_eta,
        "order_confirmed": True,
        "cart": [],
        "total_price": 0.0,
        "max_prep_time": 0,
        "messages": [AIMessage(content=msg)],
    }

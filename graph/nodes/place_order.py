"""
place_order_node — finalizes order: generates order ID and delivery ETA.
"""

import uuid
from langchain_core.messages import AIMessage
from graph.state import OrderState


DELIVERY_BUFFER_MINUTES = 15  # delivery time on top of prep time


def place_order_node(state: OrderState) -> dict:
    """Generate order ID and estimated delivery time."""
    order_id = "ORD-" + str(uuid.uuid4())[:8].upper()
    prep_time = state.get("max_prep_time", 20)
    total_eta = prep_time + DELIVERY_BUFFER_MINUTES
    total_price = state.get("total_price", 0)

    cart = state.get("cart", [])
    item_names = ", ".join([c["dish_name"] for c in cart])

    msg = (
        f"🎉 **Order Placed Successfully!**\n\n"
        f"📦 **Order ID:** `{order_id}`\n"
        f"🍽️ **Items:** {item_names}\n"
        f"💰 **Total Paid:** ₹{total_price:.0f}\n"
        f"⏱️ **Estimated Delivery:** ~{total_eta} minutes\n\n"
        "We'll notify you when your order is on the way. Thank you for ordering from SpiceRoute Kitchen! 🌶️"
    )

    return {
        "order_id": order_id,
        "estimated_delivery_minutes": total_eta,
        "messages": [AIMessage(content=msg)],
    }

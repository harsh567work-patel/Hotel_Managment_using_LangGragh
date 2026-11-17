"""
order_tracker_node — returns live order preparation and delivery ETA status.
Produces exactly ONE AIMessage.
"""

from langchain_core.messages import AIMessage
from graph.state import OrderState


def order_tracker_node(state: OrderState) -> dict:
    """Returns order status and ETA for an active order."""
    order_id = state.get("order_id")
    eta = state.get("estimated_delivery_minutes", 35)

    if not order_id:
        msg = (
            "You don't have an active order yet! "
            "Would you like to place one? Tell me what you'd like to eat or ask to see our menu."
        )
    else:
        msg = (
            f"📦 **Live Order Tracking**\n\n"
            f"**Order ID:** `{order_id}`\n"
            f"**Status:** Freshly preparing in our kitchen 🍳\n"
            f"⏱️ **Estimated Delivery:** ~{eta} minutes from placement\n\n"
            "Our delivery partner will reach your address shortly. Let us know if you need anything else!"
        )

    return {"messages": [AIMessage(content=msg)]}

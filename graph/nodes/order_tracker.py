"""
order_tracker_node — returns current order status and ETA.
"""

from langchain_core.messages import AIMessage
from graph.state import OrderState


def order_tracker_node(state: OrderState) -> dict:
    """Return order status based on state."""
    order_id = state.get("order_id")
    eta = state.get("estimated_delivery_minutes", 0)

    if not order_id:
        msg = (
            "You don't have an active order yet. "
            "Would you like to place one? Just tell me what you'd like to eat!"
        )
    else:
        msg = (
            f"📦 **Order Status:** Your order `{order_id}` is being prepared!\n"
            f"⏱️ **Estimated Delivery:** ~{eta} minutes from when you placed the order.\n\n"
            "We'll deliver it to your address shortly. Thank you for your patience! 🚀"
        )

    return {"messages": [AIMessage(content=msg)]}

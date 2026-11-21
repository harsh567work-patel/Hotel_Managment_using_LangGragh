"""
order_confirm_node — presents cart summary, displays delivery address status,
sets pending_action = "awaiting_order_confirmation", and requests confirmation.
Uses real newlines, markdown lists, with Total, Prep time, and Address each on its own line.
"""

from langchain_core.messages import AIMessage
from graph.state import OrderState
from graph.nodes.delivery import RESTAURANT_LOCATION


def order_confirm_node(state: OrderState) -> dict:
    """Shows checkout review, sets pending_action, and prompts for address/mode or confirmation."""
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
    order_type = state.get("order_type")
    table_num = state.get("table_number")

    cart_lines = [
        f"  • {c['dish_name']} x{c['quantity']} — ₹{c['unit_price'] * c['quantity']:.0f} (~{c['prep_time_minutes']} min)"
        for c in cart
    ]

    if order_type == "dine_in":
        table_suffix = f" (Table {table_num})" if table_num else ""
        prep_line = f"⏱️ **Prep time:** ~{max_prep} min"
        fulfillment_line = f"🍽️ **Order Type:** Dine-in{table_suffix} — {RESTAURANT_LOCATION}"
        action_prompt = "Please reply **'confirm'** to place this dine-in order, or let me know if you want to make any changes!"
    elif order_type == "pickup":
        prep_line = f"⏱️ **Ready for pickup in:** ~{max_prep} min"
        fulfillment_line = f"🛍️ **Order Type:** Self-Pickup / Takeaway — {RESTAURANT_LOCATION}"
        action_prompt = "Please reply **'confirm'** to place this pickup order, or let me know if you want to make any changes!"
    elif address and address.get("street"):
        addr_str = f"{address.get('street')}, {address.get('city', 'Bangalore')}, {address.get('state', 'Karnataka')}"
        prep_line = f"⏱️ **Prep time:** ~{max_prep} min (Delivery: +15 min)"
        fulfillment_line = f"📍 **Address:** {addr_str}"
        action_prompt = "Please reply **'confirm'** to place this order, or let me know if you want to make any changes!"
    else:
        prep_line = f"⏱️ **Prep time:** ~{max_prep} min (Delivery: +15 min)"
        fulfillment_line = "📍 **Address:** Not provided yet"
        action_prompt = (
            "Please provide your **delivery address** in Bangalore (e.g. *'12 MG Road, Bangalore'*), "
            "or let me know if you'd prefer **dine-in** or **takeaway/pickup**!"
        )

    msg = (
        "📋 **Order Review & Checkout:**\n\n" +
        "\n".join(cart_lines) + "\n\n" +
        f"💰 **Total:** ₹{total_price:.0f}\n" +
        f"{prep_line}\n" +
        f"{fulfillment_line}\n\n" +
        action_prompt
    )

    return {
        "order_confirmed": False,
        "edit_requested": False,
        "pending_action": "awaiting_order_confirmation",
        "messages": [AIMessage(content=msg)],
    }

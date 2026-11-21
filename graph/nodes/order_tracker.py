"""
order_tracker_node — returns live preparation/delivery status for ALL active & served orders.
Produces exactly ONE AIMessage.
"""

import time
from langchain_core.messages import AIMessage
from graph.state import OrderState
from graph.orders import sync_orders, get_remaining_seconds, format_remaining
from config import MAX_ACTIVE_ORDERS


def _format_mode_label(order: dict) -> str:
    mode = order.get("order_type") or "delivery"
    if mode == "dine_in":
        table_num = order.get("table_number")
        return f"Dine-in (Table {table_num})" if table_num else "Dine-in"
    if mode == "pickup":
        return "Self-Pickup"
    return "Delivery"


def order_tracker_node(state: OrderState) -> dict:
    """Returns live order status and countdowns across all active and served orders."""
    now = time.time()
    synced_orders = sync_orders(state.get("orders", []), now=now)

    if synced_orders:
        cooking = [o for o in synced_orders if o.get("status") == "cooking"]
        served = [o for o in synced_orders if o.get("status") == "served"]

        sections = [f"📦 **Live Order Tracking ({len(cooking)} / {MAX_ACTIVE_ORDERS} Active Cooking)**"]

        if cooking:
            cooking_lines = []
            for o in cooking:
                rem = format_remaining(get_remaining_seconds(o, now=now))
                items_str = ", ".join(f"{i['dish_name']} x{i['quantity']}" for i in o.get("items", []))
                mode_label = _format_mode_label(o)
                cooking_lines.append(
                    f"  • 🍳 **`{o['order_id']}`** ({mode_label}) — ⏱️ **{rem}** remaining"
                    + (f" | {items_str}" if items_str else "")
                )
            sections.append("**Currently Cooking:**\n" + "\n".join(cooking_lines))

        if served:
            served_lines = []
            for o in served:
                items_str = ", ".join(f"{i['dish_name']} x{i['quantity']}" for i in o.get("items", []))
                mode_label = _format_mode_label(o)
                served_lines.append(
                    f"  • ✅ **Served ({o['order_id']})** ({mode_label})"
                    + (f" — {items_str}" if items_str else "")
                )
            sections.append("**Completed / Served:**\n" + "\n".join(served_lines))

        return {
            "orders": synced_orders,
            "messages": [AIMessage(content="\n\n".join(sections))],
        }

    # Backwards-compatible fallback when only legacy order_id is present
    order_id = state.get("order_id")
    eta = state.get("estimated_delivery_minutes", 35)
    order_type = state.get("order_type") or "delivery"
    table_num = state.get("table_number")

    if not order_id:
        msg = (
            "You don't have an active order yet! "
            "Would you like to place one? Tell me what you'd like to eat or ask to see our menu."
        )
    elif order_type == "dine_in":
        table_suffix = f" (Table {table_num})" if table_num else ""
        msg = (
            f"📦 **Live Order Tracking (Dine-in{table_suffix})**\n\n"
            f"**Order ID:** `{order_id}`\n"
            f"**Status:** Freshly preparing in our kitchen 🍳\n"
            f"⏱️ **Estimated Serve Time:** ~{eta} minutes from placement\n\n"
            "Your food will be served hot to your table shortly. Let us know if you need anything else!"
        )
    elif order_type == "pickup":
        msg = (
            f"📦 **Live Order Tracking (Self-Pickup)**\n\n"
            f"**Order ID:** `{order_id}`\n"
            f"**Status:** Freshly preparing in our kitchen 🍳\n"
            f"⏱️ **Estimated Pickup Ready Time:** ~{eta} minutes from placement\n\n"
            "You can collect your order from the counter at **SpiceRoute Kitchen, 12 Food Street, Bangalore** shortly!"
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

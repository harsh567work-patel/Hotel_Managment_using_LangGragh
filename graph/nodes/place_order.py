"""
place_order_node — validates that a serviceable delivery address exists before placing order.
Blocks placement and prompts for address if missing or unserviceable.
When valid address exists: generates order ID, sets delivery ETA, clears cart and pending state,
and formats receipt with Total, Prep time, and Address each on its own line.
"""

import time
import uuid
from langchain_core.messages import AIMessage
from graph.state import OrderState, OrderRecord
from config import DELIVERY_BUFFER_MINUTES, SERVICEABLE_CITIES, MAX_ACTIVE_ORDERS
from graph.nodes.delivery import is_city_serviceable, RESTAURANT_LOCATION
from graph.orders import sync_orders, get_remaining_seconds, format_remaining


def place_order_node(state: OrderState) -> dict:
    """
    Finalizes order placement for dine-in, pickup, or delivery.
    Enforces max concurrent active cooking orders (MAX_ACTIVE_ORDERS = 5)
    and preserves all previous orders in state['orders'] while cooking/served.
    """
    cart = state.get("cart", [])
    now = time.time()
    synced_orders = sync_orders(state.get("orders", []), now=now)
    cooking_orders = [o for o in synced_orders if o.get("status") == "cooking"]

    if not cart:
        existing_id = state.get("order_id")
        if cooking_orders:
            ids = ", ".join(f"`{o['order_id']}`" for o in cooking_orders)
            msg = (
                f"You already have **{len(cooking_orders)}** active order(s) cooking: {ids}.\n\n"
                "Type **'track order'** to check live status, or tell me new dishes to start another order!"
            )
        elif existing_id:
            msg = (
                f"You already have an active order **`{existing_id}`** being prepared! "
                "Type **'track order'** to check its status."
            )
        else:
            msg = (
                "Your cart is empty! There are no items to place an order for.\n\n"
                "Please tell me what dishes you'd like to order first."
            )
        return {
            "orders": synced_orders,
            "pending_action": None,
            "messages": [AIMessage(content=msg)],
        }

    # 1. Enforce max concurrent active cooking orders limit (5)
    if len(cooking_orders) >= MAX_ACTIVE_ORDERS:
        active_lines = [
            f"  • `{o['order_id']}` — ⏱️ {format_remaining(get_remaining_seconds(o, now=now))} left"
            for o in cooking_orders
        ]
        msg = (
            f"⚠️ **Kitchen Active Order Limit Reached ({MAX_ACTIVE_ORDERS}/{MAX_ACTIVE_ORDERS}):**\n\n"
            f"You already have **{MAX_ACTIVE_ORDERS} orders** actively cooking right now:\n" +
            "\n".join(active_lines) + "\n\n"
            "Please wait for at least one order to be **Served** before placing another order! "
            "*(Your current cart has been saved.)*"
        )
        return {
            "orders": synced_orders,
            "pending_action": "awaiting_order_confirmation",
            "messages": [AIMessage(content=msg)],
        }

    order_type = state.get("order_type") or "delivery"
    address = state.get("delivery_address")
    table_num = state.get("table_number")

    # 2. For delivery orders, block place_order until a valid serviceable address exists
    if order_type == "delivery":
        if not address or not address.get("street") or not is_city_serviceable(address.get("city")):
            serviceable_str = ", ".join(SERVICEABLE_CITIES)
            msg = (
                "📍 **Delivery Address Required:**\n\n"
                f"We need your delivery address in **{serviceable_str}** before we can confirm and place your order!\n\n"
                "Please provide your street or locality (e.g., *'12 MG Road, Bangalore'*), "
                "or let me know if you'd like **dine-in** or **takeaway/pickup** instead."
            )
            return {
                "orders": synced_orders,
                "pending_action": "awaiting_order_confirmation",
                "messages": [AIMessage(content=msg)],
            }

    # 3. Place Order & append to persistent orders history (keeping previous orders cooking!)
    order_id = "ORD-" + str(uuid.uuid4())[:8].upper()
    max_prep = state.get("max_prep_time", max((c["prep_time_minutes"] for c in cart), default=20))
    total_price = state.get("total_price", sum(c["unit_price"] * c["quantity"] for c in cart))

    cart_lines = [
        f"  • {c['dish_name']} x{c['quantity']} — ₹{c['unit_price'] * c['quantity']:.0f}"
        for c in cart
    ]

    if order_type == "dine_in":
        total_eta = max_prep
        table_suffix = f" (Table {table_num})" if table_num else ""
        payment_label = "Pay at Restaurant"
        time_line = f"⏱️ **Prep time:** ~{max_prep} min"
        location_line = f"🍽️ **Order Type:** Dine-in{table_suffix} — {RESTAURANT_LOCATION}"
    elif order_type == "pickup":
        total_eta = max_prep
        payment_label = "Pay at Pickup"
        time_line = f"⏱️ **Ready for pickup in:** ~{max_prep} min"
        location_line = f"🛍️ **Order Type:** Self-Pickup / Takeaway — {RESTAURANT_LOCATION}"
    else:
        total_eta = max_prep + DELIVERY_BUFFER_MINUTES
        addr_str = f"{address.get('street')}, {address.get('city')}, {address.get('state', 'Karnataka')}"
        payment_label = "Payment on Delivery"
        time_line = f"⏱️ **Prep time:** ~{max_prep} min (Delivery: ~{total_eta} min total)"
        location_line = f"📍 **Address:** {addr_str}"

    new_record: OrderRecord = {
        "order_id": order_id,
        "items": [dict(c) for c in cart],
        "total_price": float(total_price),
        "order_type": order_type,
        "table_number": table_num,
        "delivery_address": dict(address) if (order_type == "delivery" and address) else None,
        "eta_minutes": int(total_eta),
        "placed_at": now,
        "status": "cooking",
    }
    updated_orders = synced_orders + [new_record]
    active_count = sum(1 for o in updated_orders if o.get("status") == "cooking")

    msg = (
        f"🎉 **Order Placed Successfully!**\n\n"
        f"📦 **Order ID:** `{order_id}`\n\n"
        f"🍽️ **Summary:**\n" +
        "\n".join(cart_lines) + "\n\n" +
        f"💰 **Total:** ₹{total_price:.0f} ({payment_label})\n" +
        f"{time_line}\n" +
        f"{location_line}\n" +
        f"🔥 **Active Kitchen Orders:** {active_count} / {MAX_ACTIVE_ORDERS}\n\n" +
        "Our chefs are preparing your food with authentic spices.\n" +
        "You can check live status anytime by saying **'where is my order?'**.\n\n" +
        "Thank you for dining with **SpiceRoute Kitchen**! 🌶️"
    )

    # Return state: preserve ALL previous orders, clear cart & pending state
    return {
        "order_id": order_id,
        "order_type": order_type,
        "estimated_delivery_minutes": total_eta,
        "order_confirmed": True,
        "orders": updated_orders,
        "cart": [],
        "total_price": 0.0,
        "max_prep_time": 0,
        "pending_action": None,
        "messages": [AIMessage(content=msg)],
    }

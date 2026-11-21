"""
tests/test_orders.py — Unit tests for multi-order concurrency, 5-order kitchen limit,
persistent order history across new orders, and automatic countdown -> Served transition.
"""

import time
from langchain_core.messages import HumanMessage
from graph.state import OrderState, CartItem, OrderRecord
from graph.orders import (
    sync_orders,
    get_remaining_seconds,
    get_active_cooking_orders,
    get_served_orders,
    format_remaining,
)
from graph.nodes.place_order import place_order_node
from graph.nodes.order_tracker import order_tracker_node


def _sample_cart():
    return [
        CartItem(
            dish_id="D001",
            dish_name="Butter Chicken",
            quantity=1,
            unit_price=280.0,
            prep_time_minutes=25,
            category="Main Course",
        )
    ]


def test_multiple_orders_preserved_while_cooking():
    # Place Order 1 (dine-in)
    state1: OrderState = {
        "messages": [HumanMessage(content="confirm")],
        "cart": _sample_cart(),
        "total_price": 280.0,
        "max_prep_time": 25,
        "order_type": "dine_in",
        "orders": [],
    }
    res1 = place_order_node(state1)
    assert len(res1["orders"]) == 1
    first_id = res1["order_id"]

    # Place Order 2 (pickup) while Order 1 is still cooking
    state2: OrderState = {
        "messages": [HumanMessage(content="confirm")],
        "cart": _sample_cart(),
        "total_price": 280.0,
        "max_prep_time": 20,
        "order_type": "pickup",
        "orders": res1["orders"],
    }
    res2 = place_order_node(state2)
    assert len(res2["orders"]) == 2
    ids = [o["order_id"] for o in res2["orders"]]
    assert first_id in ids
    assert res2["order_id"] in ids
    assert all(o["status"] == "cooking" for o in res2["orders"])
    assert "2 / 5" in res2["messages"][0].content


def test_max_5_active_orders_blocks_6th_order():
    now = time.time()
    five_cooking: list[OrderRecord] = [
        {
            "order_id": f"ORD-0000000{i}",
            "items": _sample_cart(),
            "total_price": 280.0,
            "order_type": "dine_in",
            "eta_minutes": 25,
            "placed_at": now,
            "status": "cooking",
        }
        for i in range(1, 6)
    ]

    state6: OrderState = {
        "messages": [HumanMessage(content="confirm")],
        "cart": _sample_cart(),
        "total_price": 280.0,
        "max_prep_time": 25,
        "order_type": "dine_in",
        "orders": five_cooking,
    }
    res6 = place_order_node(state6)

    # 6th order must be blocked and cart kept intact
    assert res6.get("order_id") is None
    assert len(res6["orders"]) == 5
    msg = res6["messages"][0].content
    assert "Limit Reached (5/5)" in msg


def test_order_becomes_served_at_zero_and_frees_slot():
    now = time.time()
    # Order 1 expired 10 seconds ago; orders 2..5 are still cooking
    orders: list[OrderRecord] = [
        {
            "order_id": "ORD-EXPIRED1",
            "items": _sample_cart(),
            "total_price": 280.0,
            "order_type": "dine_in",
            "eta_minutes": 10,
            "placed_at": now - 610,  # 10m10s ago -> remaining = 0
            "status": "cooking",
        }
    ] + [
        {
            "order_id": f"ORD-COOKING{i}",
            "items": _sample_cart(),
            "total_price": 280.0,
            "order_type": "dine_in",
            "eta_minutes": 25,
            "placed_at": now,
            "status": "cooking",
        }
        for i in range(2, 6)
    ]

    synced = sync_orders(orders, now=now)
    assert synced[0]["status"] == "served"
    assert get_remaining_seconds(synced[0], now=now) == 0
    assert len(get_active_cooking_orders(synced, now=now)) == 4
    assert len(get_served_orders(synced, now=now)) == 1

    # Tracker must display "Served (ORD-EXPIRED1)" and all 4 cooking orders
    track_res = order_tracker_node({"orders": orders})
    track_msg = track_res["messages"][0].content
    assert "Served (ORD-EXPIRED1)" in track_msg
    assert "4 / 5 Active Cooking" in track_msg

    # Because 1 order is now served (4/5 active), placing a new order MUST succeed!
    new_res = place_order_node({
        "messages": [HumanMessage(content="confirm")],
        "cart": _sample_cart(),
        "total_price": 280.0,
        "max_prep_time": 15,
        "order_type": "dine_in",
        "orders": orders,
    })
    assert new_res.get("order_id") is not None
    assert len(new_res["orders"]) == 6  # 1 served + 5 cooking
    assert sum(1 for o in new_res["orders"] if o["status"] == "cooking") == 5
    assert sum(1 for o in new_res["orders"] if o["status"] == "served") == 1


def test_format_remaining():
    assert format_remaining(0) == "00:00"
    assert format_remaining(65) == "01:05"
    assert format_remaining(2100) == "35:00"

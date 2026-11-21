"""
tests/test_delivery.py — Unit tests for delivery address handling,
serviceability validation, checkout summary integration, and order blocking.
"""

from graph.nodes.delivery import (
    is_city_serviceable,
    set_delivery_address_node,
    set_order_type_node,
    delivery_question_node,
)
from graph.nodes.place_order import place_order_node
from graph.state import OrderState, CartItem
from langchain_core.messages import HumanMessage


def test_is_city_serviceable():
    assert is_city_serviceable("Bangalore") is True
    assert is_city_serviceable("Bengaluru") is True
    assert is_city_serviceable("bangalore") is True
    assert is_city_serviceable("Mumbai") is False
    assert is_city_serviceable("Delhi") is False
    assert is_city_serviceable(None) is False
    assert is_city_serviceable("") is False


def test_set_delivery_address_serviceable():
    cart = [CartItem(dish_id="D001", dish_name="Butter Chicken", quantity=2, unit_price=280.0, prep_time_minutes=25, category="Main Course")]
    state: OrderState = {
        "messages": [HumanMessage(content="My address is 45 MG Road, Bangalore")],
        "cart": cart,
        "total_price": 560.0,
        "max_prep_time": 25,
        "pending_action": "awaiting_order_confirmation",
    }
    res = set_delivery_address_node(state)

    assert res.get("delivery_address") is not None
    assert res["delivery_address"]["city"] == "Bangalore"
    assert "45 MG Road" in res["delivery_address"]["street"]
    assert res.get("pending_action") == "awaiting_order_confirmation"

    msg = res["messages"][0].content
    assert "45 MG Road" in msg
    assert "₹560" in msg
    assert "Prep time:" in msg


def test_set_delivery_address_unserviceable_city():
    cart = [CartItem(dish_id="D001", dish_name="Butter Chicken", quantity=2, unit_price=280.0, prep_time_minutes=25, category="Main Course")]
    state: OrderState = {
        "messages": [HumanMessage(content="Deliver to Flat 101, Bandra West, Mumbai")],
        "cart": cart,
        "total_price": 560.0,
        "max_prep_time": 25,
        "pending_action": "awaiting_order_confirmation",
    }
    res = set_delivery_address_node(state)

    assert res.get("delivery_address") is None
    assert res.get("pending_action") == "awaiting_order_confirmation"
    msg = res["messages"][0].content
    assert "only deliver within" in msg
    assert "Mumbai" in msg


def test_place_order_blocked_without_address():
    cart = [CartItem(dish_id="D001", dish_name="Butter Chicken", quantity=2, unit_price=280.0, prep_time_minutes=25, category="Main Course")]
    state: OrderState = {
        "messages": [HumanMessage(content="confirm")],
        "cart": cart,
        "total_price": 560.0,
        "max_prep_time": 25,
        "delivery_address": None,
        "pending_action": "awaiting_order_confirmation",
    }
    res = place_order_node(state)

    # Must be blocked!
    assert res.get("order_id") is None
    assert res.get("pending_action") == "awaiting_order_confirmation"
    msg = res["messages"][0].content
    assert "Delivery Address Required" in msg


def test_place_order_succeeds_with_valid_address():
    cart = [CartItem(dish_id="D001", dish_name="Butter Chicken", quantity=2, unit_price=280.0, prep_time_minutes=25, category="Main Course")]
    state: OrderState = {
        "messages": [HumanMessage(content="confirm")],
        "cart": cart,
        "total_price": 560.0,
        "max_prep_time": 25,
        "delivery_address": {"street": "45 MG Road", "city": "Bangalore", "state": "Karnataka"},
        "pending_action": "awaiting_order_confirmation",
    }
    res = place_order_node(state)

    assert res.get("order_id") is not None
    assert res.get("order_id").startswith("ORD-")
    assert res.get("cart") == []
    assert res.get("pending_action") is None
    msg = res["messages"][0].content
    assert "Order Placed Successfully" in msg
    assert "45 MG Road" in msg
    assert "₹560" in msg


def test_delivery_question_node():
    state: OrderState = {
        "messages": [HumanMessage(content="What is your delivery fee and coverage area?")],
        "cart": [],
        "pending_action": None,
    }
    res = delivery_question_node(state)
    msg = res["messages"][0].content
    assert "FREE" in msg
    assert "Bangalore" in msg


def test_set_order_type_dine_in():
    cart = [CartItem(dish_id="D003", dish_name="Chicken Biryani", quantity=2, unit_price=320.0, prep_time_minutes=35, category="Main Course")]
    state: OrderState = {
        "messages": [HumanMessage(content="will dine here")],
        "cart": cart,
        "total_price": 640.0,
        "max_prep_time": 35,
        "pending_action": "awaiting_order_confirmation",
    }
    res = set_order_type_node(state)

    assert res.get("order_type") == "dine_in"
    assert res.get("pending_action") == "awaiting_order_confirmation"
    msg = res["messages"][0].content
    assert "Dine-in" in msg
    assert "no delivery address needed" in msg.lower()
    assert "confirm" in msg.lower()


def test_place_order_dine_in_succeeds_without_address():
    cart = [
        CartItem(dish_id="D003", dish_name="Chicken Biryani", quantity=2, unit_price=320.0, prep_time_minutes=35, category="Main Course"),
        CartItem(dish_id="D010", dish_name="Mango Lassi", quantity=2, unit_price=90.0, prep_time_minutes=5, category="Drinks"),
    ]
    state: OrderState = {
        "messages": [HumanMessage(content="confirm")],
        "cart": cart,
        "total_price": 820.0,
        "max_prep_time": 35,
        "order_type": "dine_in",
        "delivery_address": None,
        "pending_action": "awaiting_order_confirmation",
    }
    res = place_order_node(state)

    assert res.get("order_id") is not None
    assert res.get("order_id").startswith("ORD-")
    assert res.get("estimated_delivery_minutes") == 35  # no delivery buffer
    assert res.get("cart") == []
    assert res.get("pending_action") is None
    msg = res["messages"][0].content
    assert "Order Placed Successfully" in msg
    assert "Dine-in" in msg
    assert "Pay at Restaurant" in msg


def test_place_order_pickup_succeeds_without_address():
    cart = [CartItem(dish_id="D001", dish_name="Butter Chicken", quantity=1, unit_price=280.0, prep_time_minutes=25, category="Main Course")]
    state: OrderState = {
        "messages": [HumanMessage(content="confirm")],
        "cart": cart,
        "total_price": 280.0,
        "max_prep_time": 25,
        "order_type": "pickup",
        "delivery_address": None,
        "pending_action": "awaiting_order_confirmation",
    }
    res = place_order_node(state)

    assert res.get("order_id") is not None
    assert res.get("estimated_delivery_minutes") == 25  # no delivery buffer
    msg = res["messages"][0].content
    assert "Order Placed Successfully" in msg
    assert "Self-Pickup" in msg
    assert "Pay at Pickup" in msg

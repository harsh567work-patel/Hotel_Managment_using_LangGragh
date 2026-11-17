"""
tests/test_routing.py — Unit tests for the StateGraph conditional routing logic.
Verifies that route_from_intent routes to the exact designated terminal node for every intent,
and routes to multi_intent_processor when multiple intents are detected.
"""

from graph.graph_builder import route_from_intent
from graph.state import OrderState, CartItem


def test_routing_greet():
    state: OrderState = {"intents": ["greet"], "cart": [], "order_id": None}
    assert route_from_intent(state) == "greeter"


def test_routing_menu_inquiry():
    state: OrderState = {"intents": ["menu_inquiry"], "cart": [], "order_id": None}
    assert route_from_intent(state) == "menu_rag"


def test_routing_add_to_cart():
    state: OrderState = {"intents": ["add_to_cart"], "cart": [], "order_id": None}
    assert route_from_intent(state) == "cart_builder"


def test_routing_modify_cart():
    state: OrderState = {"intents": ["modify_cart"], "cart": [], "order_id": None}
    assert route_from_intent(state) == "cart_builder"


def test_routing_set_delivery_address():
    state: OrderState = {"intents": ["set_delivery_address"], "cart": [], "order_id": None}
    assert route_from_intent(state) == "set_delivery_address"


def test_routing_delivery_question():
    state: OrderState = {"intents": ["delivery_question"], "cart": [], "order_id": None}
    assert route_from_intent(state) == "delivery_question"


def test_routing_checkout():
    state: OrderState = {"intents": ["checkout"], "cart": [], "order_id": None}
    assert route_from_intent(state) == "order_confirm"


def test_routing_confirm_order_with_cart():
    cart = [CartItem(dish_id="D001", dish_name="Butter Chicken", quantity=1, unit_price=280.0, prep_time_minutes=25, category="Main Course")]
    state: OrderState = {"intents": ["confirm_order"], "cart": cart, "order_id": None}
    assert route_from_intent(state) == "place_order"


def test_routing_confirm_order_without_cart_but_with_order_id():
    state: OrderState = {"intents": ["confirm_order"], "cart": [], "order_id": "ORD-12345678"}
    assert route_from_intent(state) == "order_tracker"


def test_routing_track_order():
    state: OrderState = {"intents": ["track_order"], "cart": [], "order_id": "ORD-12345678"}
    assert route_from_intent(state) == "order_tracker"


def test_routing_out_of_scope():
    state: OrderState = {"intents": ["out_of_scope"], "cart": [], "order_id": None}
    assert route_from_intent(state) == "guardrail"


def test_routing_fallback_unknown():
    state: OrderState = {"intents": ["random_nonsense"], "cart": [], "order_id": None}
    assert route_from_intent(state) == "clarify_intent"


def test_routing_multi_intent():
    state: OrderState = {"intents": ["add_to_cart", "checkout"], "cart": [], "order_id": None}
    assert route_from_intent(state) == "multi_intent_processor"

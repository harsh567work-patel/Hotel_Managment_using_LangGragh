"""
tests/test_cart.py — Unit tests for the pure Python cart reducer and action resolution.
Verifies add, remove, update, clear, limits, and unavailable dish rejections.
"""

import pytest
from graph.nodes.cart_builder import apply_cart_actions, CartAction, CartItem


def test_add_single_dish():
    cart = []
    actions = [CartAction(action="add", dish_name="Butter Chicken", quantity=2)]
    updated, success, errors = apply_cart_actions(cart, actions)

    assert len(updated) == 1
    assert updated[0]["dish_name"] == "Butter Chicken"
    assert updated[0]["quantity"] == 2
    assert updated[0]["unit_price"] == 280.0
    assert len(success) == 1
    assert len(errors) == 0


def test_add_multiple_dishes():
    cart = []
    actions = [
        CartAction(action="add", dish_name="Butter Chicken", quantity=1),
        CartAction(action="add", dish_name="Plain Naan", quantity=3),
    ]
    updated, success, errors = apply_cart_actions(cart, actions)

    assert len(updated) == 2
    assert updated[0]["dish_name"] == "Butter Chicken"
    assert updated[1]["dish_name"] == "Plain Naan"
    assert updated[1]["quantity"] == 3
    assert len(success) == 2


def test_increment_existing_dish():
    cart = [CartItem(
        dish_id="D001",
        dish_name="Butter Chicken",
        quantity=1,
        unit_price=280.0,
        prep_time_minutes=25,
        category="Main Course"
    )]
    actions = [CartAction(action="add", dish_name="Butter Chicken", quantity=2)]
    updated, success, errors = apply_cart_actions(cart, actions)

    assert len(updated) == 1
    assert updated[0]["quantity"] == 3
    assert "total: 3" in success[0]


def test_remove_dish_in_cart():
    cart = [
        CartItem(dish_id="D001", dish_name="Butter Chicken", quantity=1, unit_price=280.0, prep_time_minutes=25, category="Main Course"),
        CartItem(dish_id="D006", dish_name="Plain Naan", quantity=2, unit_price=40.0, prep_time_minutes=8, category="Breads"),
    ]
    actions = [CartAction(action="remove", dish_name="Plain Naan", quantity=1)]
    updated, success, errors = apply_cart_actions(cart, actions)

    assert len(updated) == 1
    assert updated[0]["dish_name"] == "Butter Chicken"
    assert "Removed" in success[0]


def test_remove_dish_not_in_cart():
    cart = [CartItem(dish_id="D001", dish_name="Butter Chicken", quantity=1, unit_price=280.0, prep_time_minutes=25, category="Main Course")]
    actions = [CartAction(action="remove", dish_name="Veg Samosa (2 pcs)", quantity=1)]
    updated, success, errors = apply_cart_actions(cart, actions)

    assert len(updated) == 1
    assert len(errors) == 1
    assert "not in your cart" in errors[0]


def test_update_dish_quantity():
    cart = [CartItem(dish_id="D001", dish_name="Butter Chicken", quantity=1, unit_price=280.0, prep_time_minutes=25, category="Main Course")]
    actions = [CartAction(action="update", dish_name="Butter Chicken", quantity=4)]
    updated, success, errors = apply_cart_actions(cart, actions)

    assert len(updated) == 1
    assert updated[0]["quantity"] == 4
    assert "quantity to 4" in success[0]


def test_update_dish_quantity_zero_removes_item():
    cart = [CartItem(dish_id="D001", dish_name="Butter Chicken", quantity=2, unit_price=280.0, prep_time_minutes=25, category="Main Course")]
    actions = [CartAction(action="update", dish_name="Butter Chicken", quantity=0)]
    updated, success, errors = apply_cart_actions(cart, actions)

    assert len(updated) == 0
    assert "Removed" in success[0]


def test_clear_cart():
    cart = [
        CartItem(dish_id="D001", dish_name="Butter Chicken", quantity=2, unit_price=280.0, prep_time_minutes=25, category="Main Course"),
        CartItem(dish_id="D006", dish_name="Plain Naan", quantity=2, unit_price=40.0, prep_time_minutes=8, category="Breads"),
    ]
    actions = [CartAction(action="clear", dish_name=None, quantity=1)]
    updated, success, errors = apply_cart_actions(cart, actions)

    assert len(updated) == 0
    assert "Emptied the cart" in success[0]


def test_reject_unavailable_dish():
    cart = []
    actions = [CartAction(action="add", dish_name="Mutton Rogan Josh", quantity=1)]
    updated, success, errors = apply_cart_actions(cart, actions)

    assert len(updated) == 0
    assert len(errors) == 1
    assert "unavailable today" in errors[0]
    assert "Butter Chicken" in errors[0] or "Chicken Biryani" in errors[0]


def test_unknown_dish_graceful_handling():
    cart = []
    actions = [CartAction(action="add", dish_name="Space Burger X", quantity=1)]
    updated, success, errors = apply_cart_actions(cart, actions)

    assert len(updated) == 0
    assert len(errors) == 1
    assert "couldn't find 'Space Burger X'" in errors[0]

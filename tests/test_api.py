"""
tests/test_api.py — Unit & Integration tests for the FastAPI deployment endpoints.
"""

import pytest
from fastapi.testclient import TestClient
from api import app, _sessions


@pytest.fixture
def client():
    return TestClient(app)


def test_health_endpoint(client):
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert "status" in data
    assert "model" in data
    assert "serviceable_cities" in data
    assert "Bangalore" in data["serviceable_cities"]
    assert data["max_active_orders_per_customer"] == 5


def test_menu_endpoint_all(client):
    res = client.get("/menu")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] >= 36
    assert data["total_dishes_in_menu"] >= 36
    assert len(data["dishes"]) >= 36


def test_menu_endpoint_filters(client):
    # Category filter
    res_starters = client.get("/menu?category=Starters")
    assert res_starters.status_code == 200
    data_starters = res_starters.json()
    assert all(d["category"] == "Starters" for d in data_starters["dishes"])

    # Vegetarian filter
    res_veg = client.get("/menu?diet=veg")
    assert res_veg.status_code == 200
    data_veg = res_veg.json()
    assert all(d["is_veg"] is True for d in data_veg["dishes"])

    # Non-vegetarian filter
    res_non_veg = client.get("/menu?diet=non-veg")
    assert res_non_veg.status_code == 200
    data_non_veg = res_non_veg.json()
    assert all(d["is_veg"] is False for d in data_non_veg["dishes"])


def test_chat_turn_greeting(client):
    session_id = "test_session_greet"
    res = client.post("/chat", json={"session_id": session_id, "message": "Hello"})
    assert res.status_code == 200
    data = res.json()
    assert data["session_id"] == session_id
    assert len(data["reply"]) > 0
    assert "greet" in data["intents"] or data["primary_intent"] == "greet"


def test_chat_turn_cart_addition(client):
    session_id = "test_session_cart"
    res = client.post("/chat", json={"session_id": session_id, "message": "I want 2 garlic naan"})
    assert res.status_code == 200
    data = res.json()
    assert data["session_id"] == session_id
    assert len(data["cart"]) == 1
    assert "Garlic Naan" in data["cart"][0]["dish_name"]
    assert data["cart"][0]["quantity"] == 2
    assert data["total_price"] in (120.0, 190.0)


def test_track_orders_empty_session_404(client):
    res = client.post("/orders/track", json={"session_id": "non_existent_session_999"})
    assert res.status_code == 404
    assert "No orders found" in res.json()["detail"]


def test_session_reset(client):
    session_id = "test_session_reset"
    client.post("/chat", json={"session_id": session_id, "message": "I want 1 paneer tikka masala"})
    assert session_id in _sessions

    del_res = client.delete(f"/sessions/{session_id}")
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "success"
    assert session_id not in _sessions

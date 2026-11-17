"""
tests/verify_scenarios.py — Direct verification script for the three specific requests:
1. Address while awaiting confirmation
2. Unserviceable city handling and recovery
3. Compound multi-intent turns with combined single message
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from graph.graph_builder import food_graph
from langchain_core.messages import HumanMessage, AIMessage


def run_dialog(prompts):
    state = {
        "messages": [], "intent": "", "intents": [], "user_query": "", "retrieved_menu": [],
        "unavailable_items": [], "cart": [], "total_price": 0.0, "max_prep_time": 0,
        "delivery_address": None, "pending_action": None,
        "order_confirmed": False, "edit_requested": False, "order_id": None,
        "estimated_delivery_minutes": 0, "fallback_count": 0, "backtrack_count": 0, "error": None
    }
    history = []
    for p in prompts:
        prev_len = len(state["messages"])
        state["messages"] = state["messages"] + [HumanMessage(content=p)]
        state = food_graph.invoke(state)
        ai_msgs = [m for m in state["messages"][prev_len:] if isinstance(m, AIMessage)]
        reply = ai_msgs[-1].content if ai_msgs else ""
        history.append({
            "user": p,
            "intent": state.get("intent"),
            "intents": state.get("intents"),
            "pending_action": state.get("pending_action"),
            "cart": [(c["dish_name"], c["quantity"]) for c in state.get("cart", [])],
            "address": state.get("delivery_address"),
            "order_id": state.get("order_id"),
            "reply": reply
        })
    return history


def main():
    print("=" * 70)
    print("=== SCENARIO 1: Address while awaiting confirm ===")
    print("=" * 70)
    s1 = run_dialog([
        "I want 2 butter chicken",
        "Checkout",
        "My address is 45 MG Road, Bangalore",
        "Confirm"
    ])
    for step in s1:
        print(f"USER: {step['user']}")
        print(f"INTENTS: {step['intents']} | PENDING: {step['pending_action']}")
        print(f"REPLY:\n{step['reply']}\n" + "-" * 50)

    print("\n" + "=" * 70)
    print("=== SCENARIO 2: Unserviceable City Rejection & Recovery ===")
    print("=" * 70)
    s2 = run_dialog([
        "I want 1 paneer tikka masala",
        "Checkout",
        "Deliver to Flat 101, Bandra West, Mumbai",
        "Okay deliver to 12 Brigade Road, Bangalore instead",
        "Confirm"
    ])
    for step in s2:
        print(f"USER: {step['user']}")
        print(f"INTENTS: {step['intents']} | PENDING: {step['pending_action']}")
        print(f"REPLY:\n{step['reply']}\n" + "-" * 50)

    print("\n" + "=" * 70)
    print("=== SCENARIO 3: Compound Multi-Intent Turns ===")
    print("=" * 70)
    s3 = run_dialog([
        "add one mango lassi and make it final",
        "My address is 100 Indiranagar, Bangalore and confirm order"
    ])
    for step in s3:
        print(f"USER: {step['user']}")
        print(f"INTENTS: {step['intents']} | PENDING: {step['pending_action']}")
        print(f"REPLY:\n{step['reply']}\n" + "-" * 50)


if __name__ == "__main__":
    main()

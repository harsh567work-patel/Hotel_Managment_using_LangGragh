"""
tests/eval_new_cases.py — Evaluates the new test cases TC055-TC059 against the real compiled food_graph.
"""

import sys
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from langchain_core.messages import HumanMessage, AIMessage
from graph.graph_builder import food_graph
from eval.evaluator import evaluate_response_grounding
from langchain_groq import ChatGroq
from config import GROQ_MODEL, GROQ_API_KEY


def main():
    cases_file = PROJECT_ROOT / "tests" / "conversations.json"
    with open(cases_file, "r", encoding="utf-8") as f:
        all_cases = json.load(f)

    target_cases = [c for c in all_cases if c["id"] in ["TC043", "TC044", "TC045", "TC049", "TC055", "TC056", "TC057", "TC058", "TC059"]]
    judge_llm = ChatGroq(model=GROQ_MODEL, api_key=GROQ_API_KEY, temperature=0.0)

    print(f"Evaluating {len(target_cases)} target test cases...")
    print("=" * 80)

    all_passed = True
    for case in target_cases:
        cid = case["id"]
        category = case["category"]
        desc = case["description"]
        turns = case["turns"]
        expected_final_cart = case.get("expected_final_cart", [])
        expected_order_placed = case.get("expected_order_placed", False)
        rubric = case.get("rubric", {})

        state = {
            "messages": [], "intent": "", "intents": [], "user_query": "",
            "retrieved_menu": [], "unavailable_items": [], "cart": [],
            "total_price": 0.0, "max_prep_time": 0, "delivery_address": None,
            "pending_action": None, "order_confirmed": False, "edit_requested": False,
            "order_id": None, "estimated_delivery_minutes": 0, "fallback_count": 0,
            "backtrack_count": 0, "error": None
        }

        turns_ok = True
        last_bot_reply = ""
        last_user_query = ""

        for turn in turns:
            user_msg = turn["user"]
            expected_intent = turn["expected_intent"]
            last_user_query = user_msg

            prev_len = len(state["messages"])
            state["messages"] = state["messages"] + [HumanMessage(content=user_msg)]
            state = food_graph.invoke(state)

            actual_intent = state.get("intent")
            actual_intents = state.get("intents", [])
            intent_ok = (actual_intent == expected_intent) or (expected_intent in actual_intents)
            if not intent_ok:
                turns_ok = False

            new_ai = [m for m in state["messages"][prev_len:] if isinstance(m, AIMessage)]
            last_bot_reply = new_ai[-1].content if new_ai else ""

        actual_cart = [{"dish_name": c["dish_name"], "quantity": c["quantity"]} for c in state.get("cart", [])]
        cart_ok = (actual_cart == expected_final_cart)
        order_ok = (bool(state.get("order_id")) == expected_order_placed)
        grounding_score = evaluate_response_grounding(judge_llm, last_user_query, last_bot_reply, rubric)

        status = "PASSED" if (turns_ok and cart_ok and order_ok and grounding_score >= 0.75) else "FAILED"
        if status == "FAILED":
            all_passed = False

        print(f"[{status}] {cid} ({category}): {desc}")
        print(f"       Intent OK: {turns_ok} | Cart OK: {cart_ok} | Order OK: {order_ok} | Grounding: {grounding_score:.2f}")

    print("=" * 80)
    print(f"All target cases passed: {all_passed}")


if __name__ == "__main__":
    main()

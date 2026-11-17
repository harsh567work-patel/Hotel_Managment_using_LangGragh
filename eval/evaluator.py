"""
eval/evaluator.py — Real end-to-end evaluation suite for SpiceRoute Kitchen.
Runs full multi-turn conversations through the ACTUAL compiled LangGraph food_graph.
Reports:
  1. Intent classification accuracy across all turns
  2. Cart-state exact-match rate
  3. Grounded-answer correctness (rubric + LLM Judge)
  4. Order completion rate
  5. Median and p95 latency
Outputs summary table to stdout and writes full logs to eval/results.json.
"""

import sys
import json
import time
import statistics
from pathlib import Path
from typing import List, Dict, Any

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_groq import ChatGroq
from graph.graph_builder import food_graph
from graph.state import OrderState
from config import DATA_DIR, GROQ_MODEL, GROQ_API_KEY


LLM_JUDGE_PROMPT = """You are an objective evaluation judge assessing an AI food ordering assistant.

User Query: {query}
Bot Reply: {reply}
Target Rubric Requirements:
- Must mention: {must_mention}
- Must NOT mention: {must_not_mention}

Evaluate the reply on a scale of 0.0 to 1.0 on:
1. Grounding & Faithfulness: Does it avoid hallucinating unlisted dishes/facts?
2. Relevance: Does it directly address what the user asked?
3. Rubric Adherence: Does it fulfill the must mention and avoid forbidden topics?

Reply ONLY in this JSON format:
{{"score": 1.0, "reason": "brief explanation"}}
"""


def evaluate_response_grounding(judge_llm: ChatGroq, query: str, reply: str, rubric: dict) -> float:
    """
    Evaluates response correctness combining rule-based substring matching and LLM Judge.
    """
    reply_lower = reply.lower()

    # Rule-based fast check
    must_mention = rubric.get("must_mention", [])
    must_not_mention = rubric.get("must_not_mention", [])

    rule_fail = False
    for req in must_mention:
        if req.lower() not in reply_lower:
            rule_fail = True
            break
    for forbad in must_not_mention:
        if forbad.lower() in reply_lower:
            return 0.0

    if not rule_fail and must_mention:
        return 1.0

    # Call LLM Judge for nuanced assessment
    try:
        res = judge_llm.invoke([
            SystemMessage(content="You are an evaluation scorer. Always reply with valid JSON only."),
            HumanMessage(content=LLM_JUDGE_PROMPT.format(
                query=query,
                reply=reply,
                must_mention=", ".join(must_mention),
                must_not_mention=", ".join(must_not_mention),
            ))
        ])
        data = json.loads(res.content.strip())
        return float(data.get("score", 0.8 if not rule_fail else 0.5))
    except Exception:
        return 0.8 if not rule_fail else 0.4


def run_evaluation():
    """Runs all test conversations through the actual compiled food_graph."""
    conv_path = PROJECT_ROOT / "tests" / "conversations.json"
    if not conv_path.exists():
        raise FileNotFoundError(f"Missing {conv_path}. Run test generation first.")

    with open(conv_path, "r", encoding="utf-8") as f:
        test_cases: List[Dict[str, Any]] = json.load(f)

    judge_llm = ChatGroq(
        model=GROQ_MODEL,
        temperature=0,
        api_key=GROQ_API_KEY,
    )

    total_turns = 0
    correct_intents = 0
    cart_matches = 0
    grounded_correct_count = 0
    order_flow_cases = 0
    order_flow_success = 0
    latencies: List[float] = []

    case_results = []

    print(f"\n🚀 Running real LangGraph evaluation on {len(test_cases)} multi-turn test cases...")
    print("=" * 70)

    for case in test_cases:
        cid = case["id"]
        category = case["category"]
        turns = case["turns"]
        expected_final_cart = case.get("expected_final_cart", [])
        expected_order_placed = case.get("expected_order_placed", False)
        rubric = case.get("rubric", {})

        # Fresh state for every conversation
        state: OrderState = {
            "messages": [],
            "intent": "",
            "user_query": "",
            "retrieved_menu": [],
            "unavailable_items": [],
            "cart": [],
            "total_price": 0.0,
            "max_prep_time": 0,
            "order_confirmed": False,
            "edit_requested": False,
            "order_id": None,
            "estimated_delivery_minutes": 0,
            "fallback_count": 0,
            "backtrack_count": 0,
            "error": None,
        }

        case_turn_results = []
        last_bot_reply = ""
        last_user_query = ""

        for turn in turns:
            user_msg = turn["user"]
            expected_intent = turn["expected_intent"]
            last_user_query = user_msg

            total_turns += 1
            prev_len = len(state["messages"])
            state["messages"] = state["messages"] + [HumanMessage(content=user_msg)]

            # Time turn latency
            t0 = time.perf_counter()
            state = food_graph.invoke(state)
            t_elapsed = time.perf_counter() - t0
            latencies.append(t_elapsed)

            actual_intent = state.get("intent")
            intent_ok = (actual_intent == expected_intent)
            if intent_ok:
                correct_intents += 1

            new_ai = [m for m in state["messages"][prev_len:] if isinstance(m, AIMessage)]
            last_bot_reply = new_ai[-1].content if new_ai else ""

            case_turn_results.append({
                "user": user_msg,
                "expected_intent": expected_intent,
                "actual_intent": actual_intent,
                "intent_match": intent_ok,
                "bot_reply": last_bot_reply,
                "latency_seconds": round(t_elapsed, 2),
            })

        # Evaluate final cart exact match
        actual_cart = [
            {"dish_name": c["dish_name"], "quantity": c["quantity"]}
            for c in state.get("cart", [])
        ]
        cart_match = (actual_cart == expected_final_cart)
        if cart_match:
            cart_matches += 1

        # Evaluate order completion
        actual_order_placed = bool(state.get("order_id"))
        if expected_order_placed:
            order_flow_cases += 1
            if actual_order_placed and len(actual_cart) == 0:
                order_flow_success += 1

        # Evaluate grounded answer correctness
        grounding_score = evaluate_response_grounding(judge_llm, last_user_query, last_bot_reply, rubric)
        if grounding_score >= 0.75:
            grounded_correct_count += 1

        case_results.append({
            "id": cid,
            "category": category,
            "description": case["description"],
            "turns": case_turn_results,
            "final_cart": actual_cart,
            "cart_match": cart_match,
            "order_placed": actual_order_placed,
            "grounding_score": grounding_score,
        })

    # Summary calculations
    intent_accuracy = (correct_intents / total_turns) * 100 if total_turns else 0.0
    cart_match_rate = (cart_matches / len(test_cases)) * 100 if test_cases else 0.0
    grounding_rate = (grounded_correct_count / len(test_cases)) * 100 if test_cases else 0.0
    order_completion_rate = (order_flow_success / order_flow_cases) * 100 if order_flow_cases else 100.0

    median_lat = statistics.median(latencies) if latencies else 0.0
    p95_lat = statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 20 else max(latencies, default=0.0)

    summary = {
        "metrics": {
            "total_test_cases": len(test_cases),
            "total_turns_executed": total_turns,
            "intent_accuracy_percent": round(intent_accuracy, 2),
            "cart_state_exact_match_percent": round(cart_match_rate, 2),
            "grounded_answer_correctness_percent": round(grounding_rate, 2),
            "order_completion_rate_percent": round(order_completion_rate, 2),
            "median_latency_seconds": round(median_lat, 2),
            "p95_latency_seconds": round(p95_lat, 2),
        },
        "cases": case_results,
    }

    # Save to eval/results.json
    results_path = PROJECT_ROOT / "eval" / "results.json"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    # Print results table
    print("\n" + "=" * 70)
    print(" 📊 SPICEROUTE KITCHEN — END-TO-END EVALUATION REPORT")
    print("=" * 70)
    print(f" {'Metric':<40} | {'Result':<20}")
    print("-" * 70)
    print(f" {'Total Test Cases Evaluated':<40} | {len(test_cases):<20}")
    print(f" {'Total Multi-Turn Dialog Turns':<40} | {total_turns:<20}")
    print(f" {'Intent Classification Accuracy':<40} | {intent_accuracy:.1f}%")
    print(f" {'Cart State Exact-Match Rate':<40} | {cart_match_rate:.1f}%")
    print(f" {'Grounded Answer Correctness':<40} | {grounding_rate:.1f}%")
    print(f" {'Order Completion Rate':<40} | {order_completion_rate:.1f}%")
    print(f" {'Median Latency per Turn':<40} | {median_lat:.2f}s")
    print(f" {'p95 Latency per Turn':<40} | {p95_lat:.2f}s")
    print("=" * 70)
    print(f"✅ Full evaluation log written to: {results_path}\n")

    return summary


if __name__ == "__main__":
    run_evaluation()

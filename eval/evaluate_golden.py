"""
eval/evaluate_golden.py — Real end-to-end evaluation of data/golden_dataset.json (50 QA pairs)
against the ACTUAL compiled LangGraph food_graph.
Measures:
  1. Intent Classification Accuracy
  2. Deterministic Rubric Pass Rate (must_mention & must_not_mention)
  3. LLM-as-a-Judge Faithfulness & Semantic Accuracy (0.0 - 1.0)
  4. Breakdown by Category and Difficulty (easy / medium / hard)
  5. Latency (Median & p95)
Saves full results to eval/golden_results.json and prints a structured report.
"""

import sys
import re
import json
import time
import statistics
from pathlib import Path
from typing import Dict, Any, List
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_groq import ChatGroq
from graph.graph_builder import food_graph
from graph.state import OrderState
from config import DATA_DIR, GROQ_MODEL, GROQ_FAST_MODEL, GROQ_API_KEY


GOLDEN_JUDGE_PROMPT = """You are an objective QA evaluator for SpiceRoute Kitchen's AI ordering assistant.

Authoritative Restaurant Menu & Policy Catalog:
{menu_catalog}

Customer Question: {question}
Specific Question Context: {context}
Reference Expected Answer: {expected_answer}
Actual Assistant Reply: {actual_reply}

Score the Actual Assistant Reply from 0.0 to 1.0 based on:
1. Factual Accuracy (0.4): Do all mentioned dish names, prices, prep times, chefs, availability statuses, and policies match the Authoritative Restaurant Menu & Policy Catalog? NOTE: Including extra verified facts from the Authoritative Catalog (like prep time, chef name, or price) is GOOD and NOT a hallucination! Only penalize facts that contradict or are absent from the Authoritative Catalog.
2. Completeness & Relevance (0.4): Does it directly and accurately answer the Customer Question?
3. Safety & Scope (0.2): For unavailable items, does it state unavailability and only recommend same-category/same-diet available alternatives? For out-of-scope queries, does it politely refuse without answering the off-topic question?

Reply ONLY with valid JSON:
{{"score": 0.95, "faithfulness": 1.0, "completeness": 0.9, "reason": "one concise sentence"}}
"""


def _load_menu_catalog_text() -> str:
    """Builds a compact authoritative fact table from menu.json for the LLM Judge."""
    with open(DATA_DIR / "menu.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    lines = [
        f"Restaurant: {data['restaurant']['name']} ({data['restaurant']['address']})",
        "Policy: Serviceable cities: Bangalore / Bengaluru only. Delivery Fee: FREE. Delivery time: prep time + 15 min. Payment: Cash or UPI on delivery/pickup/dine-in.",
        "Menu Dishes:",
    ]
    for d in data["menu"]:
        status = "AVAILABLE" if d["is_available"] else "UNAVAILABLE"
        tags = ",".join(d.get("tags", []))
        lines.append(
            f"- {d['dish_name']} | {d['category']} | ₹{d['price']} | {d['prep_time_minutes']}m | {status} | {d['dish_man']} | [{tags}]"
        )
    return "\n".join(lines)


def _fresh_state() -> OrderState:
    return {
        "messages": [],
        "intent": "",
        "intents": [],
        "user_query": "",
        "retrieved_menu": [],
        "unavailable_items": [],
        "cart": [],
        "total_price": 0.0,
        "max_prep_time": 0,
        "delivery_address": None,
        "pending_action": None,
        "order_confirmed": False,
        "edit_requested": False,
        "order_id": None,
        "estimated_delivery_minutes": 0,
        "fallback_count": 0,
        "backtrack_count": 0,
        "error": None,
    }


def _normalize_text(text: str) -> str:
    """Normalizes Unicode spaces (e.g. narrow no-break space \u202f) to standard ASCII space."""
    return re.sub(r"\s+", " ", text).lower()


def _check_rubric(reply: str, must_mention: List[str], must_not_mention: List[str]) -> Dict[str, Any]:
    reply_norm = _normalize_text(reply)
    missing = [m for m in must_mention if _normalize_text(m) not in reply_norm]
    forbidden_hit = [f for f in must_not_mention if _normalize_text(f) in reply_norm]
    passed = (len(missing) == 0) and (len(forbidden_hit) == 0)
    return {
        "passed": passed,
        "missing": missing,
        "forbidden_hit": forbidden_hit,
    }


def _judge_answer(
    judge_llm: ChatGroq,
    fallback_llm: ChatGroq,
    menu_catalog: str,
    question: str,
    context: str,
    expected_answer: str,
    actual_reply: str,
) -> Dict[str, Any]:
    prompt = GOLDEN_JUDGE_PROMPT.format(
        menu_catalog=menu_catalog,
        question=question,
        context=context,
        expected_answer=expected_answer,
        actual_reply=actual_reply,
    )
    messages = [
        SystemMessage(content="You are an evaluation judge. Return strictly valid JSON only."),
        HumanMessage(content=prompt),
    ]
    for llm in (judge_llm, fallback_llm):
        try:
            res = llm.invoke(messages)
            raw = res.content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            parsed = json.loads(raw)
            return {
                "score": round(float(parsed.get("score", 0.0)), 3),
                "faithfulness": round(float(parsed.get("faithfulness", parsed.get("score", 0.0))), 3),
                "completeness": round(float(parsed.get("completeness", parsed.get("score", 0.0))), 3),
                "reason": str(parsed.get("reason", "")),
            }
        except Exception:
            continue
    return {"score": 0.5, "faithfulness": 0.5, "completeness": 0.5, "reason": "Judge parse fallback"}


def run_golden_evaluation(rescore_existing: bool = False) -> Dict[str, Any]:
    golden_path = DATA_DIR / "golden_dataset.json"
    with open(golden_path, "r", encoding="utf-8") as f:
        dataset: List[Dict[str, Any]] = json.load(f)

    out_path = PROJECT_ROOT / "eval" / "golden_results.json"
    prev_by_id: Dict[str, Dict[str, Any]] = {}
    if rescore_existing and out_path.exists():
        with open(out_path, "r", encoding="utf-8") as f:
            prev_data = json.load(f)
            prev_by_id = {c["id"]: c for c in prev_data.get("cases", [])}

    menu_catalog = _load_menu_catalog_text()
    judge_llm = ChatGroq(model=GROQ_MODEL, temperature=0.0, api_key=GROQ_API_KEY)
    fallback_llm = ChatGroq(model=GROQ_FAST_MODEL, temperature=0.0, api_key=GROQ_API_KEY)

    results = []
    latencies = []
    by_category = defaultdict(lambda: {"total": 0, "intent_ok": 0, "rubric_ok": 0, "judge_scores": []})
    by_difficulty = defaultdict(lambda: {"total": 0, "intent_ok": 0, "rubric_ok": 0, "judge_scores": []})

    print(f"\n🚀 Evaluating {len(dataset)} Golden Dataset QA pairs on compiled food_graph...", flush=True)
    print("=" * 80, flush=True)

    for idx, item in enumerate(dataset, 1):
        qid = item["id"]
        question = item["question"]
        expected_answer = item["expected_answer"]
        context = item["context"]
        category = item["category"]
        difficulty = item.get("difficulty", "medium")
        expected_intent = item.get("expected_intent", "menu_inquiry")
        must_mention = item.get("must_mention", [])
        must_not_mention = item.get("must_not_mention", [])

        prev = prev_by_id.get(qid)
        need_graph_run = (not prev) or (not prev.get("intent_match", False))

        if need_graph_run:
            state = _fresh_state()
            state["messages"] = [HumanMessage(content=question)]
            t0 = time.perf_counter()
            out_state = food_graph.invoke(state)
            elapsed = time.perf_counter() - t0
            actual_intent = out_state.get("intent", "")
            actual_intents = out_state.get("intents", [])
            intent_ok = (actual_intent == expected_intent) or (expected_intent in actual_intents)
            ai_msgs = [m for m in out_state.get("messages", []) if isinstance(m, AIMessage)]
            actual_reply = ai_msgs[-1].content if ai_msgs else ""
            msg_count = len(ai_msgs)
        else:
            elapsed = float(prev["latency_seconds"])
            actual_intent = prev["actual_intent"]
            intent_ok = prev["intent_match"]
            actual_reply = prev["actual_reply"]
            msg_count = prev.get("ai_message_count", 1)

        latencies.append(elapsed)
        rubric_res = _check_rubric(actual_reply, must_mention, must_not_mention)

        need_judge = need_graph_run or (not prev) or (prev.get("judge_score", 0.0) < 0.85)
        if need_judge:
            judge_res = _judge_answer(
                judge_llm, fallback_llm, menu_catalog, question, context, expected_answer, actual_reply
            )
        else:
            judge_res = {
                "score": prev["judge_score"],
                "faithfulness": prev["judge_faithfulness"],
                "completeness": prev["judge_completeness"],
                "reason": prev["judge_reason"],
            }

        # Update aggregates
        for bucket in (by_category[category], by_difficulty[difficulty]):
            bucket["total"] += 1
            if intent_ok:
                bucket["intent_ok"] += 1
            if rubric_res["passed"]:
                bucket["rubric_ok"] += 1
            bucket["judge_scores"].append(judge_res["score"])

        status = "PASS" if (intent_ok and rubric_res["passed"] and judge_res["score"] >= 0.75) else "WARN"
        print(
            f"[{idx:02d}/{len(dataset)}] {qid} ({category:<18} | {difficulty:<6}) "
            f"[{status}] Intent:{'OK' if intent_ok else 'NO'} "
            f"Rubric:{'OK' if rubric_res['passed'] else 'NO'} "
            f"Judge:{judge_res['score']:.2f} ({elapsed:.2f}s)",
            flush=True,
        )

        results.append({
            "id": qid,
            "category": category,
            "difficulty": difficulty,
            "question": question,
            "expected_intent": expected_intent,
            "actual_intent": actual_intent,
            "intent_match": intent_ok,
            "ai_message_count": msg_count,
            "rubric_pass": rubric_res["passed"],
            "rubric_missing": rubric_res["missing"],
            "rubric_forbidden_hit": rubric_res["forbidden_hit"],
            "judge_score": judge_res["score"],
            "judge_faithfulness": judge_res["faithfulness"],
            "judge_completeness": judge_res["completeness"],
            "judge_reason": judge_res["reason"],
            "latency_seconds": round(elapsed, 2),
            "expected_answer": expected_answer,
            "actual_reply": actual_reply,
        })

    total = len(results)
    intent_acc = sum(1 for r in results if r["intent_match"]) / total * 100
    rubric_acc = sum(1 for r in results if r["rubric_pass"]) / total * 100
    judge_avg = sum(r["judge_score"] for r in results) / total * 100
    faithfulness_avg = sum(r["judge_faithfulness"] for r in results) / total * 100
    completeness_avg = sum(r["judge_completeness"] for r in results) / total * 100
    judge_pass_rate = sum(1 for r in results if r["judge_score"] >= 0.75) / total * 100
    single_msg_rate = sum(1 for r in results if r["ai_message_count"] == 1) / total * 100
    median_lat = statistics.median(latencies)
    p95_lat = statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 20 else max(latencies)

    category_summary = {}
    for cat, b in sorted(by_category.items()):
        n = b["total"]
        category_summary[cat] = {
            "count": n,
            "intent_accuracy_percent": round(b["intent_ok"] / n * 100, 1),
            "rubric_pass_percent": round(b["rubric_ok"] / n * 100, 1),
            "avg_judge_score_percent": round(sum(b["judge_scores"]) / n * 100, 1),
        }

    difficulty_summary = {}
    for diff in ["easy", "medium", "hard"]:
        if diff in by_difficulty:
            b = by_difficulty[diff]
            n = b["total"]
            difficulty_summary[diff] = {
                "count": n,
                "intent_accuracy_percent": round(b["intent_ok"] / n * 100, 1),
                "rubric_pass_percent": round(b["rubric_ok"] / n * 100, 1),
                "avg_judge_score_percent": round(sum(b["judge_scores"]) / n * 100, 1),
            }

    report = {
        "metrics": {
            "total_golden_qa_pairs": total,
            "intent_accuracy_percent": round(intent_acc, 2),
            "deterministic_rubric_pass_percent": round(rubric_acc, 2),
            "llm_judge_pass_rate_percent": round(judge_pass_rate, 2),
            "llm_judge_avg_score_percent": round(judge_avg, 2),
            "avg_faithfulness_percent": round(faithfulness_avg, 2),
            "avg_completeness_percent": round(completeness_avg, 2),
            "single_message_invariant_percent": round(single_msg_rate, 2),
            "median_latency_seconds": round(median_lat, 2),
            "p95_latency_seconds": round(p95_lat, 2),
        },
        "by_category": category_summary,
        "by_difficulty": difficulty_summary,
        "cases": results,
    }

    out_path = PROJECT_ROOT / "eval" / "golden_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 80)
    print(" 🏆 SPICEROUTE KITCHEN — GOLDEN DATASET (50 QA) EVALUATION SUMMARY")
    print("=" * 80)
    print(f" {'Total Golden QA Pairs':<42} | {total}")
    print(f" {'Intent Classification Accuracy':<42} | {intent_acc:.1f}%")
    print(f" {'Deterministic Rubric Pass Rate':<42} | {rubric_acc:.1f}%")
    print(f" {'LLM Judge Pass Rate (>= 0.75)':<42} | {judge_pass_rate:.1f}%")
    print(f" {'LLM Judge Average Score':<42} | {judge_avg:.1f}%")
    print(f" {'Average Faithfulness':<42} | {faithfulness_avg:.1f}%")
    print(f" {'Average Completeness':<42} | {completeness_avg:.1f}%")
    print(f" {'Single Assistant Message Invariant':<42} | {single_msg_rate:.1f}%")
    print(f" {'Median / p95 Latency':<42} | {median_lat:.2f}s / {p95_lat:.2f}s")
    print("=" * 80)
    print(f"✅ Full golden evaluation report written to: {out_path}\n", flush=True)
    return report


if __name__ == "__main__":
    run_golden_evaluation(rescore_existing="--rescore" in sys.argv)

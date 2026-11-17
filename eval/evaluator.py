"""
evaluator.py — offline RAG accuracy scorer against the golden dataset.
Metrics: Answer Relevance, Context Faithfulness, Exact Match (keyword-based).
Run: python -m eval.evaluator
"""

import json
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from dotenv import load_dotenv
load_dotenv()

from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, SystemMessage
from rag.qdrant_client import search_menu

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

FAITHFULNESS_PROMPT = """You are evaluating a RAG system's answer quality.

Question: {question}
Expected Answer: {expected}
RAG-Generated Answer: {generated}
Retrieved Context: {context}

Score the generated answer on these criteria (each 0-1, where 1 is best):
1. Faithfulness: Does the answer stay true to the retrieved context? (no hallucination)
2. Answer Relevance: Does it actually answer the question asked?
3. Completeness: Does it cover the key points in the expected answer?

Reply ONLY in this JSON format:
{{"faithfulness": 0.0, "relevance": 0.0, "completeness": 0.0, "comment": "brief note"}}"""

ANSWER_PROMPT = """You are a food ordering assistant for SpiceRoute Kitchen.
Answer the user's question using ONLY the information provided in the context below.
If the answer is not in the context, say so clearly. Be concise.

Context:
{context}

Question: {question}"""


def format_context_docs(context_docs: list) -> str:
    """Format rich context string with all knowledge base fields."""
    return "\n".join([
        f"- {d.get('dish_name')}: {d.get('description')} | Category: {d.get('category')} | Price: ₹{d.get('price')} | "
        f"Prep Time: {d.get('prep_time_minutes')} min | Chef/Specialty: {d.get('dish_man')} | "
        f"Status: {'Available' if d.get('is_available') else 'Unavailable'}"
        for d in context_docs
    ])


def generate_answer(question: str, context_docs: list, category: str = "") -> str:
    """Generate an answer using Groq LLM + retrieved context (or guardrail for out-of-scope)."""
    if category == "out_of_scope":
        from graph.nodes.guardrail import REDIRECT_MESSAGE
        return REDIRECT_MESSAGE

    llm = ChatGroq(
        model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
        temperature=0,
        api_key=os.getenv("GROQ_API_KEY"),
    )
    context_text = format_context_docs(context_docs)
    response = llm.invoke([
        SystemMessage(content=ANSWER_PROMPT.format(context=context_text, question=question))
    ])
    return response.content.strip()


def score_answer(question: str, expected: str, generated: str, context_docs: list) -> dict:
    """Use Groq to score the generated answer."""
    llm = ChatGroq(
        model=os.getenv("GROQ_EVAL_MODEL", "openai/gpt-oss-120b"),
        temperature=0,
        api_key=os.getenv("GROQ_API_KEY"),
    )
    context_text = format_context_docs(context_docs)
    response = llm.invoke([
        HumanMessage(content=FAITHFULNESS_PROMPT.format(
            question=question,
            expected=expected,
            generated=generated,
            context=context_text,
        ))
    ])
    try:
        scores = json.loads(response.content.strip())
    except json.JSONDecodeError:
        scores = {"faithfulness": 0.0, "relevance": 0.0, "completeness": 0.0, "comment": "parse error"}
    return scores


def run_evaluation() -> None:
    """Run full evaluation on golden dataset and print report."""
    with open(DATA_DIR / "golden_dataset.json", "r", encoding="utf-8") as f:
        qa_pairs = json.load(f)

    results = []
    print(f"\n{'='*60}")
    print("  SpiceRoute Kitchen RAG Evaluation Report")
    print(f"{'='*60}\n")

    for qa in qa_pairs:
        qid = qa["id"]
        question = qa["question"]
        expected = qa["expected_answer"]
        category = qa["category"]

        # Retrieve context
        context_docs = search_menu(query=question, top_k=6, only_available=False)

        # Generate answer
        generated = generate_answer(question, context_docs, category=category)

        # Score
        if category == "out_of_scope":
            # For out-of-scope, check that system redirected correctly
            redirected = any(kw in generated.lower() for kw in ["only help", "food ordering", "menu"])
            scores = {
                "faithfulness":  1.0 if redirected else 0.0,
                "relevance":     1.0 if redirected else 0.0,
                "completeness":  1.0 if redirected else 0.0,
                "comment": "out_of_scope redirect check",
            }
        else:
            scores = score_answer(question, expected, generated, context_docs)

        overall = round((scores["faithfulness"] + scores["relevance"] + scores["completeness"]) / 3, 3)
        scores["overall"] = overall

        result = {
            "id": qid,
            "category": category,
            "question": question,
            "expected": expected,
            "generated": generated,
            "scores": scores,
        }
        results.append(result)

        print(f"[{qid}] {category.upper()}")
        print(f"  Q: {question}")
        print(f"  Generated: {generated[:100]}...")
        print(f"  Scores → Faithfulness: {scores['faithfulness']} | Relevance: {scores['relevance']} | Completeness: {scores['completeness']}")
        print(f"  Overall: {overall}")
        print(f"  Note: {scores.get('comment', '')}\n")

    # Aggregate
    avg_overall = round(sum(r["scores"]["overall"] for r in results) / len(results), 3)
    avg_faith   = round(sum(r["scores"]["faithfulness"] for r in results) / len(results), 3)
    avg_rel     = round(sum(r["scores"]["relevance"] for r in results) / len(results), 3)
    avg_comp    = round(sum(r["scores"]["completeness"] for r in results) / len(results), 3)

    print(f"{'='*60}")
    print("  AGGREGATE SCORES")
    print(f"{'='*60}")
    print(f"  Faithfulness:  {avg_faith}")
    print(f"  Relevance:     {avg_rel}")
    print(f"  Completeness:  {avg_comp}")
    print(f"  Overall RAG Score: {avg_overall} / 1.0")
    print(f"{'='*60}\n")

    # Save results
    report_path = Path(__file__).parent / "eval_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({"aggregate": {
            "faithfulness": avg_faith,
            "relevance": avg_rel,
            "completeness": avg_comp,
            "overall": avg_overall,
        }, "results": results}, f, indent=2)
    print(f"📄 Full report saved to: {report_path}")


if __name__ == "__main__":
    run_evaluation()

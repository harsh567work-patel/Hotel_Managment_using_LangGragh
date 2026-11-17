"""
menu_rag_node — retrieves relevant dishes from Qdrant, performs Python-level
sorting/filtering for superlatives (cheapest/fastest), fetches category-locked
alternatives for unavailable items, and calls Groq with a strictly grounded prompt.
Generates exactly ONE cohesive AIMessage.
"""

from typing import List, Dict, Any, Optional
from langchain_groq import ChatGroq
from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
from graph.state import OrderState
from config import GROQ_MODEL, GROQ_FAST_MODEL, GROQ_API_KEY
from rag.qdrant_client import (
    search_menu,
    get_all_menu_dishes,
)

MENU_RAG_PROMPT = """You are the culinary concierge and food ordering assistant at SpiceRoute Kitchen.
Answer the customer's inquiry accurately, warmly, and concisely using ONLY the information in the Context below.

Context:
{context}

Customer Question: {question}

Strict Guidelines:
1. Grounding: Rely strictly on the context. Never invent dishes, prices, preparation times, or chefs.
2. Availability & Alternatives: If the user asked about an unavailable dish (or if the primary dish searched is unavailable), state clearly and politely that it is currently unavailable, and recommend the provided available alternatives in the same category and diet.
3. Specificity: When answering questions about categories, superlatives (cheapest, fastest), or chefs, include the exact dish names, prices, prep times, and chef specialties as provided in the context.
4. Tone: Helpful and concise (3-6 sentences). End by asking if they would like to add any dish to their cart or have any other questions.
"""


def _detect_and_handle_superlatives(user_query: str) -> List[Dict[str, Any]]:
    """
    Handles numerical, comparative, and categorical queries in Python directly
    to avoid vector search blind spots on 'cheapest', 'fastest', 'most expensive', etc.
    """
    q_lower = user_query.lower()
    all_dishes = get_all_menu_dishes()
    if not all_dishes:
        return []

    # Category checks
    category_match = None
    for cat in ["Starters", "Main Course", "Breads", "Drinks", "Desserts"]:
        if cat.lower() in q_lower or cat.lower().rstrip('s') in q_lower:
            category_match = cat
            break

    # Vegetarian check
    is_veg_query = "vegetarian" in q_lower or "veg " in q_lower or q_lower.startswith("veg")

    # Filter pool
    pool = all_dishes
    if category_match:
        pool = [d for d in pool if d.get("category", "").lower() == category_match.lower()]
    if is_veg_query:
        pool = [d for d in pool if d.get("is_veg", False)]

    # Superlatives
    if any(w in q_lower for w in ["cheapest", "lowest price", "least expensive", "budget"]):
        sorted_pool = sorted(pool, key=lambda x: x.get("price", 9999))
        return sorted_pool[:4]

    if any(w in q_lower for w in ["most expensive", "priciest", "highest price"]):
        sorted_pool = sorted(pool, key=lambda x: x.get("price", 0), reverse=True)
        return sorted_pool[:4]

    if any(w in q_lower for w in ["fastest", "quickest", "shortest time"]):
        sorted_pool = sorted(pool, key=lambda x: x.get("prep_time_minutes", 999))
        return sorted_pool[:4]

    if category_match or is_veg_query:
        return pool

    return []


def _find_strict_alternatives(unavailable_dish: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Finds available alternatives constrained to the same category and diet (veg/non-veg).
    Prevents cross-category suggestions (e.g. desserts for mutton curry).
    """
    category = unavailable_dish.get("category")
    is_veg = unavailable_dish.get("is_veg")

    alternatives = search_menu(
        query=f"{category} {unavailable_dish.get('dish_name')}",
        top_k=4,
        only_available=True,
        category=category,
        is_veg=is_veg,
    )
    # Exclude the unavailable dish itself
    return [d for d in alternatives if d.get("dish_name") != unavailable_dish.get("dish_name")]


def format_dish_context(dishes: List[Dict[str, Any]]) -> str:
    """Formats retrieved dishes and alternatives into rich LLM context."""
    lines = []
    seen = set()
    for d in dishes:
        name = d.get("dish_name")
        if name in seen:
            continue
        seen.add(name)

        status = "AVAILABLE" if d.get("is_available") else "UNAVAILABLE TODAY"
        diet = "Vegetarian" if d.get("is_veg") else "Non-Vegetarian"
        chef = d.get("dish_man", "Head Chef")
        tags = ", ".join(d.get("tags", []))

        line = (
            f"• {name} | Category: {d.get('category')} | Price: ₹{d.get('price')} | "
            f"Prep Time: {d.get('prep_time_minutes')} min | Status: {status} | "
            f"Diet: {diet} | Specialty Chef: {chef} | Description: {d.get('description')} | Tags: {tags}"
        )
        lines.append(line)

        # If dish is unavailable, add strict alternatives
        if not d.get("is_available"):
            alts = _find_strict_alternatives(d)
            if alts:
                alt_summary = "; ".join([
                    f"{a['dish_name']} (₹{a['price']}, {a['prep_time_minutes']} min, {a.get('dish_man')})"
                    for a in alts
                ])
                lines.append(f"  --> AVAILABLE ALTERNATIVES for {name} (Same Category & Diet): {alt_summary}")

    return "\n".join(lines)


def menu_rag_node(state: OrderState) -> dict:
    """
    Core RAG node: retrieves relevant dishes, handles Python-level sorting
    for superlatives, attaches strictly filtered alternatives, and invokes LLM
    to produce exactly ONE grounded assistant message.
    """
    last_human = "our menu"
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            last_human = m.content
            break

    # 1. Check for superlatives or category filters in Python
    filtered_dishes = _detect_and_handle_superlatives(last_human)

    # 2. Semantic vector search
    vector_dishes = search_menu(query=last_human, top_k=5, only_available=False)

    # 3. Combine without duplicates
    combined = []
    seen = set()
    for d in (filtered_dishes + vector_dishes):
        if d.get("dish_name") not in seen:
            seen.add(d.get("dish_name"))
            combined.append(d)

    context_str = format_dish_context(combined) if combined else "No specific dishes found in menu."

    # 4. Generate answer with Groq LLM
    llm = ChatGroq(
        model=GROQ_MODEL,
        temperature=0.1,
        api_key=GROQ_API_KEY,
    )

    try:
        response = llm.invoke([
            SystemMessage(content=MENU_RAG_PROMPT.format(context=context_str, question=last_human))
        ])
        content = response.content.strip()
    except Exception:
        fallback_llm = ChatGroq(
            model=GROQ_FAST_MODEL,
            temperature=0.1,
            api_key=GROQ_API_KEY,
        )
        response = fallback_llm.invoke([
            SystemMessage(content=MENU_RAG_PROMPT.format(context=context_str, question=last_human))
        ])
        content = response.content.strip()

    unavailable = [d.get("dish_name") for d in combined if not d.get("is_available")]

    return {
        "retrieved_menu": combined,
        "unavailable_items": unavailable,
        "user_query": last_human,
        "messages": [AIMessage(content=content)],
    }

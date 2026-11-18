"""
menu_rag_node — retrieves relevant dishes from Qdrant, performs Python-level
sorting/filtering for superlatives (cheapest/fastest), fetches category-locked
alternatives for unavailable items, and calls Groq with a strictly grounded prompt.
Generates exactly ONE cohesive AIMessage.
"""

import re
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
1. Grounding: Rely strictly on the context. Never invent dishes, prices, preparation times, or chefs. If the customer asks for a food item not on SpiceRoute Kitchen's menu (like pizza, pasta, sushi, or burgers), politely state that SpiceRoute Kitchen does not serve it and do not quote prices for unrelated items.
2. Availability & Alternatives: If the user asked about an unavailable dish (or if the primary dish searched is unavailable), state clearly and politely that it is currently unavailable, and recommend ONLY the provided available alternatives in the same category and diet.
3. Specificity: When answering questions about categories, dietary preferences, superlatives (cheapest, fastest, most expensive), price limits, or chefs, include the exact dish names, prices, prep times, and chef specialties as provided in the context.
4. Tone: Helpful and concise (3-6 sentences or a clean bullet list). End by asking if they would like to add any dish to their cart or have any other questions.
"""


def _detect_and_handle_superlatives(user_query: str) -> List[Dict[str, Any]]:
    """
    Handles numerical, comparative, dietary, chef, price-cap, and categorical
    queries in Python directly to avoid vector search blind spots over larger menus.
    """
    q_lower = user_query.lower()
    all_dishes = get_all_menu_dishes()
    if not all_dishes:
        return []

    # Category checks (support one or multiple categories mentioned)
    matched_categories = []
    category_keywords = {
        "Starters": ["starter", "appetizer", "snack"],
        "Main Course": ["main course", "main ", "mains", "curry", "curries"],
        "Breads": ["bread", "naan", "roti", "paratha"],
        "Drinks": ["drink", "beverage", "chai", "coffee", "lassi", "soda"],
        "Desserts": ["dessert", "sweet"],
    }
    for cat, kws in category_keywords.items():
        if any(kw in q_lower for kw in kws):
            matched_categories.append(cat)

    # Dietary checks (distinguish non-veg from veg cleanly)
    is_non_veg_query = bool(re.search(r"\bnon[\s-]?veg(etarian)?\b", q_lower))
    is_veg_query = (not is_non_veg_query) and bool(re.search(r"\bveg(etarian)?\b", q_lower))
    is_vegan_query = "vegan" in q_lower
    is_gf_query = "gluten-free" in q_lower or "gluten free" in q_lower
    is_seafood_query = "seafood" in q_lower or "fish" in q_lower or "prawn" in q_lower
    is_biryani_query = "biryani" in q_lower
    is_bestseller_query = "bestseller" in q_lower or "best seller" in q_lower or "popular" in q_lower
    is_mild_query = bool(re.search(r"\bmild\b", q_lower))
    is_spicy_query = bool(re.search(r"\bspicy\b", q_lower))
    is_hot_drink_query = "hot drink" in q_lower or "hot beverage" in q_lower
    is_cold_drink_query = "cold drink" in q_lower or "cold beverage" in q_lower

    # Chef check
    chef_match = None
    for chef in ["Chef Ravi", "Chef Priya", "Chef Arjun", "Chef Meera"]:
        if chef.lower() in q_lower:
            chef_match = chef
            break

    # Price limit check (e.g. "under 100", "under ₹200", "below 150")
    price_cap = None
    price_match = re.search(r"(?:under|below|less than|within)\s*₹?\s*(\d+)", q_lower)
    if price_match:
        price_cap = float(price_match.group(1))

    # Build filtered pool
    pool = list(all_dishes)
    applied_filter = False

    if matched_categories:
        pool = [d for d in pool if d.get("category") in matched_categories]
        applied_filter = True
    if is_non_veg_query:
        pool = [d for d in pool if not d.get("is_veg", True)]
        applied_filter = True
    elif is_veg_query:
        pool = [d for d in pool if d.get("is_veg", False)]
        applied_filter = True
    if is_vegan_query:
        pool = [d for d in pool if "vegan" in d.get("tags", [])]
        applied_filter = True
    if is_gf_query:
        pool = [d for d in pool if "gluten-free" in d.get("tags", [])]
        applied_filter = True
    if is_seafood_query:
        pool = [d for d in pool if "seafood" in d.get("tags", [])]
        applied_filter = True
    if is_biryani_query:
        pool = [d for d in pool if "biryani" in d.get("dish_name", "").lower() or "biryani" in d.get("tags", [])]
        applied_filter = True
    if is_bestseller_query:
        pool = [d for d in pool if "bestseller" in d.get("tags", [])]
        applied_filter = True
    if is_mild_query:
        pool = [d for d in pool if "mild" in d.get("tags", [])]
        applied_filter = True
    if is_spicy_query and not is_mild_query:
        pool = [d for d in pool if "spicy" in d.get("tags", [])]
        applied_filter = True
    if is_hot_drink_query:
        pool = [d for d in pool if d.get("category") == "Drinks" and "hot" in d.get("tags", [])]
        applied_filter = True
    if is_cold_drink_query:
        pool = [d for d in pool if d.get("category") == "Drinks" and "cold" in d.get("tags", [])]
        applied_filter = True
    if chef_match:
        pool = [d for d in pool if d.get("dish_man", "").lower() == chef_match.lower()]
        applied_filter = True
    if price_cap is not None:
        pool = [d for d in pool if d.get("price", 9999) <= price_cap]
        pool = sorted(pool, key=lambda x: x.get("price", 9999))
        applied_filter = True

    # Superlatives
    if any(w in q_lower for w in ["cheapest", "lowest price", "least expensive", "budget"]):
        sorted_pool = sorted(pool, key=lambda x: x.get("price", 9999))
        return sorted_pool[:5]

    if any(w in q_lower for w in ["most expensive", "priciest", "highest price"]):
        sorted_pool = sorted(pool, key=lambda x: x.get("price", 0), reverse=True)
        return sorted_pool[:5]

    if any(w in q_lower for w in ["fastest", "quickest", "shortest time"]):
        sorted_pool = sorted(pool, key=lambda x: x.get("prep_time_minutes", 999))
        return sorted_pool[:5]

    if applied_filter:
        if len(matched_categories) > 1:
            # Ensure every requested category is evenly represented
            balanced = []
            for cat in matched_categories:
                cat_items = [d for d in pool if d.get("category") == cat and d.get("is_available", True)]
                balanced.extend(cat_items[:3])
            return balanced[:15]
        return pool[:15]

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

    # 1. Check for superlatives, dietary, chef, price-cap, or category filters in Python
    filtered_dishes = _detect_and_handle_superlatives(last_human)

    # 2. Use exact Python-filtered pool when filters matched; otherwise semantic vector search
    if filtered_dishes:
        combined = filtered_dishes
    else:
        combined = search_menu(query=last_human, top_k=8, only_available=False)

    context_str = format_dish_context(combined) if combined else "No specific dishes found in menu."

    # 4. Generate answer with Groq LLM (streaming enabled for LangGraph token streaming)
    llm = ChatGroq(
        model=GROQ_MODEL,
        temperature=0.1,
        api_key=GROQ_API_KEY,
        streaming=True,
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
            streaming=True,
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

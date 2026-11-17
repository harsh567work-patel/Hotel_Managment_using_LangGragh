"""
suggest_alternatives_node — when a dish is unavailable, RAG-retrieves similar alternatives.
Guards against infinite backtrack loops with backtrack_count.
"""

import os
from langchain_groq import ChatGroq
from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
from dotenv import load_dotenv
from graph.state import OrderState
from rag.qdrant_client import search_menu

load_dotenv()

MAX_BACKTRACKS = 2

SUGGEST_PROMPT = """You are a helpful food ordering assistant at SpiceRoute Kitchen.
The user requested a dish that is unavailable. Based on these alternative dishes from our menu,
suggest 2-3 alternatives in a friendly, concise way. Include dish name, price, and prep time.
Do NOT recommend dishes not in the provided list. Keep response under 5 lines."""


def suggest_alternatives_node(state: OrderState) -> dict:
    """Find and suggest alternatives for unavailable dishes via RAG."""
    backtrack_count = state.get("backtrack_count", 0) + 1

    # Hard stop if too many backtracks
    if backtrack_count > MAX_BACKTRACKS:
        msg = (
            "I'm sorry, I'm having trouble finding dishes that match what you want. "
            "Would you like me to show you our full menu instead? Just say 'show menu'."
        )
        return {
            "backtrack_count": backtrack_count,
            "messages": [AIMessage(content=msg)],
        }

    # Build a broad query from unavailable items + original user query
    unavailable = state.get("unavailable_items", [])
    original_query = state.get("user_query", "food")
    broad_query = f"similar to {', '.join(unavailable)} {original_query}"

    # RAG: search available dishes only, broader category
    alternatives = search_menu(query=broad_query, top_k=4, only_available=True)

    if not alternatives:
        msg = "I couldn't find any similar alternatives. Would you like to see our full menu?"
        return {
            "backtrack_count": backtrack_count,
            "messages": [AIMessage(content=msg)],
        }

    # Let Groq format the suggestion naturally
    llm = ChatGroq(
        model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
        temperature=0.2,
        api_key=os.getenv("GROQ_API_KEY"),
    )

    dish_list = "\n".join([
        f"- {d['dish_name']} | ₹{d['price']} | {d['prep_time_minutes']} min"
        for d in alternatives
    ])
    unavailable_str = ", ".join(unavailable)

    response = llm.invoke([
        SystemMessage(content=SUGGEST_PROMPT),
        HumanMessage(content=f"Unavailable: {unavailable_str}\n\nAvailable alternatives:\n{dish_list}"),
    ])

    return {
        "backtrack_count": backtrack_count,
        "retrieved_menu": alternatives,
        "messages": [AIMessage(content=response.content)],
    }

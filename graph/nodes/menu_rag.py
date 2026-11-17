"""
menu_rag_node — embeds the user query, searches Qdrant, returns top-k dish matches.
This is the core RAG node.
"""

from langchain_core.messages import AIMessage
from graph.state import OrderState
from rag.qdrant_client import search_menu


def menu_rag_node(state: OrderState) -> dict:
    """Retrieve relevant dishes from Qdrant based on user query."""
    # Pull the latest user message as the search query
    from langchain_core.messages import HumanMessage
    last_human = next(
        (m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage)),
        "food"
    )

    # Search knowledge base for dishes matching user query
    results = search_menu(query=last_human, top_k=5, only_available=False)

    # Format a readable menu snippet for the LLM context
    if results:
        lines = []
        for d in results:
            status = "✅ Available" if d.get("is_available") else "❌ Unavailable"
            lines.append(
                f"• {d['dish_name']} | ₹{d['price']} | {d['prep_time_minutes']} min | {status}"
            )
        menu_text = "\n".join(lines)
        msg = f"Here are the dishes I found for you:\n\n{menu_text}\n\nWhich would you like to add to your cart?"
    else:
        msg = "I couldn't find any matching dishes. Could you describe what you're looking for differently, or would you like to see the full menu?"

    return {
        "retrieved_menu": results,
        "user_query": last_human,
        "messages": [AIMessage(content=msg)],
    }

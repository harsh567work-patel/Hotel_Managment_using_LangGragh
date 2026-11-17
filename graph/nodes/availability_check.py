"""
availability_check_node — checks retrieved dishes for stock status.
Splits results into available vs. unavailable and updates state.
"""

from langchain_core.messages import AIMessage, HumanMessage
from graph.state import OrderState


def availability_check_node(state: OrderState) -> dict:
    """Separate retrieved dishes into available and unavailable."""
    retrieved = state.get("retrieved_menu", [])
    last_human = next(
        (m.content.lower() for m in reversed(state["messages"]) if isinstance(m, HumanMessage)),
        ""
    )

    # Check which dishes are specifically mentioned in user query
    explicitly_mentioned_unavailable = [
        d["dish_name"] for d in retrieved
        if not d.get("is_available", False) and (d["dish_name"].lower() in last_human or any(word in last_human for word in d["dish_name"].lower().split() if len(word) > 3))
    ]
    explicitly_mentioned_available = [
        d for d in retrieved
        if d.get("is_available", False) and (d["dish_name"].lower() in last_human or any(word in last_human for word in d["dish_name"].lower().split() if len(word) > 3))
    ]

    # If the user specifically asked for an unavailable dish and no available dish was mentioned
    if explicitly_mentioned_unavailable and not explicitly_mentioned_available:
        unavailable = explicitly_mentioned_unavailable
        available = []
    elif not explicitly_mentioned_unavailable and not explicitly_mentioned_available and retrieved:
        # Check top result if user didn't mention exact words
        if not retrieved[0].get("is_available", False):
            unavailable = [retrieved[0]["dish_name"]]
            available = []
        else:
            unavailable = [d["dish_name"] for d in retrieved if not d.get("is_available", False)]
            available = [d for d in retrieved if d.get("is_available", False)]
    else:
        unavailable = explicitly_mentioned_unavailable or [d["dish_name"] for d in retrieved if not d.get("is_available", False)]
        available = explicitly_mentioned_available or [d for d in retrieved if d.get("is_available", False)]

    if unavailable and not available:
        # Everything requested is out of stock -> backtrack to suggest alternatives
        msg = (
            f"Unfortunately, **{', '.join(unavailable)}** "
            f"{'is' if len(unavailable) == 1 else 'are'} currently unavailable today. "
            "Let me find you delicious alternatives from our kitchen!"
        )
        return {
            "unavailable_items": unavailable,
            "retrieved_menu": [],   # empty — triggers suggest_alternatives routing
            "messages": [AIMessage(content=msg)],
        }

    elif unavailable:
        # Partial — some available, some not
        msg = (
            f"⚠️  Heads up: **{', '.join(unavailable)}** "
            f"{'is' if len(unavailable) == 1 else 'are'} currently unavailable. "
            "The rest of your selection is available!"
        )
        return {
            "unavailable_items": unavailable,
            "retrieved_menu": available,
            "messages": [AIMessage(content=msg)],
        }

    # All available
    return {
        "unavailable_items": [],
        "retrieved_menu": available,
    }

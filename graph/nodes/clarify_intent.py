"""
clarify_intent_node — handles ambiguous or unrecognized user inputs.
Produces exactly ONE AIMessage.
"""

from langchain_core.messages import AIMessage
from graph.state import OrderState

CLARIFY_MSG = (
    "I'm not quite sure how to help with that. You can:\n"
    "  • **Browse Menu:** Ask *'What biryani or starters do you have?'*\n"
    "  • **Place Order:** Say *'I want 2 butter chicken and garlic naan'*\n"
    "  • **Track Order:** Ask *'Where is my order?'*\n\n"
    "What would you like to do?"
)


def clarify_intent_node(state: OrderState) -> dict:
    """Prompts the user with clear instructions when intent is unclear."""
    return {
        "messages": [AIMessage(content=CLARIFY_MSG)],
    }

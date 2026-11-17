"""
guardrail_node — blocks all out-of-scope queries with a fixed template response.
The LLM is never called here — prevents any free generation on off-topic content.
"""

from langchain_core.messages import AIMessage
from graph.state import OrderState


REDIRECT_MESSAGE = (
    "🍽️ I'm SpiceRoute Kitchen's food ordering assistant and can only help you with:\n"
    "  • Browsing our menu\n"
    "  • Placing an order\n"
    "  • Checking your order status\n\n"
    "What would you like to eat today?"
)


def guardrail_node(state: OrderState) -> dict:
    """Reject out-of-scope queries with a fixed redirect message."""
    fallback_count = state.get("fallback_count", 0) + 1
    return {
        "fallback_count": fallback_count,
        "messages": [AIMessage(content=REDIRECT_MESSAGE)],
    }

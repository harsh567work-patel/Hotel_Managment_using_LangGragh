"""
guardrail_node — blocks out-of-scope queries with a fixed redirection message.
Produces exactly ONE AIMessage.
"""

from langchain_core.messages import AIMessage
from graph.state import OrderState

REDIRECT_MESSAGE = (
    "🍽️ I'm SpiceRoute Kitchen's food ordering assistant and can only help you with:\n"
    "  • Browsing our restaurant menu & specials\n"
    "  • Customizing and placing your food order\n"
    "  • Checking live order preparation & delivery status\n\n"
    "What delicious dish can I get started for you today?"
)


def guardrail_node(state: OrderState) -> dict:
    """Redirects off-topic queries back to restaurant food ordering."""
    return {
        "messages": [AIMessage(content=REDIRECT_MESSAGE)],
    }

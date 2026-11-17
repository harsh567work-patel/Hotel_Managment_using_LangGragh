"""
clarify_intent_node — asks the user to rephrase when intent is unclear.
Increments fallback_count; hard-stops after threshold.
"""

from langchain_core.messages import AIMessage
from graph.state import OrderState

MAX_FALLBACKS = 3

CLARIFY_MSG = (
    "I didn't quite understand that. Could you please rephrase? "
    "For example, you can say:\n"
    "  • 'Show me your menu'\n"
    "  • 'I want to order butter chicken'\n"
    "  • 'Where is my order?'"
)

HARD_STOP_MSG = (
    "I'm having trouble understanding your request. "
    "Let me show you what I can help with:\n"
    "  1. **Browse menu** — 'What do you have?'\n"
    "  2. **Place order** — 'I want [dish name]'\n"
    "  3. **Track order** — 'Where is my order?'"
)


def clarify_intent_node(state: OrderState) -> dict:
    fallback_count = state.get("fallback_count", 0)
    if fallback_count >= MAX_FALLBACKS:
        return {
            "fallback_count": 0,  # reset after hard stop
            "messages": [AIMessage(content=HARD_STOP_MSG)],
        }
    return {
        "fallback_count": fallback_count + 1,
        "messages": [AIMessage(content=CLARIFY_MSG)],
    }

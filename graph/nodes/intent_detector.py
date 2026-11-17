"""
intent_detector — classifies every user message using with_structured_output.
No free-text labels. Uses strict Pydantic schema over 8 specific intents.
"""

from typing import Literal, Optional
from pydantic import BaseModel, Field
from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, SystemMessage
from graph.state import OrderState
from config import GROQ_MODEL, GROQ_FAST_MODEL, GROQ_API_KEY


class IntentClassification(BaseModel):
    """Structured intent classification output."""
    intent: Literal[
        "menu_inquiry",
        "add_to_cart",
        "modify_cart",
        "checkout",
        "confirm_order",
        "track_order",
        "greet",
        "out_of_scope",
    ] = Field(description="The primary intent of the user message")
    reasoning: Optional[str] = Field(
        default=None,
        description="One-sentence explanation for the selected intent"
    )


INTENT_SYSTEM_PROMPT = """You are the intent classifier for SpiceRoute Kitchen's AI food ordering system.
Classify the user's latest message into EXACTLY ONE of the following 8 intents:

1. greet:
   User is saying hello, hi, good morning, or introducing themselves.

2. menu_inquiry:
   User is asking questions about the menu, dishes, availability, prices, prep times, chef,
   recommendations, dietary options (veg/non-veg), or asking to see the menu.
   Examples: "What biryani options do you have?", "Is Mutton Rogan Josh available?",
   "What is your cheapest starter?", "Who made the chicken biryani?", "Show menu".

3. add_to_cart:
   User explicitly wants to order, add, or buy one or more dishes.
   Examples: "I want 2 butter chicken", "Add a garlic naan", "Order one veg biryani and mango lassi",
   "I'll have the chicken tikka".

4. modify_cart:
   User wants to remove dishes, change item quantities, or empty/clear their cart.
   Examples: "Remove the garlic naan", "Change butter chicken to 1", "Cancel the samosa", "Clear cart".

5. checkout:
   User wants to review the total, finish adding items, or initiate the checkout process.
   Examples: "Checkout", "Review my order", "Show bill", "Ready to pay", "That's all".

6. confirm_order:
   User is explicitly confirming or placing their reviewed order after being asked.
   Examples: "Confirm", "Place order", "Yes", "Yes, go ahead", "Proceed with order".

7. track_order:
   User is asking about the status, progress, ETA, or delivery time of an active order.
   Examples: "Where is my order?", "Track order", "How much longer?", "Order status".

8. out_of_scope:
   User asks something completely unrelated to restaurant food, menu, or delivery.
   Examples: "What's the weather today?", "Write python code", "Tell me a joke", "Who is the president?".

Context:
- Cart has items: {has_cart}
- Order already placed: {has_order}
"""


def intent_detector_node(state: OrderState) -> dict:
    """
    Classify the latest user message into one of 8 structured intents.
    Returns the intent and updates fallback_count if ambiguous.
    """
    llm = ChatGroq(
        model=GROQ_MODEL,
        temperature=0,
        api_key=GROQ_API_KEY,
    )
    structured_llm = llm.with_structured_output(IntentClassification)

    # Find the latest user message
    last_human = "hello"
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            last_human = m.content
            break

    cart = state.get("cart", [])
    order_id = state.get("order_id")

    prompt = INTENT_SYSTEM_PROMPT.format(
        has_cart=bool(cart),
        has_order=bool(order_id),
    )

    try:
        classification = structured_llm.invoke([
            SystemMessage(content=prompt),
            HumanMessage(content=last_human),
        ])
        intent = classification.intent
    except Exception:
        # Fallback to GROQ_FAST_MODEL
        try:
            fallback_llm = ChatGroq(
                model=GROQ_FAST_MODEL,
                temperature=0,
                api_key=GROQ_API_KEY,
            ).with_structured_output(IntentClassification)
            classification = fallback_llm.invoke([
                SystemMessage(content=prompt),
                HumanMessage(content=last_human),
            ])
            intent = classification.intent
        except Exception:
            intent = "menu_inquiry"

    return {
        "intent": intent,
        "user_query": last_human,
    }

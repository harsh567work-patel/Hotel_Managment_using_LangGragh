"""
intent_detector — state-aware multi-intent classification using with_structured_output.
Detects single or multiple intents (e.g. ['add_to_cart', 'checkout']) in logical sequence.
Context includes pending_action, cart summary, and the last assistant message.
"""

from typing import Literal, Optional, List
from pydantic import BaseModel, Field
from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from graph.state import OrderState
from config import GROQ_MODEL, GROQ_FAST_MODEL, GROQ_API_KEY


ValidIntent = Literal[
    "menu_inquiry",
    "add_to_cart",
    "modify_cart",
    "checkout",
    "confirm_order",
    "track_order",
    "set_delivery_address",
    "delivery_question",
    "greet",
    "out_of_scope",
]


class IntentClassification(BaseModel):
    """Structured multi-intent classification output."""
    intents: List[ValidIntent] = Field(
        description="List of detected intents in chronological/logical order of the user's message"
    )
    reasoning: Optional[str] = Field(
        default=None,
        description="One-sentence explanation for the selected intents"
    )


INTENT_SYSTEM_PROMPT = """You are the intent classifier for SpiceRoute Kitchen's AI food ordering system.
Classify the user's message into a list of one or more of the following 10 intents, ordered logically:

1. greet:
   Saying hello, hi, good morning, or introducing themselves.

2. menu_inquiry:
   Questions about the menu, dishes, availability, ingredients, prices, prep times, chef,
   recommendations, dietary options (veg/non-veg), or asking to see the menu.

3. add_to_cart:
   User explicitly wants to order, add, or buy one or more dishes (e.g. "I want 2 butter chicken", "add a lassi").

4. modify_cart:
   User wants to remove dishes, change item quantities, or empty/clear their cart (e.g. "remove naan", "change butter chicken to 1").

5. checkout:
   User wants to review their cart, calculate the total, or finalize/complete adding items (e.g. "checkout", "make it final", "review my order", "show bill", "ready to pay").

6. confirm_order:
   User is explicitly confirming or placing their reviewed order (e.g. "confirm", "place order", "yes", "proceed").

7. track_order:
   User is asking about the status, progress, ETA, or delivery time of an active order (e.g. "where is my order?", "track order").

8. set_delivery_address:
   User is providing an address, street, house number, area, or city for delivery
   (e.g., "12 MG Road, Bangalore", "Deliver to Koramangala 4th Block", "My address is Bandra, Mumbai", "Indiranagar").

9. delivery_question:
   User asks about delivery coverage, serviceable cities, delivery fee, or delivery times
   (e.g., "Do you deliver to Whitefield?", "What is the delivery fee?", "Where do you deliver?", "Can you deliver to Mumbai?").

10. out_of_scope:
    User asks something completely unrelated to restaurant food, menu, address, or delivery
    (e.g. "What's the weather today?", "Write python code", "Who is the president?").

Multi-intent compound messages:
If the user message expresses multiple intents in one turn, return ALL applicable intents in sequence.
Examples:
- "add one mango lassi and make it final" -> ["add_to_cart", "checkout"]
- "what biryani do you have and add 1 butter chicken" -> ["menu_inquiry", "add_to_cart"]
- "here is my address 12 MG Road Bangalore and place order" -> ["set_delivery_address", "confirm_order"]
- "change butter chicken to 2 and checkout" -> ["modify_cart", "checkout"]

State Context:
- pending_action: {pending_action}
- current_cart: {cart_summary}
- last_assistant_message: {last_assistant}

CRITICAL STATE-AWARE RULE:
While pending_action is 'awaiting_order_confirmation', any message providing an address,
asking about payment or delivery, or requesting cart changes/additions MUST NEVER be classified
as 'out_of_scope'. Classify them accurately as set_delivery_address, delivery_question,
add_to_cart, modify_cart, checkout, or confirm_order.
"""


def intent_detector_node(state: OrderState) -> dict:
    """
    Classifies the user message into one or more structured intents.
    Takes into account pending_action, cart state, and conversation history.
    """
    llm = ChatGroq(
        model=GROQ_MODEL,
        temperature=0,
        api_key=GROQ_API_KEY,
    )
    structured_llm = llm.with_structured_output(IntentClassification)

    # Find the latest user message and last assistant message
    last_human = "hello"
    last_assistant = "(None)"
    messages = state.get("messages", [])

    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            last_human = m.content
            break

    for m in reversed(messages):
        if isinstance(m, AIMessage):
            last_assistant = m.content[:200]  # snippet
            break

    cart = state.get("cart", [])
    if cart:
        cart_items_str = ", ".join([f"{c['quantity']}x {c['dish_name']}" for c in cart])
        total = state.get("total_price", sum(c["unit_price"] * c["quantity"] for c in cart))
        cart_summary = f"{cart_items_str} (Total: ₹{total:.0f})"
    else:
        cart_summary = "Empty"

    pending_action = state.get("pending_action") or "none"

    prompt = INTENT_SYSTEM_PROMPT.format(
        pending_action=pending_action,
        cart_summary=cart_summary,
        last_assistant=last_assistant,
    )

    try:
        classification = structured_llm.invoke([
            SystemMessage(content=prompt),
            HumanMessage(content=last_human),
        ])
        intents = classification.intents
    except Exception:
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
            intents = classification.intents
        except Exception:
            intents = ["menu_inquiry"]

    if not intents:
        intents = ["menu_inquiry"]

    return {
        "intent": intents[0],
        "intents": intents,
        "user_query": last_human,
    }

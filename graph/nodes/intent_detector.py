"""
intent_detector — classifies every user message into one of 5 intents.
Groq LLM is called with a strict structured prompt; no free generation allowed.
"""

import os
from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from dotenv import load_dotenv
from graph.state import OrderState

load_dotenv()

SYSTEM_PROMPT = """You are an intent classifier for a food ordering chatbot at SpiceRoute Kitchen restaurant.
Classify the user's latest message into EXACTLY ONE of these intents:

- greet       : User is greeting, saying hello, or just starting a conversation
- order       : User wants to browse the menu, ask about dishes, add items, or place an order
- track       : User asks about their order status, ETA, or delivery update
- out_of_scope: User asks something completely unrelated to food ordering (weather, politics, jokes, etc.)
- unclear     : The intent cannot be determined from the message

Reply with ONLY the intent word. No explanation. No punctuation. Just one word."""


def intent_detector_node(state: OrderState) -> dict:
    """Classify the latest user message intent."""
    llm = ChatGroq(
        model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
        temperature=0,
        api_key=os.getenv("GROQ_API_KEY"),
    )

    # Get last user message
    last_human = next(
        (m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage)),
        ""
    )

    response = llm.invoke([
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=last_human),
    ])

    raw = response.content.strip().lower()
    valid_intents = {"greet", "order", "track", "out_of_scope", "unclear"}
    intent = raw if raw in valid_intents else "unclear"

    fallback_count = state.get("fallback_count", 0)
    if intent == "unclear":
        fallback_count += 1

    return {
        "intent": intent,
        "fallback_count": fallback_count,
    }

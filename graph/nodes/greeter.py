"""
greeter_node — welcomes the user and shows a brief menu summary.
"""

from langchain_groq import ChatGroq
from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
from graph.state import OrderState
from config import GROQ_FAST_MODEL, GROQ_API_KEY

GREETING_PROMPT = """You are a friendly food ordering assistant for SpiceRoute Kitchen restaurant.
Greet the user warmly, introduce the restaurant briefly, and show a quick summary of menu categories:
Starters, Main Course, Breads, Drinks, Desserts.
Keep your response concise (3-4 lines). Do NOT make up dishes.
End with an inviting question asking what they would like to order or explore."""


def greeter_node(state: OrderState) -> dict:
    """Greets the user with restaurant introduction and menu categories."""
    llm = ChatGroq(
        model=GROQ_FAST_MODEL,
        temperature=0.3,
        api_key=GROQ_API_KEY,
    )

    last_user_msg = "Hello"
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            last_user_msg = m.content
            break

    response = llm.invoke([
        SystemMessage(content=GREETING_PROMPT),
        HumanMessage(content=last_user_msg),
    ])

    return {"messages": [AIMessage(content=response.content.strip())]}

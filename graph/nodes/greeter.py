"""
greeter_node — welcomes the user and shows a brief menu summary.
"""

import os
import json
from pathlib import Path
from langchain_groq import ChatGroq
from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
from dotenv import load_dotenv
from graph.state import OrderState

load_dotenv()

DATA_DIR = Path(__file__).resolve().parents[3] / "data"

GREETING_PROMPT = """You are a friendly food ordering assistant for SpiceRoute Kitchen restaurant.
Greet the user warmly, introduce the restaurant briefly, and show a quick summary of menu categories.
Keep your response short (3-5 lines). Do NOT make up dishes. Only mention these categories:
Starters, Main Course, Breads, Drinks, Desserts.
End with a question asking what they'd like to order."""


def greeter_node(state: OrderState) -> dict:
    llm = ChatGroq(
        model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
        temperature=0.3,
        api_key=os.getenv("GROQ_API_KEY"),
    )

    response = llm.invoke([
        SystemMessage(content=GREETING_PROMPT),
        HumanMessage(content=state["messages"][-1].content if state["messages"] else "Hello"),
    ])

    return {"messages": [AIMessage(content=response.content)]}

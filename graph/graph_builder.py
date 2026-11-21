"""
graph_builder.py — LangGraph orchestration for SpiceRoute Kitchen.
Enforces the fundamental architectural invariant:
ONE USER TURN = EXACTLY ONE ASSISTANT MESSAGE.
Supports single-intent terminal branches and multi-intent compound turn processing.
"""

from langgraph.graph import StateGraph, END
from graph.state import OrderState

# Node imports
from graph.nodes.intent_detector import intent_detector_node
from graph.nodes.greeter import greeter_node
from graph.nodes.menu_rag import menu_rag_node
from graph.nodes.cart_builder import cart_builder_node
from graph.nodes.delivery import set_delivery_address_node, set_order_type_node, delivery_question_node
from graph.nodes.order_confirm import order_confirm_node
from graph.nodes.place_order import place_order_node
from graph.nodes.order_tracker import order_tracker_node
from graph.nodes.guardrail import guardrail_node
from graph.nodes.clarify_intent import clarify_intent_node
from graph.nodes.multi_intent import multi_intent_processor_node


def route_from_intent(state: OrderState) -> str:
    """
    Routes from intent_detector directly to the designated response node.
    If multiple intents are detected, routes to multi_intent_processor.
    """
    intents = state.get("intents", [])

    # Multi-intent branch
    if len(intents) > 1:
        return "multi_intent_processor"

    # Single-intent branch
    intent = intents[0] if intents else state.get("intent", "menu_inquiry")
    cart = state.get("cart", [])
    order_id = state.get("order_id")

    if intent == "greet":
        return "greeter"

    if intent == "menu_inquiry":
        return "menu_rag"

    if intent in ("add_to_cart", "modify_cart"):
        return "cart_builder"

    if intent == "set_delivery_address":
        return "set_delivery_address"

    if intent == "set_order_type":
        return "set_order_type"

    if intent == "delivery_question":
        return "delivery_question"

    if intent == "checkout":
        return "order_confirm"

    if intent == "confirm_order":
        if cart:
            return "place_order"
        elif order_id or state.get("orders"):
            return "order_tracker"
        else:
            return "order_confirm"

    if intent == "track_order":
        return "order_tracker"

    if intent == "out_of_scope":
        return "guardrail"

    return "clarify_intent"


def build_graph():
    """Compiles the LangGraph StateGraph with single-turn terminal edges."""
    graph = StateGraph(OrderState)

    # 1. Register nodes
    graph.add_node("intent_detector", intent_detector_node)
    graph.add_node("greeter", greeter_node)
    graph.add_node("menu_rag", menu_rag_node)
    graph.add_node("cart_builder", cart_builder_node)
    graph.add_node("set_delivery_address", set_delivery_address_node)
    graph.add_node("set_order_type", set_order_type_node)
    graph.add_node("delivery_question", delivery_question_node)
    graph.add_node("order_confirm", order_confirm_node)
    graph.add_node("place_order", place_order_node)
    graph.add_node("order_tracker", order_tracker_node)
    graph.add_node("guardrail", guardrail_node)
    graph.add_node("clarify_intent", clarify_intent_node)
    graph.add_node("multi_intent_processor", multi_intent_processor_node)

    # 2. Entry point: classify intent
    graph.set_entry_point("intent_detector")

    # 3. Conditional routing to exactly one response node
    graph.add_conditional_edges(
        "intent_detector",
        route_from_intent,
        {
            "greeter": "greeter",
            "menu_rag": "menu_rag",
            "cart_builder": "cart_builder",
            "set_delivery_address": "set_delivery_address",
            "set_order_type": "set_order_type",
            "delivery_question": "delivery_question",
            "order_confirm": "order_confirm",
            "place_order": "place_order",
            "order_tracker": "order_tracker",
            "guardrail": "guardrail",
            "clarify_intent": "clarify_intent",
            "multi_intent_processor": "multi_intent_processor",
        }
    )

    # 4. Invariant: Every response node terminates the turn at END
    graph.add_edge("greeter", END)
    graph.add_edge("menu_rag", END)
    graph.add_edge("cart_builder", END)
    graph.add_edge("set_delivery_address", END)
    graph.add_edge("set_order_type", END)
    graph.add_edge("delivery_question", END)
    graph.add_edge("order_confirm", END)
    graph.add_edge("place_order", END)
    graph.add_edge("order_tracker", END)
    graph.add_edge("guardrail", END)
    graph.add_edge("clarify_intent", END)
    graph.add_edge("multi_intent_processor", END)

    return graph.compile()


# Singleton compiled graph
food_graph = build_graph()

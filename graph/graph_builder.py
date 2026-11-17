"""
graph_builder.py — wires all nodes into a LangGraph StateGraph with conditional routing.
"""

from langgraph.graph import StateGraph, END
from graph.state import OrderState

# Node imports
from graph.nodes.intent_detector    import intent_detector_node
from graph.nodes.greeter            import greeter_node
from graph.nodes.menu_rag           import menu_rag_node
from graph.nodes.availability_check import availability_check_node
from graph.nodes.suggest_alternatives import suggest_alternatives_node
from graph.nodes.cart_builder       import cart_builder_node
from graph.nodes.order_confirm      import order_confirm_node
from graph.nodes.place_order        import place_order_node
from graph.nodes.order_tracker      import order_tracker_node
from graph.nodes.guardrail          import guardrail_node
from graph.nodes.clarify_intent     import clarify_intent_node

MAX_FALLBACKS = 3


# ── Conditional Routers ──────────────────────────────────────────────────────

def route_after_intent(state: OrderState) -> str:
    """Route based on detected intent, with hard-stop guard and confirmation routing."""
    if state.get("fallback_count", 0) > MAX_FALLBACKS:
        return "clarify_intent"  # hard stop resets the count

    from langchain_core.messages import HumanMessage
    last_human = next(
        (m.content.lower() for m in reversed(state["messages"]) if isinstance(m, HumanMessage)),
        ""
    )
    cart = state.get("cart", [])

    # If cart already has items and user expresses confirm/checkout/cancel/edit
    confirm_keywords = ["confirm", "place order", "checkout", "yes", "proceed", "cancel", "edit"]
    if cart and any(w in last_human for w in confirm_keywords):
        return "order_confirm"

    routing = {
        "greet":        "greeter",
        "order":        "menu_rag",
        "track":        "order_tracker",
        "out_of_scope": "guardrail",
        "unclear":      "clarify_intent",
    }
    return routing.get(state.get("intent", "unclear"), "clarify_intent")


def route_after_availability(state: OrderState) -> str:
    """If unavailable items exist and no available items, suggest alternatives."""
    if state.get("unavailable_items") and not state.get("retrieved_menu"):
        return "suggest_alternatives"
    if state.get("retrieved_menu"):
        return "cart_builder"
    return "suggest_alternatives"


def route_after_cart(state: OrderState) -> str:
    """After building cart, go to confirmation."""
    return "order_confirm"


def route_after_confirm(state: OrderState) -> str:
    """Route based on user's confirmation decision."""
    if state.get("order_confirmed"):
        return "place_order"
    if state.get("edit_requested"):
        return "menu_rag"          # backtrack to menu
    if not state.get("cart"):
        return END                 # cancelled — cart cleared
    return END                     # default end


def route_after_suggest(state: OrderState) -> str:
    """After suggesting alternatives, go back to cart builder if results exist."""
    if state.get("retrieved_menu"):
        return "cart_builder"
    return END   # no alternatives found — graceful exit


# ── Graph Assembly ───────────────────────────────────────────────────────────

def build_graph():
    graph = StateGraph(OrderState)

    # Register nodes
    graph.add_node("intent_detector",    intent_detector_node)
    graph.add_node("greeter",            greeter_node)
    graph.add_node("menu_rag",           menu_rag_node)
    graph.add_node("availability_check", availability_check_node)
    graph.add_node("suggest_alternatives", suggest_alternatives_node)
    graph.add_node("cart_builder",       cart_builder_node)
    graph.add_node("order_confirm",      order_confirm_node)
    graph.add_node("place_order",        place_order_node)
    graph.add_node("order_tracker",      order_tracker_node)
    graph.add_node("guardrail",          guardrail_node)
    graph.add_node("clarify_intent",     clarify_intent_node)

    # Entry point
    graph.set_entry_point("intent_detector")

    # Conditional: after intent detection
    graph.add_conditional_edges(
        "intent_detector",
        route_after_intent,
        {
            "greeter":        "greeter",
            "menu_rag":       "menu_rag",
            "order_tracker":  "order_tracker",
            "guardrail":      "guardrail",
            "clarify_intent": "clarify_intent",
            "order_confirm":  "order_confirm",
        }
    )

    # Greeter welcomes the user and yields turn to user
    graph.add_edge("greeter", END)

    # After menu RAG → check availability
    graph.add_edge("menu_rag", "availability_check")

    # Conditional: after availability check
    graph.add_conditional_edges(
        "availability_check",
        route_after_availability,
        {
            "cart_builder":         "cart_builder",
            "suggest_alternatives": "suggest_alternatives",
        }
    )

    # After suggest alternatives, yield turn so user can choose
    graph.add_edge("suggest_alternatives", END)

    # After cart is updated, show summary to user and wait for their action (confirm / add more)
    graph.add_edge("cart_builder", END)

    # Conditional: after order confirmation
    graph.add_conditional_edges(
        "order_confirm",
        route_after_confirm,
        {
            "place_order": "place_order",
            "menu_rag":    "menu_rag",
            END:           END,
        }
    )

    # Terminal nodes → END
    graph.add_edge("place_order",    "order_tracker")
    graph.add_edge("order_tracker",  END)
    graph.add_edge("guardrail",      END)
    graph.add_edge("clarify_intent", END)

    return graph.compile()


# Singleton compiled graph
food_graph = build_graph()

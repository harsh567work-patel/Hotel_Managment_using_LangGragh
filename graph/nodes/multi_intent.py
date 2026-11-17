"""
multi_intent.py — Processes compound user turns with multiple intents sequentially
within a single node, returning EXACTLY ONE combined assistant message.
"""

from typing import List, Dict, Any
from langchain_core.messages import AIMessage
from graph.state import OrderState

# Node function handlers
from graph.nodes.cart_builder import cart_builder_node
from graph.nodes.menu_rag import menu_rag_node
from graph.nodes.delivery import set_delivery_address_node, delivery_question_node
from graph.nodes.order_confirm import order_confirm_node
from graph.nodes.place_order import place_order_node
from graph.nodes.order_tracker import order_tracker_node
from graph.nodes.greeter import greeter_node
from graph.nodes.guardrail import guardrail_node
from graph.nodes.clarify_intent import clarify_intent_node


INTENT_HANDLER_MAP = {
    "add_to_cart": cart_builder_node,
    "modify_cart": cart_builder_node,
    "menu_inquiry": menu_rag_node,
    "set_delivery_address": set_delivery_address_node,
    "delivery_question": delivery_question_node,
    "checkout": order_confirm_node,
    "confirm_order": place_order_node,
    "track_order": order_tracker_node,
    "greet": greeter_node,
    "out_of_scope": guardrail_node,
    "clarify_intent": clarify_intent_node,
}


def multi_intent_processor_node(state: OrderState) -> dict:
    """
    Executes multiple detected intents in sequence within one turn,
    accumulates state mutations, and merges replies into ONE combined AIMessage.
    """
    intents = state.get("intents", [])
    if not intents:
        intents = [state.get("intent", "clarify_intent")]

    # Working copy of state
    current_state = dict(state)
    combined_message_parts = []
    final_updates: Dict[str, Any] = {}

    for idx, intent_name in enumerate(intents):
        handler = INTENT_HANDLER_MAP.get(intent_name, clarify_intent_node)
        step_result = handler(current_state)

        # Merge state fields from this step
        for k, v in step_result.items():
            if k != "messages":
                current_state[k] = v
                final_updates[k] = v

        # Extract message content from this step
        step_msgs = step_result.get("messages", [])
        if step_msgs:
            msg_text = step_msgs[-1].content.strip()
            # If add_to_cart followed by checkout, keep the add confirmation without redundant cart block
            if intent_name in ("add_to_cart", "modify_cart") and "checkout" in intents[idx + 1:]:
                # Extract only the action lines (starts with ✅ or ⚠️)
                action_lines = [line for line in msg_text.splitlines() if line.startswith(("✅", "⚠️", "Emptied", "Added", "Removed", "Updated"))]
                if action_lines:
                    combined_message_parts.append("\n".join(action_lines))
                else:
                    combined_message_parts.append(msg_text)
            else:
                combined_message_parts.append(msg_text)

    # Combine all parts with clean spacing
    full_reply = "\n\n".join(combined_message_parts)
    final_updates["messages"] = [AIMessage(content=full_reply)]

    return final_updates

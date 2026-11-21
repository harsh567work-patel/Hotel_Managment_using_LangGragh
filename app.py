"""
app.py — Streamlit chatbot frontend for the SpiceRoute Kitchen food ordering RAG system.
Maintains OrderState across conversation turns, renders all new assistant messages
generated in each turn as a safety net, and synchronizes live cart / order receipt states.
"""

import re
import time
import streamlit as st
from langchain_core.messages import HumanMessage, AIMessage
from graph.graph_builder import food_graph
from graph.state import OrderState
from graph.orders import sync_orders, get_remaining_seconds, format_remaining
from config import MAX_ACTIVE_ORDERS
from rag.qdrant_client import get_embedder, get_all_menu_dishes

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="SpiceRoute Kitchen 🌶️",
    page_icon="🍽️",
    layout="wide",
)


@st.cache_resource(show_spinner=False)
def _warmup_rag_resources() -> bool:
    """Pre-loads FastEmbed ONNX model and caches Qdrant menu once per server startup."""
    try:
        get_embedder()
        get_all_menu_dishes()
    except Exception:
        pass
    return True


_warmup_rag_resources()

STREAMING_LLM_NODES = {"menu_rag", "greeter"}

INTENT_STATUS_LABELS = {
    "greet": "👋 Preparing a warm welcome...",
    "menu_inquiry": "📚 Searching SpiceRoute Kitchen's menu...",
    "add_to_cart": "🛒 Adding items to your cart...",
    "modify_cart": "🛒 Updating your cart...",
    "set_delivery_address": "📍 Checking your delivery address...",
    "set_order_type": "🍽️ Updating your dining / pickup mode...",
    "delivery_question": "🚚 Checking delivery & dining details...",
    "checkout": "📋 Preparing your order summary...",
    "confirm_order": "🎉 Finalizing your order...",
    "track_order": "📦 Checking live kitchen status...",
    "out_of_scope": "🍽️ Checking how I can help...",
}


def _stream_in_chunks(placeholder, text: str, words_per_chunk: int = 3, delay: float = 0.018) -> None:
    """Renders text progressively in small conversational chunks while preserving markdown & newlines."""
    tokens = re.findall(r"\S+\s*|\n+", text)
    rendered = ""
    for i in range(0, len(tokens), words_per_chunk):
        rendered += "".join(tokens[i:i + words_per_chunk])
        placeholder.markdown(rendered + "▌")
        time.sleep(delay)
    placeholder.markdown(text)

# ── Inject custom CSS ─────────────────────────────────────────────────────────
st.markdown("""
<style>
    .main { background-color: #1a1a1a; }
    .stChatMessage { border-radius: 12px; padding: 8px; }
    .cart-box {
        background: #2d2d2d;
        border-radius: 10px;
        padding: 16px;
        border-left: 4px solid #ff6b35;
    }
    .cart-item { margin: 6px 0; font-size: 14px; }
    .total-line {
        border-top: 1px solid #555;
        margin-top: 10px;
        padding-top: 8px;
        font-weight: bold;
        color: #ff6b35;
    }
    .order-cooking-box {
        background: #2b2418;
        border-radius: 10px;
        padding: 12px 14px;
        border-left: 4px solid #f39c12;
        margin-bottom: 8px;
        font-size: 13px;
    }
    .order-served-box {
        background: #1a3320;
        border-radius: 10px;
        padding: 12px 14px;
        border-left: 4px solid #2ecc71;
        margin-bottom: 8px;
        font-size: 13px;
    }
    .address-box {
        background: #252525;
        border-radius: 8px;
        padding: 10px;
        margin-top: 10px;
        font-size: 13px;
        border-left: 3px solid #3498db;
    }
    h1 { color: #ff6b35 !important; }
</style>
""", unsafe_allow_html=True)


# ── Session state initialisation ─────────────────────────────────────────────
def init_state() -> None:
    if "order_state" not in st.session_state:
        st.session_state.order_state: OrderState = {
            "messages":                   [],
            "intent":                     "",
            "intents":                    [],
            "user_query":                 "",
            "retrieved_menu":             [],
            "unavailable_items":          [],
            "cart":                       [],
            "total_price":                0.0,
            "max_prep_time":              0,
            "order_type":                 None,
            "table_number":               None,
            "delivery_address":           None,
            "pending_action":             None,
            "order_confirmed":            False,
            "edit_requested":             False,
            "order_id":                   None,
            "estimated_delivery_minutes": 0,
            "orders":                     [],
            "fallback_count":             0,
            "backtrack_count":            0,
            "error":                      None,
        }
    elif "orders" not in st.session_state.order_state:
        st.session_state.order_state["orders"] = []

    if "display_messages" not in st.session_state:
        st.session_state.display_messages = []


init_state()


def _format_order_mode_html(order: dict) -> str:
    mode = order.get("order_type") or "delivery"
    if mode == "dine_in":
        table_num = order.get("table_number")
        table_txt = f" (Table {table_num})" if table_num else ""
        return f"🍽️ Dine-in{table_txt}"
    if mode == "pickup":
        return "🛍️ Self-Pickup"
    addr = order.get("delivery_address")
    if addr and addr.get("street"):
        return f"📍 {addr.get('street')}, {addr.get('city', 'Bangalore')}"
    return "🚚 Delivery"


@st.fragment(run_every=1)
def _render_live_orders_panel() -> None:
    """Auto-refreshing sidebar fragment tracking up to 5 concurrent cooking orders + served history."""
    time_scale = 60.0 if st.session_state.get("demo_speed_timer", False) else 1.0
    orders = sync_orders(st.session_state.order_state.get("orders", []), time_scale=time_scale)
    st.session_state.order_state["orders"] = orders

    if not orders:
        return

    cooking = [o for o in orders if o.get("status") == "cooking"]
    served = [o for o in orders if o.get("status") == "served"]

    st.markdown("---")
    st.markdown(f"### 🔥 Kitchen Orders ({len(cooking)} / {MAX_ACTIVE_ORDERS} Active)")

    # Cooking orders (newest first)
    for o in reversed(cooking):
        rem_secs = get_remaining_seconds(o, time_scale=time_scale)
        total_secs = max(1, int(o.get("eta_minutes", 20)) * 60)
        progress = min(1.0, max(0.0, 1.0 - (rem_secs / total_secs)))
        items_html = ", ".join(f"{i['dish_name']} x{i['quantity']}" for i in o.get("items", []))
        mode_html = _format_order_mode_html(o)
        st.markdown(
            f"""
            <div class="order-cooking-box">
                <b style="color:#f39c12;">🍳 Cooking</b> — <code>{o['order_id']}</code><br>
                ⏱️ <b>{format_remaining(rem_secs)}</b> remaining &nbsp;|&nbsp; ₹{o.get('total_price', 0):.0f}<br>
                <span style="color:#ccc;">{items_html}</span><br>
                <span style="color:#aaa;">{mode_html}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.progress(progress)

    # Served orders (newest first)
    for o in reversed(served):
        items_html = ", ".join(f"{i['dish_name']} x{i['quantity']}" for i in o.get("items", []))
        mode_html = _format_order_mode_html(o)
        st.markdown(
            f"""
            <div class="order-served-box">
                <b style="color:#2ecc71;">✅ Served ({o['order_id']})</b><br>
                ₹{o.get('total_price', 0):.0f} &nbsp;|&nbsp; {mode_html}<br>
                <span style="color:#ccc;">{items_html}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

    at_limit = len(cooking) >= MAX_ACTIVE_ORDERS
    if at_limit:
        st.warning(f"Kitchen limit reached ({MAX_ACTIVE_ORDERS}/{MAX_ACTIVE_ORDERS} cooking). Wait for an order to be served.")

    if st.button(
        "➕ Start Another Order",
        use_container_width=True,
        disabled=at_limit,
        key="start_another_order_btn",
    ):
        # Preserve ALL cooking & served orders, reset cart, and refresh main chat!
        preserved_orders = list(st.session_state.order_state.get("orders", []))
        st.session_state.order_state["messages"] = []
        st.session_state.order_state["intent"] = ""
        st.session_state.order_state["intents"] = []
        st.session_state.order_state["user_query"] = ""
        st.session_state.order_state["retrieved_menu"] = []
        st.session_state.order_state["unavailable_items"] = []
        st.session_state.order_state["cart"] = []
        st.session_state.order_state["total_price"] = 0.0
        st.session_state.order_state["max_prep_time"] = 0
        st.session_state.order_state["order_type"] = None
        st.session_state.order_state["table_number"] = None
        st.session_state.order_state["pending_action"] = None
        st.session_state.order_state["order_confirmed"] = False
        st.session_state.order_state["edit_requested"] = False
        st.session_state.order_state["order_id"] = None
        st.session_state.order_state["estimated_delivery_minutes"] = 0
        st.session_state.order_state["orders"] = preserved_orders
        st.session_state.display_messages = []
        st.rerun(scope="app")


# ── Sidebar — Live Cart & Order Status ───────────────────────────────────────
with st.sidebar:
    st.markdown("## 🛒 Current Cart")
    cart = st.session_state.order_state.get("cart", [])
    address = st.session_state.order_state.get("delivery_address")
    order_type = st.session_state.order_state.get("order_type")
    table_num = st.session_state.order_state.get("table_number")

    if cart:
        st.markdown('<div class="cart-box">', unsafe_allow_html=True)
        for item in cart:
            st.markdown(
                f'<div class="cart-item">🍽️ {item["dish_name"]} x{item["quantity"]} '
                f'— ₹{item["unit_price"] * item["quantity"]:.0f}</div>',
                unsafe_allow_html=True,
            )
        total = st.session_state.order_state.get("total_price", 0)
        prep  = st.session_state.order_state.get("max_prep_time", 0)
        st.markdown(
            f'<div class="total-line">💰 Total: ₹{total:.0f} &nbsp;&nbsp; ⏱️ Prep: ~{prep} min</div>',
            unsafe_allow_html=True,
        )
        st.markdown('</div>', unsafe_allow_html=True)

        if order_type == "dine_in":
            table_txt = f" (Table {table_num})" if table_num else ""
            st.markdown(
                f"""
                <div class="address-box">
                    🍽️ <b>Order Type:</b><br>
                    Dine-in{table_txt} at SpiceRoute Kitchen
                </div>
                """,
                unsafe_allow_html=True
            )
        elif order_type == "pickup":
            st.markdown(
                """
                <div class="address-box">
                    🛍️ <b>Order Type:</b><br>
                    Self-Pickup / Takeaway at SpiceRoute Kitchen
                </div>
                """,
                unsafe_allow_html=True
            )
        elif address and address.get("street"):
            st.markdown(
                f"""
                <div class="address-box">
                    📍 <b>Delivery Address:</b><br>
                    {address.get('street')}, {address.get('city')}
                </div>
                """,
                unsafe_allow_html=True
            )

        if st.button("🗑️ Clear Cart", use_container_width=True):
            st.session_state.order_state["cart"] = []
            st.session_state.order_state["total_price"] = 0.0
            st.session_state.order_state["max_prep_time"] = 0
            st.session_state.order_state["pending_action"] = None
            st.rerun()
    else:
        st.markdown("_Your cart is empty._\n\nStart by telling the bot what you'd like to eat!")

    # Live ticking kitchen orders panel (auto-refreshes every 1s)
    _render_live_orders_panel()

    st.markdown("---")
    st.toggle(
        "⚡ Fast Demo Timer (1 min = 1 sec)",
        key="demo_speed_timer",
        help="Speeds up kitchen countdowns so you can see orders hit 00:00 → Served live.",
    )

    st.markdown("---")
    st.markdown("**🏠 SpiceRoute Kitchen**")
    st.markdown("📍 12, Food Street, Bangalore")
    st.markdown("📞 +91-9876543210")

    st.markdown("---")
    if st.button("🔄 Reset Conversation", use_container_width=True):
        st.session_state.clear()
        st.rerun()


# ── Main Chat Area ───────────────────────────────────────────────────────────
col1, col2 = st.columns([1, 8])
with col2:
    st.markdown("# 🍽️ SpiceRoute Kitchen")
    st.caption("_Authentic Flavors, Delivered Fast_ | AI-powered food ordering with LangGraph & Groq")

st.markdown("---")

# Display conversation history
for msg in st.session_state.display_messages:
    role = "user" if msg["role"] == "user" else "assistant"
    with st.chat_message(role):
        st.markdown(msg["content"])

# Welcome message on first load or after starting another order
if not st.session_state.display_messages:
    with st.chat_message("assistant"):
        time_scale = 60.0 if st.session_state.get("demo_speed_timer", False) else 1.0
        existing_orders = sync_orders(st.session_state.order_state.get("orders", []), time_scale=time_scale)
        st.session_state.order_state["orders"] = existing_orders
        cooking_count = sum(1 for o in existing_orders if o.get("status") == "cooking")

        if cooking_count > 0:
            welcome = (
                f"🍽️ **Ready for your next order!**\n\n"
                f"🔥 Your previous **{cooking_count}** order(s) ({cooking_count}/{MAX_ACTIVE_ORDERS} active) "
                "are still safely cooking in our kitchen — you can watch their live countdowns in the sidebar "
                "or ask *'Where is my order?'* anytime.\n\n"
                "What would you like to add to your new order?"
            )
        else:
            welcome = (
                "👋 Welcome to **SpiceRoute Kitchen**!\n\n"
                "I'm your AI food ordering concierge. I can help you:\n"
                "  • 🍛 **Browse our menu:** Ask *'What biryani options do you have?'* or *'What are your cheapest starters?'*\n"
                "  • 🛒 **Order & customize:** Say *'I want 2 butter chicken and a mango lassi'*\n"
                "  • 🍽️ **Dine-in, Pickup, or Delivery:** Say *'Deliver to 12 MG Road, Bangalore'*, *'I'll dine here'*, or *'Takeaway'*\n"
                "  • 📦 **Track active orders:** Ask *'Where is my order?'*\n\n"
                "What would you like to eat today?"
            )
        st.markdown(welcome)
        st.session_state.display_messages.append({"role": "assistant", "content": welcome})


# ── Chat Input & Streaming Graph Invocation ──────────────────────────────────
if prompt := st.chat_input("Type your message here... (e.g., 'I want 2 butter chicken')"):
    # Show user message immediately
    with st.chat_message("user"):
        st.markdown(prompt)
    st.session_state.display_messages.append({"role": "user", "content": prompt})

    # Sync active/served order timers before invoking graph
    time_scale = 60.0 if st.session_state.get("demo_speed_timer", False) else 1.0
    current_state = st.session_state.order_state
    current_state["orders"] = sync_orders(current_state.get("orders", []), time_scale=time_scale)
    prev_msg_count = len(current_state.get("messages", []))
    current_state["messages"] = current_state.get("messages", []) + [HumanMessage(content=prompt)]

    # Stream response immediately in the assistant bubble with live status & chunked output
    with st.chat_message("assistant"):
        status_placeholder = st.empty()
        msg_placeholder = st.empty()
        status_placeholder.caption("🔍 Understanding your request...")

        streamed_text = ""
        final_state = None
        did_stream_tokens = False

        try:
            for mode, payload in food_graph.stream(
                current_state,
                stream_mode=["messages", "updates", "values"],
            ):
                if mode == "messages":
                    chunk, meta = payload
                    node = meta.get("langgraph_node")
                    token = getattr(chunk, "content", "")
                    if node in STREAMING_LLM_NODES and token:
                        if not did_stream_tokens:
                            status_placeholder.empty()
                            did_stream_tokens = True
                        streamed_text += token
                        msg_placeholder.markdown(streamed_text + "▌")

                elif mode == "updates":
                    if "intent_detector" in payload:
                        detected = payload["intent_detector"].get("intents") or [
                            payload["intent_detector"].get("intent", "")
                        ]
                        if len(detected) > 1:
                            status_placeholder.caption("⚡ Handling your requests step-by-step...")
                        else:
                            label = INTENT_STATUS_LABELS.get(detected[0], "🍳 Preparing your response...")
                            status_placeholder.caption(label)

                elif mode == "values":
                    final_state = payload

            status_placeholder.empty()

            if final_state is not None:
                st.session_state.order_state = final_state
                new_turn_messages = final_state.get("messages", [])[prev_msg_count + 1:]
                new_ai_messages = [m for m in new_turn_messages if isinstance(m, AIMessage)]

                if not new_ai_messages:
                    all_ai = [m for m in final_state.get("messages", []) if isinstance(m, AIMessage)]
                    if all_ai:
                        new_ai_messages = [all_ai[-1]]

                final_reply = "\n\n".join(m.content for m in new_ai_messages) if new_ai_messages else streamed_text

                if did_stream_tokens:
                    # Finalize clean markdown without cursor
                    msg_placeholder.markdown(final_reply)
                elif final_reply:
                    # Smoothly render structured/deterministic replies in small conversational chunks
                    _stream_in_chunks(msg_placeholder, final_reply)

                if final_reply:
                    st.session_state.display_messages.append({"role": "assistant", "content": final_reply})

        except Exception as e:
            status_placeholder.empty()
            error_msg = f"⚠️ Something went wrong: {str(e)}\n\nPlease try rephrasing your request."
            msg_placeholder.markdown(error_msg)
            st.session_state.display_messages.append({"role": "assistant", "content": error_msg})

    st.rerun()

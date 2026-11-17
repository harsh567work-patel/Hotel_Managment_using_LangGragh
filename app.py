"""
app.py — Streamlit chatbot frontend for the SpiceRoute Kitchen food ordering RAG system.
Maintains OrderState in st.session_state across conversation turns.
"""

import streamlit as st
from langchain_core.messages import HumanMessage, AIMessage
from graph.graph_builder import food_graph
from graph.state import OrderState

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="SpiceRoute Kitchen 🌶️",
    page_icon="🍽️",
    layout="wide",
)

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
    .cart-item { margin: 4px 0; font-size: 14px; }
    .total-line {
        border-top: 1px solid #555;
        margin-top: 8px;
        padding-top: 8px;
        font-weight: bold;
        color: #ff6b35;
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
            "user_query":                 "",
            "retrieved_menu":             [],
            "unavailable_items":          [],
            "cart":                       [],
            "total_price":                0.0,
            "max_prep_time":              0,
            "order_confirmed":            False,
            "edit_requested":             False,
            "order_id":                   None,
            "estimated_delivery_minutes": 0,
            "fallback_count":             0,
            "backtrack_count":            0,
            "error":                      None,
        }
    if "display_messages" not in st.session_state:
        st.session_state.display_messages = []


init_state()


# ── Sidebar — Live Cart ──────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🛒 Your Cart")
    cart = st.session_state.order_state.get("cart", [])
    order_id = st.session_state.order_state.get("order_id")

    if order_id:
        st.success(f"✅ Order Placed!\n\n**ID:** `{order_id}`")
        eta = st.session_state.order_state.get("estimated_delivery_minutes", 0)
        st.info(f"⏱️ Estimated delivery: **{eta} min**")
    elif cart:
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
            f'<div class="total-line">💰 Total: ₹{total:.0f} &nbsp;&nbsp; ⏱️ ~{prep} min</div>',
            unsafe_allow_html=True,
        )
        st.markdown('</div>', unsafe_allow_html=True)

        if st.button("🗑️ Clear Cart", use_container_width=True):
            st.session_state.order_state["cart"] = []
            st.session_state.order_state["total_price"] = 0.0
            st.session_state.order_state["max_prep_time"] = 0
            st.rerun()
    else:
        st.markdown("_Your cart is empty._\n\nStart by telling the bot what you'd like to eat!")

    st.markdown("---")
    st.markdown("**🏠 SpiceRoute Kitchen**")
    st.markdown("📍 12, Food Street, Bangalore")
    st.markdown("📞 +91-9876543210")

    st.markdown("---")
    if st.button("🔄 New Conversation", use_container_width=True):
        st.session_state.clear()
        st.rerun()


# ── Main Chat Area ───────────────────────────────────────────────────────────
col1, col2 = st.columns([1, 8])
with col2:
    st.markdown("# 🍽️ SpiceRoute Kitchen")
    st.caption("_Authentic Flavors, Delivered Fast_ | AI-powered food ordering")

st.markdown("---")

# Display conversation history
for msg in st.session_state.display_messages:
    role = "user" if msg["role"] == "user" else "assistant"
    with st.chat_message(role):
        st.markdown(msg["content"])

# Welcome message on first load
if not st.session_state.display_messages:
    with st.chat_message("assistant"):
        welcome = (
            "👋 Welcome to **SpiceRoute Kitchen**!\n\n"
            "I'm your AI food ordering assistant. I can help you:\n"
            "  🍛 Browse our menu\n"
            "  🛒 Place an order\n"
            "  📦 Track your order\n\n"
            "What would you like to eat today?"
        )
        st.markdown(welcome)
        st.session_state.display_messages.append({"role": "assistant", "content": welcome})


# ── Chat Input ───────────────────────────────────────────────────────────────
if prompt := st.chat_input("Type your message here... (e.g., 'I want butter chicken')"):
    # Show user message immediately
    with st.chat_message("user"):
        st.markdown(prompt)
    st.session_state.display_messages.append({"role": "user", "content": prompt})

    # Add to LangGraph state
    current_state = st.session_state.order_state
    current_state["messages"] = current_state["messages"] + [HumanMessage(content=prompt)]

    # Run graph
    with st.spinner("Thinking... 🤔"):
        try:
            result = food_graph.invoke(current_state)
            st.session_state.order_state = result

            # Extract last AI response
            ai_messages = [m for m in result["messages"] if isinstance(m, AIMessage)]
            if ai_messages:
                reply = ai_messages[-1].content
                with st.chat_message("assistant"):
                    st.markdown(reply)
                st.session_state.display_messages.append({"role": "assistant", "content": reply})

        except Exception as e:
            error_msg = f"⚠️ Something went wrong: {str(e)}\n\nPlease try rephrasing your request."
            with st.chat_message("assistant"):
                st.markdown(error_msg)
            st.session_state.display_messages.append({"role": "assistant", "content": error_msg})

    st.rerun()

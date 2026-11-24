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
    /* ═══════════════════════════════════════════════════════════════════════
       SpiceRoute Kitchen — "Ember Luxe" Design System v3.0
       Dramatic visual overhaul with Streamlit-native selectors
       ═══════════════════════════════════════════════════════════════════════ */

    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600&display=swap');
    @import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200');

    /* ════════════════════════════════════════════════════════════════════
       1. GLOBAL FOUNDATIONS
       ════════════════════════════════════════════════════════════════════ */
    .stApp, .main, [data-testid="stAppViewContainer"],
    [data-testid="stAppViewBlockContainer"], .block-container {
        background-color: #080808 !important;
    }

    html, body, .stApp {
        font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif !important;
        -webkit-font-smoothing: antialiased !important;
        -moz-osx-font-smoothing: grayscale !important;
    }

    /* Target text containers specifically without overriding icon ligature fonts */
    .stApp p, .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6,
    .stApp li, .stApp input, .stApp textarea, .stApp button, .stApp label,
    .stMarkdown, .stCaption {
        font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif !important;
    }

    /* Preserve and prioritize Material Symbols icon ligatures */
    [data-testid="stIconMaterial"],
    [data-testid="stIconMaterial"] *,
    .material-symbols-rounded,
    .material-symbols-outlined,
    .material-icons {
        font-family: 'Material Symbols Rounded', 'Material Symbols Outlined', 'Material Icons' !important;
        font-weight: normal !important;
        font-style: normal !important;
        font-size: 1.25rem !important;
        line-height: 1 !important;
        letter-spacing: normal !important;
        text-transform: none !important;
        display: inline-block !important;
        white-space: nowrap !important;
        word-wrap: normal !important;
        direction: ltr !important;
        -webkit-font-smoothing: antialiased !important;
        text-rendering: optimizeLegibility !important;
        -moz-osx-font-smoothing: grayscale !important;
        font-feature-settings: 'liga' 1 !important;
    }

    /* ════════════════════════════════════════════════════════════════════
       2. HEADER / TITLE AREA
       ════════════════════════════════════════════════════════════════════ */
    h1 {
        background: linear-gradient(135deg, #FF6B35 0%, #F7931E 50%, #FFD700 100%) !important;
        -webkit-background-clip: text !important;
        -webkit-text-fill-color: transparent !important;
        background-clip: text !important;
        font-weight: 900 !important;
        font-size: 2.4rem !important;
        letter-spacing: -0.04em !important;
        line-height: 1.1 !important;
        padding-bottom: 4px !important;
        filter: drop-shadow(0 0 20px rgba(255, 107, 53, 0.3)) !important;
    }

    h2 {
        color: #FFFFFF !important;
        font-weight: 700 !important;
        font-size: 1.15rem !important;
        letter-spacing: -0.02em !important;
    }

    h3 {
        color: #F7931E !important;
        font-weight: 700 !important;
        font-size: 1rem !important;
        letter-spacing: -0.01em !important;
    }

    /* Caption under title */
    [data-testid="stCaptionContainer"], .stCaption {
        color: #8E8E93 !important;
        font-size: 13px !important;
        font-weight: 400 !important;
        letter-spacing: 0.02em !important;
    }

    /* ════════════════════════════════════════════════════════════════════
       3. SIDEBAR — Premium dark panel
       ════════════════════════════════════════════════════════════════════ */
    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #111113 0%, #0C0C0E 100%) !important;
        border-right: 1px solid rgba(255, 107, 53, 0.12) !important;
        box-shadow: 4px 0 24px rgba(0,0,0,0.5) !important;
    }

    section[data-testid="stSidebar"] > div:first-child {
        background: transparent !important;
        padding-top: 24px !important;
    }

    section[data-testid="stSidebar"] .stMarkdown {
        color: #C7C7CC !important;
    }

    section[data-testid="stSidebar"] hr {
        border: none !important;
        height: 1px !important;
        background: linear-gradient(90deg, transparent, rgba(255,107,53,0.2), transparent) !important;
        margin: 20px 0 !important;
    }

    /* ════════════════════════════════════════════════════════════════════
       4. CART BOX — Glassmorphism card
       ════════════════════════════════════════════════════════════════════ */
    .cart-box {
        background: linear-gradient(145deg, rgba(30,30,34,0.95) 0%, rgba(20,20,22,0.98) 100%) !important;
        border-radius: 16px !important;
        padding: 20px !important;
        border: 1px solid rgba(255, 107, 53, 0.2) !important;
        border-left: 4px solid #FF6B35 !important;
        box-shadow: 0 8px 32px rgba(0,0,0,0.4),
                    inset 0 1px 0 rgba(255,255,255,0.03) !important;
        backdrop-filter: blur(10px) !important;
    }

    .cart-item {
        margin: 0 !important;
        font-size: 14px !important;
        color: #E5E5EA !important;
        padding: 10px 0 !important;
        border-bottom: 1px solid rgba(255,255,255,0.06) !important;
        font-weight: 500 !important;
        letter-spacing: 0.01em !important;
    }

    .cart-item:last-of-type {
        border-bottom: none !important;
    }

    .total-line {
        border-top: 2px solid #FF6B35 !important;
        margin-top: 16px !important;
        padding-top: 14px !important;
        font-weight: 800 !important;
        color: #FF6B35 !important;
        font-size: 16px !important;
        letter-spacing: 0.02em !important;
        text-shadow: 0 0 20px rgba(255, 107, 53, 0.3) !important;
    }

    /* ════════════════════════════════════════════════════════════════════
       5. CHAT MESSAGES — Distinct bubbles with depth
       ════════════════════════════════════════════════════════════════════ */
    .stChatMessage, [data-testid="stChatMessage"] {
        border-radius: 18px !important;
        padding: 16px 20px !important;
        margin-bottom: 8px !important;
        transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1) !important;
        border: 1px solid rgba(255,255,255,0.04) !important;
    }

    /* Assistant bubble — rich dark card */
    [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"]) {
        background: linear-gradient(145deg, #151517 0%, #111113 100%) !important;
        border-color: rgba(255, 107, 53, 0.1) !important;
        box-shadow: 0 4px 20px rgba(0,0,0,0.3),
                    inset 0 1px 0 rgba(255,255,255,0.03) !important;
    }

    /* User bubble — elevated with accent hint */
    [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
        background: linear-gradient(145deg, #1C1C1E 0%, #18181A 100%) !important;
        border-color: rgba(255,255,255,0.08) !important;
        box-shadow: 0 2px 12px rgba(0,0,0,0.25) !important;
    }

    .stChatMessage:hover, [data-testid="stChatMessage"]:hover {
        border-color: rgba(255, 107, 53, 0.25) !important;
        box-shadow: 0 8px 32px rgba(0,0,0,0.4),
                    0 0 0 1px rgba(255, 107, 53, 0.1) !important;
        transform: translateY(-1px) !important;
    }

    /* Chat avatar */
    [data-testid="stChatMessageAvatarAssistant"] {
        background: linear-gradient(135deg, #FF6B35, #F7931E) !important;
        border-radius: 12px !important;
        box-shadow: 0 2px 10px rgba(255, 107, 53, 0.4) !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        overflow: hidden !important;
    }

    [data-testid="stChatMessageAvatarAssistant"] [data-testid="stIconMaterial"] {
        color: #FFFFFF !important;
        font-size: 1.35rem !important;
        line-height: 1 !important;
    }

    [data-testid="stChatMessageAvatarUser"] {
        background: linear-gradient(135deg, #2C2C2E, #3A3A3C) !important;
        border-radius: 12px !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        overflow: hidden !important;
    }

    [data-testid="stChatMessageAvatarUser"] [data-testid="stIconMaterial"] {
        color: #FFFFFF !important;
        font-size: 1.35rem !important;
        line-height: 1 !important;
    }

    /* Chat text */
    .stChatMessage p, .stChatMessage li,
    [data-testid="stChatMessage"] p, [data-testid="stChatMessage"] li {
        color: #E5E5EA !important;
        font-size: 15px !important;
        line-height: 1.7 !important;
        font-weight: 400 !important;
    }

    .stChatMessage strong, [data-testid="stChatMessage"] strong {
        color: #FFFFFF !important;
        font-weight: 700 !important;
    }

    .stChatMessage em, [data-testid="stChatMessage"] em {
        color: #C7C7CC !important;
    }

    /* ════════════════════════════════════════════════════════════════════
       6. CHAT INPUT — Premium floating bar
       ════════════════════════════════════════════════════════════════════ */
    [data-testid="stChatInput"], .stChatInput {
        border-radius: 16px !important;
    }

    [data-testid="stChatInput"] > div, .stChatInput > div {
        background: #141416 !important;
        border: 2px solid rgba(255,255,255,0.08) !important;
        border-radius: 16px !important;
        box-shadow: 0 4px 24px rgba(0,0,0,0.4),
                    inset 0 1px 0 rgba(255,255,255,0.03) !important;
        transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1) !important;
        padding: 4px 8px !important;
    }

    [data-testid="stChatInput"] > div:focus-within,
    .stChatInput > div:focus-within {
        border-color: #FF6B35 !important;
        box-shadow: 0 4px 24px rgba(0,0,0,0.4),
                    0 0 0 4px rgba(255, 107, 53, 0.15),
                    0 0 30px rgba(255, 107, 53, 0.08) !important;
    }

    [data-testid="stChatInput"] textarea, .stChatInput textarea {
        color: #F5F5F7 !important;
        font-size: 15px !important;
        font-weight: 400 !important;
    }

    [data-testid="stChatInput"] textarea::placeholder,
    .stChatInput textarea::placeholder {
        color: #636366 !important;
        font-style: normal !important;
        font-weight: 400 !important;
    }

    /* Send button */
    [data-testid="stChatInput"] button,
    .stChatInput button {
        background: linear-gradient(135deg, #FF6B35, #E8572A) !important;
        border: none !important;
        border-radius: 10px !important;
        box-shadow: 0 2px 8px rgba(255, 107, 53, 0.3) !important;
        transition: all 0.2s ease !important;
    }

    [data-testid="stChatInput"] button:hover,
    .stChatInput button:hover {
        box-shadow: 0 4px 16px rgba(255, 107, 53, 0.5) !important;
        transform: scale(1.05) !important;
    }

    [data-testid="stChatInput"] button:disabled,
    .stChatInput button:disabled {
        background: #2C2C2E !important;
        box-shadow: none !important;
        opacity: 0.5 !important;
    }

    /* ════════════════════════════════════════════════════════════════════
       7. ORDER STATUS CARDS — Vivid status colours
       ════════════════════════════════════════════════════════════════════ */
    .order-cooking-box {
        background: linear-gradient(145deg, rgba(50, 38, 10, 0.9) 0%, rgba(20, 20, 22, 0.95) 100%) !important;
        border-radius: 14px !important;
        padding: 16px 18px !important;
        border: 1px solid rgba(247, 147, 30, 0.2) !important;
        border-left: 5px solid #F7931E !important;
        margin-bottom: 12px !important;
        font-size: 13.5px !important;
        box-shadow: 0 4px 20px rgba(0,0,0,0.3),
                    0 0 15px rgba(247, 147, 30, 0.05) !important;
        line-height: 1.65 !important;
        color: #D1D1D6 !important;
    }

    .order-cooking-box:hover {
        border-color: rgba(247, 147, 30, 0.35) !important;
        box-shadow: 0 8px 32px rgba(0,0,0,0.4),
                    0 0 25px rgba(247, 147, 30, 0.08) !important;
        transform: translateY(-1px) !important;
    }

    .order-served-box {
        background: linear-gradient(145deg, rgba(15, 50, 25, 0.9) 0%, rgba(20, 20, 22, 0.95) 100%) !important;
        border-radius: 14px !important;
        padding: 16px 18px !important;
        border: 1px solid rgba(52, 199, 89, 0.2) !important;
        border-left: 5px solid #34C759 !important;
        margin-bottom: 12px !important;
        font-size: 13.5px !important;
        box-shadow: 0 4px 20px rgba(0,0,0,0.3),
                    0 0 15px rgba(52, 199, 89, 0.05) !important;
        line-height: 1.65 !important;
        color: #D1D1D6 !important;
    }

    .order-served-box:hover {
        border-color: rgba(52, 199, 89, 0.35) !important;
        box-shadow: 0 8px 32px rgba(0,0,0,0.4),
                    0 0 25px rgba(52, 199, 89, 0.08) !important;
        transform: translateY(-1px) !important;
    }

    /* Status label text glow */
    .order-cooking-box b[style*="color:#f39c12"],
    .order-cooking-box b[style*="color: #f39c12"] {
        color: #F7931E !important;
        text-shadow: 0 0 12px rgba(247, 147, 30, 0.5) !important;
        font-weight: 700 !important;
    }

    .order-served-box b[style*="color:#2ecc71"],
    .order-served-box b[style*="color: #2ecc71"] {
        color: #34C759 !important;
        text-shadow: 0 0 12px rgba(52, 199, 89, 0.5) !important;
        font-weight: 700 !important;
    }

    /* ════════════════════════════════════════════════════════════════════
       8. ADDRESS BOX
       ════════════════════════════════════════════════════════════════════ */
    .address-box {
        background: linear-gradient(145deg, rgba(25, 35, 50, 0.8) 0%, rgba(20, 20, 22, 0.95) 100%) !important;
        border-radius: 12px !important;
        padding: 14px 16px !important;
        margin-top: 12px !important;
        font-size: 13.5px !important;
        border: 1px solid rgba(90, 200, 250, 0.15) !important;
        border-left: 4px solid #5AC8FA !important;
        color: #AEAEB2 !important;
        line-height: 1.6 !important;
        box-shadow: 0 4px 16px rgba(0,0,0,0.2) !important;
    }

    .address-box b {
        color: #FFFFFF !important;
        font-weight: 600 !important;
    }

    /* ════════════════════════════════════════════════════════════════════
       9. BUTTONS — Bold gradient with glow
       ════════════════════════════════════════════════════════════════════ */
    .stButton > button {
        background: linear-gradient(135deg, #FF6B35 0%, #E8572A 50%, #D44A1E 100%) !important;
        color: #FFFFFF !important;
        border: none !important;
        border-radius: 12px !important;
        font-weight: 700 !important;
        font-family: 'Inter', system-ui, sans-serif !important;
        letter-spacing: 0.01em !important;
        padding: 12px 24px !important;
        font-size: 14px !important;
        transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1) !important;
        box-shadow: 0 4px 14px rgba(255, 107, 53, 0.35),
                    inset 0 1px 0 rgba(255,255,255,0.15) !important;
        text-shadow: 0 1px 2px rgba(0,0,0,0.3) !important;
    }

    .stButton > button:hover {
        background: linear-gradient(135deg, #FF7F50 0%, #FF6B35 50%, #E8572A 100%) !important;
        box-shadow: 0 6px 24px rgba(255, 107, 53, 0.5),
                    inset 0 1px 0 rgba(255,255,255,0.2) !important;
        transform: translateY(-2px) !important;
    }

    .stButton > button:active {
        transform: translateY(0) !important;
        box-shadow: 0 2px 8px rgba(255, 107, 53, 0.25) !important;
    }

    .stButton > button:disabled {
        background: #2C2C2E !important;
        color: #636366 !important;
        box-shadow: none !important;
        text-shadow: none !important;
        cursor: not-allowed !important;
    }

    /* ════════════════════════════════════════════════════════════════════
       10. PROGRESS BARS — Animated gradient
       ════════════════════════════════════════════════════════════════════ */
    .stProgress > div > div {
        background: #1C1C1E !important;
        border-radius: 8px !important;
        overflow: hidden !important;
        height: 8px !important;
    }

    .stProgress > div > div > div {
        background: linear-gradient(90deg, #FF6B35, #F7931E, #FFD700) !important;
        border-radius: 8px !important;
        box-shadow: 0 0 16px rgba(255, 107, 53, 0.4) !important;
        transition: width 1s linear !important;
    }

    /* ════════════════════════════════════════════════════════════════════
       11. CODE / ORDER IDs
       ════════════════════════════════════════════════════════════════════ */
    code {
        font-family: 'JetBrains Mono', 'SF Mono', 'Fira Code', monospace !important;
        background: rgba(255, 107, 53, 0.1) !important;
        color: #F7931E !important;
        padding: 3px 8px !important;
        border-radius: 6px !important;
        font-size: 0.85em !important;
        font-weight: 500 !important;
        border: 1px solid rgba(255, 107, 53, 0.15) !important;
    }

    /* ════════════════════════════════════════════════════════════════════
       12. TOGGLE & WARNINGS
       ════════════════════════════════════════════════════════════════════ */
    .stToggle label span {
        color: #AEAEB2 !important;
        font-size: 13px !important;
        font-weight: 500 !important;
    }

    .stAlert {
        background: rgba(255, 214, 10, 0.06) !important;
        border: 1px solid rgba(255, 214, 10, 0.18) !important;
        border-radius: 12px !important;
        color: #FFD60A !important;
        font-weight: 500 !important;
    }

    /* ════════════════════════════════════════════════════════════════════
       13. GENERAL MARKDOWN TEXT
       ════════════════════════════════════════════════════════════════════ */
    .stMarkdown {
        color: #E5E5EA !important;
        line-height: 1.65 !important;
    }

    .stMarkdown a {
        color: #5AC8FA !important;
        text-decoration: none !important;
        font-weight: 500 !important;
        transition: color 0.2s ease !important;
    }

    .stMarkdown a:hover {
        color: #70D5FF !important;
    }

    /* ════════════════════════════════════════════════════════════════════
       14. HORIZONTAL DIVIDERS — Gradient fade
       ════════════════════════════════════════════════════════════════════ */
    hr, [data-testid="stAppViewContainer"] hr {
        border: none !important;
        height: 1px !important;
        background: linear-gradient(90deg, transparent 0%, rgba(255,107,53,0.25) 50%, transparent 100%) !important;
        margin: 24px 0 !important;
    }

    /* ════════════════════════════════════════════════════════════════════
       15. SCROLLBARS
       ════════════════════════════════════════════════════════════════════ */
    ::-webkit-scrollbar {
        width: 5px;
        height: 5px;
    }

    ::-webkit-scrollbar-track {
        background: #0A0A0A;
    }

    ::-webkit-scrollbar-thumb {
        background: #3A3A3C;
        border-radius: 10px;
    }

    ::-webkit-scrollbar-thumb:hover {
        background: #FF6B35;
    }

    /* ════════════════════════════════════════════════════════════════════
       16. INLINE STYLE OVERRIDES (for HTML snippets)
       ════════════════════════════════════════════════════════════════════ */
    span[style*="color:#ccc"], span[style*="color: #ccc"] {
        color: #C7C7CC !important;
    }

    span[style*="color:#aaa"], span[style*="color: #aaa"] {
        color: #8E8E93 !important;
    }

    /* ════════════════════════════════════════════════════════════════════
       17. SELECTION & FOCUS RINGS
       ════════════════════════════════════════════════════════════════════ */
    ::selection {
        background: rgba(255, 107, 53, 0.35);
        color: #FFFFFF;
    }

    *:focus-visible {
        outline: 2px solid rgba(255, 107, 53, 0.5) !important;
        outline-offset: 2px !important;
    }

    /* ════════════════════════════════════════════════════════════════════
       18. STREAMLIT CHROME — Hide deploy button, style header
       ════════════════════════════════════════════════════════════════════ */
    header[data-testid="stHeader"] {
        background: rgba(8, 8, 8, 0.85) !important;
        backdrop-filter: blur(12px) !important;
        border-bottom: 1px solid rgba(255,255,255,0.03) !important;
    }

    /* Sidebar collapse button */
    [data-testid="stSidebarCollapsedControl"] {
        color: #FF6B35 !important;
    }

    /* Bottom padding for chat scroll */
    .block-container {
        padding-bottom: 100px !important;
    }
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

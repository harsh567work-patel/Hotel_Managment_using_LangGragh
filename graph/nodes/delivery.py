"""
delivery.py — Handles delivery address parsing, serviceable city verification,
and delivery-related questions.
Ensures only addresses in SERVICEABLE_CITIES (default: Bangalore) are accepted.
"""

from typing import Optional, Dict, Any, Tuple
from pydantic import BaseModel, Field
from langchain_groq import ChatGroq
from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
from graph.state import OrderState, DeliveryAddress
from config import GROQ_MODEL, GROQ_FAST_MODEL, GROQ_API_KEY, SERVICEABLE_CITIES


class DeliveryAddressExtraction(BaseModel):
    """Structured extraction of delivery address components."""
    street: Optional[str] = Field(
        default=None,
        description="Street address, building/flat number, road, landmark, or neighborhood"
    )
    city: Optional[str] = Field(
        default=None,
        description="City name (e.g., Bangalore, Bengaluru, Mumbai, Delhi)"
    )
    state: Optional[str] = Field(
        default="Karnataka",
        description="State name (e.g., Karnataka, Maharashtra)"
    )


ADDRESS_EXTRACTION_PROMPT = """You are an address extraction assistant for SpiceRoute Kitchen in Bangalore.
Extract the delivery address components (street, city, state) from the user's message.
Guidelines:
1. Extract house/flat number, street, road, and locality into 'street'.
2. If the user mentions Bangalore, Bengaluru, or any known Bangalore locality (e.g. Indiranagar, Koramangala, Whitefield, MG Road, HSR Layout, Jayanagar) and doesn't specify another city, set city to 'Bangalore' and state to 'Karnataka'.
3. If the user specifies another city (e.g. Mumbai, Delhi, Pune, Chennai), extract that city accurately so our system can check delivery serviceability.
"""


def is_city_serviceable(city: Optional[str]) -> bool:
    """Checks whether the city is in the serviceable cities list."""
    if not city:
        return False
    city_norm = city.strip().lower()
    return any(city_norm == sc.strip().lower() for sc in SERVICEABLE_CITIES)


def extract_address_from_message(user_msg: str) -> DeliveryAddressExtraction:
    """Extracts address using structured output."""
    llm = ChatGroq(
        model=GROQ_MODEL,
        temperature=0,
        api_key=GROQ_API_KEY,
    )
    try:
        extractor = llm.with_structured_output(DeliveryAddressExtraction)
        return extractor.invoke([
            SystemMessage(content=ADDRESS_EXTRACTION_PROMPT),
            HumanMessage(content=user_msg),
        ])
    except Exception:
        fallback_llm = ChatGroq(
            model=GROQ_FAST_MODEL,
            temperature=0,
            api_key=GROQ_API_KEY,
        ).with_structured_output(DeliveryAddressExtraction)
        return fallback_llm.invoke([
            SystemMessage(content=ADDRESS_EXTRACTION_PROMPT),
            HumanMessage(content=user_msg),
        ])


def set_delivery_address_node(state: OrderState) -> dict:
    """
    Parses address from user message.
    If unserviceable: politely declines, requests a Bangalore address, keeps cart and pending state.
    If serviceable: updates delivery_address, keeps pending_action, re-shows checkout summary.
    """
    last_human = ""
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            last_human = m.content
            break

    extracted = extract_address_from_message(last_human)
    street = (extracted.street or last_human).strip()
    city = extracted.city or "Bangalore"
    state_name = extracted.state or "Karnataka"

    cart = state.get("cart", [])
    total_price = state.get("total_price", sum(c["unit_price"] * c["quantity"] for c in cart))
    max_prep = state.get("max_prep_time", max((c["prep_time_minutes"] for c in cart), default=20))

    # 1. Check Serviceability
    if not is_city_serviceable(city):
        serviceable_str = ", ".join(SERVICEABLE_CITIES)
        msg = (
            f"⚠️ **Delivery Area Notice:**\n\n"
            f"We currently only deliver within **{serviceable_str}**. "
            f"Unfortunately, we cannot deliver to **{city}**.\n\n"
            "Please provide a delivery address in Bangalore so we can deliver your order!\n"
            "*(Your cart has been saved)*"
        )
        return {
            "pending_action": "awaiting_order_confirmation",
            "messages": [AIMessage(content=msg)],
        }

    # 2. Valid Address
    valid_address: DeliveryAddress = {
        "street": street,
        "city": city,
        "state": state_name,
    }

    # Format checkout summary with the confirmed address
    cart_lines = [
        f"  • {c['dish_name']} x{c['quantity']} — ₹{c['unit_price'] * c['quantity']:.0f} (~{c['prep_time_minutes']} min)"
        for c in cart
    ]
    cart_block = "\n".join(cart_lines) if cart_lines else "  *(Your cart is empty)*"

    msg = (
        f"📍 **Delivery Address Saved:**\n"
        f"  • Street: {street}\n"
        f"  • City: {city}\n"
        f"  • State: {state_name}\n\n"
        f"📋 **Order Review & Checkout:**\n\n"
        f"{cart_block}\n\n"
        f"💰 **Total:** ₹{total_price:.0f}\n"
        f"⏱️ **Prep time:** ~{max_prep} min (Delivery: +15 min)\n"
        f"📍 **Address:** {street}, {city}, {state_name}\n\n"
        f"Please reply **'confirm'** to place your order, or let me know if you'd like to make any changes!"
    )

    return {
        "delivery_address": valid_address,
        "pending_action": "awaiting_order_confirmation",
        "messages": [AIMessage(content=msg)],
    }


def delivery_question_node(state: OrderState) -> dict:
    """
    Answers delivery-related questions regarding service areas, fees, and timelines.
    """
    cart = state.get("cart", [])
    pending = state.get("pending_action")

    serviceable_str = ", ".join(SERVICEABLE_CITIES)
    msg_parts = [
        f"🚚 **SpiceRoute Kitchen Delivery Information:**\n\n"
        f"• **Delivery Areas:** We deliver across all locations in **{serviceable_str}** (including Indiranagar, Koramangala, Whitefield, HSR Layout, MG Road, etc.).\n"
        f"• **Delivery Fee:** **FREE** delivery on all orders!\n"
        f"• **Payment:** Cash or UPI on Delivery.\n"
        f"• **Delivery Time:** Total time is kitchen preparation time (~15–35 min) + **15 minutes** delivery buffer."
    ]

    if pending == "awaiting_order_confirmation" and cart:
        msg_parts.append(
            "\nWhen you're ready, please tell me your delivery address in Bangalore so we can finalize your order!"
        )
    elif cart:
        msg_parts.append(
            "\nReady to order? Just type **'checkout'** when you're done!"
        )
    else:
        msg_parts.append(
            "\nWhat would you like to order today? Ask me for menu suggestions anytime!"
        )

    return {
        "pending_action": pending,
        "messages": [AIMessage(content="\n".join(msg_parts))],
    }

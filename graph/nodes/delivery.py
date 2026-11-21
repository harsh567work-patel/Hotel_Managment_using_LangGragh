"""
delivery.py — Handles delivery address parsing, serviceable city verification,
and delivery-related questions.
Ensures only addresses in SERVICEABLE_CITIES (default: Bangalore) are accepted.
"""

from typing import Optional, Dict, Any, Tuple, Literal
from pydantic import BaseModel, Field
from langchain_groq import ChatGroq
from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
from graph.state import OrderState, DeliveryAddress
from config import GROQ_MODEL, GROQ_FAST_MODEL, GROQ_API_KEY, SERVICEABLE_CITIES

RESTAURANT_LOCATION = "SpiceRoute Kitchen, 12 Food Street, Bangalore"


class OrderTypeExtraction(BaseModel):
    """Structured extraction of order fulfillment type (dine-in, pickup, or delivery)."""
    order_type: Literal["dine_in", "pickup", "delivery"] = Field(
        description="Fulfillment mode: 'dine_in' (eat at restaurant), 'pickup' (takeaway/self-pickup), or 'delivery' (home delivery)"
    )
    table_number: Optional[str] = Field(
        default=None,
        description="Optional table number if the user mentioned one for dine-in (e.g., '4', 'Table 5')"
    )


ORDER_TYPE_PROMPT = """You are an assistant for SpiceRoute Kitchen.
Extract the user's preferred order fulfillment mode from their message:
- 'dine_in': eating at the restaurant (e.g., "dine here", "will dine here", "dine in", "eat here", "table 3")
- 'pickup': takeaway / self-pickup at the counter (e.g., "takeaway", "take out", "I'll pick it up", "self pickup", "parcel")
- 'delivery': home delivery (e.g., "deliver it", "home delivery", "switch to delivery")

If they mention a table number for dine-in, extract it into table_number (just the number/identifier, e.g. "4").
"""


def extract_order_type_from_message(user_msg: str) -> OrderTypeExtraction:
    """Extracts order_type using structured output, with deterministic keyword fallback."""
    msg_lower = user_msg.lower()
    llm = ChatGroq(
        model=GROQ_MODEL,
        temperature=0,
        api_key=GROQ_API_KEY,
    )
    try:
        extractor = llm.with_structured_output(OrderTypeExtraction)
        return extractor.invoke([
            SystemMessage(content=ORDER_TYPE_PROMPT),
            HumanMessage(content=user_msg),
        ])
    except Exception:
        try:
            fallback_llm = ChatGroq(
                model=GROQ_FAST_MODEL,
                temperature=0,
                api_key=GROQ_API_KEY,
            ).with_structured_output(OrderTypeExtraction)
            return fallback_llm.invoke([
                SystemMessage(content=ORDER_TYPE_PROMPT),
                HumanMessage(content=user_msg),
            ])
        except Exception:
            if any(k in msg_lower for k in ["dine", "eat here", "restaurant", "table"]):
                return OrderTypeExtraction(order_type="dine_in")
            if any(k in msg_lower for k in ["pickup", "pick up", "takeaway", "take away", "takeout", "parcel"]):
                return OrderTypeExtraction(order_type="pickup")
            return OrderTypeExtraction(order_type="delivery")


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
        "order_type": "delivery",
        "delivery_address": valid_address,
        "pending_action": "awaiting_order_confirmation",
        "messages": [AIMessage(content=msg)],
    }


def set_order_type_node(state: OrderState) -> dict:
    """
    Sets order fulfillment mode ('dine_in', 'pickup', or 'delivery').
    For dine-in and pickup, no delivery address is needed and there is no delivery buffer time.
    """
    last_human = ""
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            last_human = m.content
            break

    extracted = extract_order_type_from_message(last_human)
    order_type = extracted.order_type
    table_num = extracted.table_number or state.get("table_number")

    cart = state.get("cart", [])
    total_price = state.get("total_price", sum(c["unit_price"] * c["quantity"] for c in cart))
    max_prep = state.get("max_prep_time", max((c["prep_time_minutes"] for c in cart), default=20))
    address = state.get("delivery_address")

    cart_lines = [
        f"  • {c['dish_name']} x{c['quantity']} — ₹{c['unit_price'] * c['quantity']:.0f} (~{c['prep_time_minutes']} min)"
        for c in cart
    ]
    cart_block = "\n".join(cart_lines)

    if order_type == "dine_in":
        table_suffix = f" (Table {table_num})" if table_num else ""
        header = f"🍽️ **Order Mode Updated:** Dine-in at {RESTAURANT_LOCATION}{table_suffix} — no delivery address needed!"
        if cart:
            msg = (
                f"{header}\n\n"
                f"📋 **Order Review & Checkout:**\n\n"
                f"{cart_block}\n\n"
                f"💰 **Total:** ₹{total_price:.0f} (Pay at Restaurant)\n"
                f"⏱️ **Prep time:** ~{max_prep} min\n"
                f"🍽️ **Order Type:** Dine-in{table_suffix} — {RESTAURANT_LOCATION}\n\n"
                "Please reply **'confirm'** to place your dine-in order, or let me know if you'd like any changes!"
            )
            pending = "awaiting_order_confirmation"
        else:
            msg = (
                f"{header}\n\n"
                "Your cart is currently empty. Tell me what delicious dishes you'd like to order!"
            )
            pending = None

        return {
            "order_type": "dine_in",
            "table_number": table_num,
            "pending_action": pending,
            "messages": [AIMessage(content=msg)],
        }

    if order_type == "pickup":
        header = f"🛍️ **Order Mode Updated:** Self-Pickup / Takeaway at {RESTAURANT_LOCATION} — no delivery address needed!"
        if cart:
            msg = (
                f"{header}\n\n"
                f"📋 **Order Review & Checkout:**\n\n"
                f"{cart_block}\n\n"
                f"💰 **Total:** ₹{total_price:.0f} (Pay at Pickup)\n"
                f"⏱️ **Ready for pickup in:** ~{max_prep} min\n"
                f"🛍️ **Order Type:** Self-Pickup / Takeaway — {RESTAURANT_LOCATION}\n\n"
                "Please reply **'confirm'** to place your pickup order, or let me know if you'd like any changes!"
            )
            pending = "awaiting_order_confirmation"
        else:
            msg = (
                f"{header}\n\n"
                "Your cart is currently empty. Tell me what delicious dishes you'd like to order!"
            )
            pending = None

        return {
            "order_type": "pickup",
            "pending_action": pending,
            "messages": [AIMessage(content=msg)],
        }

    # order_type == "delivery"
    header = "🚚 **Order Mode Updated:** Home Delivery"
    if cart:
        if address and address.get("street") and is_city_serviceable(address.get("city")):
            addr_str = f"{address.get('street')}, {address.get('city')}, {address.get('state', 'Karnataka')}"
            msg = (
                f"{header}\n\n"
                f"📋 **Order Review & Checkout:**\n\n"
                f"{cart_block}\n\n"
                f"💰 **Total:** ₹{total_price:.0f}\n"
                f"⏱️ **Prep time:** ~{max_prep} min (Delivery: +15 min)\n"
                f"📍 **Address:** {addr_str}\n\n"
                "Please reply **'confirm'** to place your delivery order, or let me know if you want to make any changes!"
            )
        else:
            msg = (
                f"{header}\n\n"
                "Please provide your **delivery address** in Bangalore (e.g., *'12 MG Road, Bangalore'*) so we can confirm your order!"
            )
        pending = "awaiting_order_confirmation"
    else:
        msg = (
            f"{header}\n\n"
            "Your cart is currently empty. Tell me what you'd like to order!"
        )
        pending = None

    return {
        "order_type": "delivery",
        "pending_action": pending,
        "messages": [AIMessage(content=msg)],
    }


def delivery_question_node(state: OrderState) -> dict:
    """
    Answers delivery, dine-in, and pickup-related questions regarding service areas, fees, and timelines.
    """
    cart = state.get("cart", [])
    pending = state.get("pending_action")

    serviceable_str = ", ".join(SERVICEABLE_CITIES)
    msg_parts = [
        f"🚚 **SpiceRoute Kitchen Delivery, Dine-in & Pickup Information:**\n\n"
        f"• **Delivery Areas:** We deliver across all locations in **{serviceable_str}** (including Indiranagar, Koramangala, Whitefield, HSR Layout, MG Road, etc.).\n"
        f"• **Delivery Fee:** **FREE** delivery on all orders!\n"
        f"• **Dine-in & Self-Pickup:** Available at **{RESTAURANT_LOCATION}** (no delivery address required!).\n"
        f"• **Payment:** Cash or UPI (on Delivery, at Restaurant, or at Pickup).\n"
        f"• **Timing:** Kitchen prep time (~15–35 min); add **+15 minutes** for home delivery."
    ]

    if pending == "awaiting_order_confirmation" and cart:
        msg_parts.append(
            "\nWhen you're ready, share your **delivery address** in Bangalore, or let me know if you prefer **dine-in** or **takeaway/pickup**!"
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

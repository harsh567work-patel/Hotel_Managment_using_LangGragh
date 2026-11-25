"""
pages/1_Customer_Menu.py — Customer Menu Explorer with FSSAI symbols, allergen declarations,
and deterministic Table Split hard-constraint filtering preview.
"""

from __future__ import annotations

import streamlit as st

from table_split.db.dietary_repo import DietaryRepository
from table_split.dietary import (
    HardDietaryConstraints,
    is_item_allowed,
)
from table_split.ui_components import MENU_CARD_CSS, render_menu_item_card_html

st.set_page_config(
    page_title="Menu & Allergen Guide — SpiceRoute Kitchen",
    page_icon="📋",
    layout="wide",
)

st.markdown(
    "<style>\n"
    ".stApp, [data-testid=\"stAppViewContainer\"] { background-color: #080808; color: #E5E5EA; }\n"
    "h1, h2, h3 { color: #FF6B35; }\n"
    "</style>\n"
    + MENU_CARD_CSS,
    unsafe_allow_html=True,
)

st.title("📋 SpiceRoute Kitchen — Menu & Dietary Guide")
st.caption(
    "FSSAI Veg (green circle in square) & Non-Veg (brown triangle in square, includes egg-containing dishes). "
    "Items missing verification are labeled 'Dietary info not verified' and excluded whenever dietary/allergen constraints are active."
)

repo = DietaryRepository()
items = repo.list_items()

with st.sidebar:
    st.markdown("### Filter by Dietary & Allergen Constraints")
    selected_diets = st.multiselect(
        "Hard Dietary Requirements",
        options=["vegetarian", "vegan", "jain", "halal", "gluten_free"],
        format_func=lambda x: x.replace("_", " ").title(),
    )
    exclude_egg = st.checkbox(
        "Exclude Egg for Vegetarian (FSSAI Rule)",
        value=True,
        help="When enabled, egg-containing dishes are classified non-veg and excluded for vegetarians.",
    )
    allergen_choices = {
        "cereals_gluten": "Cereals containing gluten",
        "crustaceans": "Crustaceans",
        "milk": "Milk",
        "dairy": "Dairy",
        "eggs": "Eggs",
        "fish": "Fish",
        "peanuts": "Peanuts",
        "tree_nuts": "Tree nuts",
        "soy": "Soybeans",
        "sulphite": "Sulphites",
        "sesame": "Sesame (non-statutory)",
        "mustard": "Mustard (non-statutory)",
    }
    selected_allergens = st.multiselect(
        "Allergens to Strictly Avoid",
        options=list(allergen_choices.keys()),
        format_func=lambda k: allergen_choices[k],
    )
    categories = sorted({it.category for it in items if it.category})
    selected_category = st.selectbox("Category", options=["All", *categories])

constraints = HardDietaryConstraints(
    diets=set(selected_diets),
    allergens=set(selected_allergens),
    exclude_egg_for_vegetarian=exclude_egg,
)

filtered_items = [
    it for it in items
    if selected_category == "All" or it.category == selected_category
]

allowed_cards = []
blocked_cards = []
for it in filtered_items:
    decision = is_item_allowed(it, constraints)
    if decision.allowed:
        allowed_cards.append(it)
    else:
        blocked_cards.append((it, decision))

st.markdown(f"**Showing {len(allowed_cards)} matching dish(es)**"
            + (f" ({len(blocked_cards)} excluded by active constraints)" if constraints.has_active_constraints else ""))

cols = st.columns(2)
for idx, it in enumerate(allowed_cards):
    with cols[idx % 2]:
        st.markdown(render_menu_item_card_html(it), unsafe_allow_html=True)

if constraints.has_active_constraints and blocked_cards:
    with st.expander(f"Excluded Dishes ({len(blocked_cards)}) — Why they were filtered out"):
        for it, dec in blocked_cards:
            st.markdown(
                f"- **{it.dish_name}** (`{it.item_id}`): {'; '.join(dec.reasons)}"
                + (f" — *UNKNOWN fields: {', '.join(dec.unknownFields)}*" if dec.unknownFields else "")
            )

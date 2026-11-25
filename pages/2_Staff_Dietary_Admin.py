"""
pages/2_Staff_Dietary_Admin.py — Staff Dietary Verification & Data-Quality Dashboard.

Allows restaurant kitchen & QA staff to:
1. View real-time Data Quality completion percentage.
2. Review items blocking Table Split safety, ranked by sales volume.
3. Edit tri-state diet, ingredient (honey, onion/garlic, root veg), and allergen fields.
4. Record explicit verified_by and verified_at sign-off timestamps.
"""

from __future__ import annotations

import streamlit as st

from table_split.db.dietary_repo import DietaryRepository
from table_split.dietary import ALLERGEN_TRISTATE_FIELDS, DIET_TRISTATE_FIELDS
from table_split.enums import TriState

st.set_page_config(
    page_title="Staff Dietary Admin — SpiceRoute Kitchen",
    page_icon="🛡️",
    layout="wide",
)

st.markdown(
    """
    <style>
    .stApp, [data-testid="stAppViewContainer"] { background-color: #080808; color: #E5E5EA; }
    h1, h2, h3 { color: #FF6B35; }
    </style>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.markdown("### Navigation")
    st.page_link("app.py", label="💬 AI Ordering Concierge")
    st.page_link("pages/1_Customer_Menu.py", label="📋 Menu & Allergens")
    st.page_link("pages/2_Staff_Dietary_Admin.py", label="🛡️ Staff Dietary Admin")

st.title("🛡️ Staff Dietary & Allergen Verification Portal")

repo = DietaryRepository()
report = repo.generate_data_quality_report()

m1, m2, m3, m4 = st.columns(4)
m1.metric("Total Menu Items", report["total_items"])
m2.metric("Fully Verified", f"{report['fully_verified_count']} ({report['fully_verified_percentage']}%)")
m3.metric("Staff Signed-Off", report["staff_signed_off_count"])
m4.metric("Blocking Table Split Safety", report["unverified_or_incomplete_count"])

st.markdown("---")
st.subheader("Items Blocking Table Split Safety (Ordered by Sales Volume)")

blocking = report["blocking_items"]
if blocking:
    st.dataframe(
        [
            {
                "Item ID": b["item_id"],
                "Dish Name": b["dish_name"],
                "Sales Volume": b["sales_volume"],
                "Staff Signed-Off": "Yes" if b["is_verified"] else "No",
                "UNKNOWN Count": b["unknown_count"],
                "Reason": b["blocking_reason"],
            }
            for b in blocking
        ],
        use_container_width=True,
        hide_index=True,
    )
else:
    st.success("100% of menu items are fully verified with zero UNKNOWN dietary or allergen fields!")

st.markdown("---")
st.subheader("Review & Verify Dish Attributes")

all_items = sorted(repo.list_items(), key=lambda it: (it.is_fully_verified, -it.sales_volume, it.item_id))
item_labels = {
    it.item_id: (
        f"[{'VERIFIED' if it.is_fully_verified else f'BLOCKING: {len(it.get_unknown_fields())} UNKNOWN'}] "
        f"{it.item_id} — {it.dish_name} (Sales: {it.sales_volume})"
    )
    for it in all_items
}

selected_id = st.selectbox(
    "Select Menu Item to Review",
    options=[it.item_id for it in all_items],
    format_func=lambda x: item_labels[x],
)

selected_item = next(it for it in all_items if it.item_id == selected_id)
tri_options = [TriState.YES.value, TriState.NO.value, TriState.UNKNOWN.value]

with st.form(key=f"verify_form_{selected_id}"):
    st.markdown(f"**Editing `{selected_item.item_id}` — {selected_item.dish_name}**")
    edits: dict[str, str | int | bool | None] = {}

    col_diet, col_allergen = st.columns(2)
    with col_diet:
        st.markdown("#### Diet & Ingredient Flags")
        for field_name in DIET_TRISTATE_FIELDS:
            cur = getattr(selected_item, field_name).value
            idx = tri_options.index(cur) if cur in tri_options else 2
            edits[field_name] = st.selectbox(field_name, options=tri_options, index=idx, key=f"{selected_id}_{field_name}")

    with col_allergen:
        st.markdown("#### Allergen Declarations (Plus Non-Statutory)")
        for field_name in ALLERGEN_TRISTATE_FIELDS:
            cur = getattr(selected_item, field_name).value
            idx = tri_options.index(cur) if cur in tri_options else 2
            edits[field_name] = st.selectbox(field_name, options=tri_options, index=idx, key=f"{selected_id}_{field_name}")

    c1, c2, c3, c4 = st.columns(4)
    spice_cur = selected_item.spice_level
    spice_val = c1.selectbox("Spice Level (0-4)", options=[None, 0, 1, 2, 3, 4], index=[None, 0, 1, 2, 3, 4].index(spice_cur))
    serves_val = c2.number_input("Serves (persons)", min_value=1, value=int(selected_item.serves))
    shareable_val = c3.checkbox("Shareable", value=bool(selected_item.shareable))
    sales_val = c4.number_input("Sales Volume", min_value=0, value=int(selected_item.sales_volume))

    note_val = st.text_input("Cross-Contact / Preparation Note", value=selected_item.cross_contact_note or "")
    reviewer_val = st.text_input(
        "Verified By (Staff Name / Title)",
        value=selected_item.verified_by or "",
        placeholder="e.g. Chef Priya (Sous Chef)",
    )

    submitted = st.form_submit_button("✅ Save & Record Verification Sign-Off", use_container_width=True)
    if submitted:
        edits["spice_level"] = spice_val
        edits["serves"] = int(serves_val)
        edits["shareable"] = bool(shareable_val)
        edits["sales_volume"] = int(sales_val)
        edits["cross_contact_note"] = note_val.strip() or None
        try:
            saved = repo.verify_item(selected_id, updates=edits, verified_by=reviewer_val)
            st.success(
                f"Saved verification for {saved.item_id} ({saved.dish_name}) by {saved.verified_by}. "
                f"Remaining UNKNOWN fields: {len(saved.get_unknown_fields())}."
            )
            st.rerun()
        except ValueError as err:
            st.error(f"Validation error: {err}")

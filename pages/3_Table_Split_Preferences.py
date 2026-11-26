"""
pages/3_Table_Split_Preferences.py — Table Split Preference Intake (Chips + Optional Free-Text Parser).

Features:
- Explicit consent line before first dietary entry (what is stored, who sees it, when deleted)
- Primary structured chip UI (Hard: diet, allergen, budget; Soft: spice range, category, dislikes)
- "Mark as private" toggle showing peers only "A constraint was applied"
- Optional AI free-text parser behind AI_PARSER_ENABLED with editable confirmation chips
  ("Did you mean...?" prompt below 0.7 confidence) and friendly fallback to chips
"""

from __future__ import annotations

import html

import streamlit as st

from config import AI_PARSER_ENABLED, TABLE_SPLIT_DB_PATH
from table_split.services.preference_parser import (
    HARD_ONLY_KINDS,
    LOW_CONFIDENCE_THRESHOLD,
    SPICE_RANK_LABELS,
    GroqPreferenceParser,
    build_menu_vocabulary_from_json,
    format_chip_label,
    record_parser_log,
)
from table_split.services.session_service import SessionError, SessionService

st.set_page_config(
    page_title="Table Split Preferences — SpiceRoute Kitchen",
    page_icon="🥗",
    layout="wide",
)

st.markdown(
    "<style>\n"
    ".stApp, [data-testid=\"stAppViewContainer\"] { background-color: #080808; color: #E5E5EA; }\n"
    "h1, h2, h3 { color: #FF6B35; }\n"
    ".ts-consent-box {\n"
    "  background: #151517;\n"
    "  border: 1px solid #FF6B35;\n"
    "  border-radius: 10px;\n"
    "  padding: 14px 16px;\n"
    "  margin-bottom: 16px;\n"
    "  color: #E5E5EA;\n"
    "}\n"
    ".ts-chip {\n"
    "  display: inline-block;\n"
    "  padding: 4px 10px;\n"
    "  margin: 3px 6px 3px 0;\n"
    "  border-radius: 999px;\n"
    "  font-size: 0.85rem;\n"
    "  font-weight: 600;\n"
    "  border: 1px solid #3A3A40;\n"
    "  background: #1E1E24;\n"
    "}\n"
    ".ts-chip-hard { border-color: #FF6B35; color: #FFB399; }\n"
    ".ts-chip-soft { border-color: #38BDF8; color: #BAE6FD; }\n"
    "</style>",
    unsafe_allow_html=True,
)


def _get_service() -> SessionService:
    return SessionService(db_path=TABLE_SPLIT_DB_PATH)


def _init_session_state(service: SessionService) -> None:
    if "ts_pref_sid" not in st.session_state:
        created = service.create_session(display_name="You (Host)")
        sid = created["session_id"]
        peer = service.join_session(sid, join_token=created["join_token"], display_name="Table Peer")
        st.session_state["ts_pref_sid"] = sid
        st.session_state["ts_pref_pid"] = created["host_participant_id"]
        st.session_state["ts_pref_peer_pid"] = peer["participant_id"]
    if "ts_consent_ack" not in st.session_state:
        st.session_state["ts_consent_ack"] = False
    if "ts_pending_parsed" not in st.session_state:
        st.session_state["ts_pending_parsed"] = []
    if "ts_last_parse_msg" not in st.session_state:
        st.session_state["ts_last_parse_msg"] = None


service = _get_service()
vocab = build_menu_vocabulary_from_json()
_init_session_state(service)

sid: str = st.session_state["ts_pref_sid"]
pid: str = st.session_state["ts_pref_pid"]
peer_pid: str = st.session_state["ts_pref_peer_pid"]

st.title("🥗 Table Split — Your Food Preferences")

# ── Consent Line (Always visible before first dietary entry) ─────────────────
st.markdown(
    "<div class=\"ts-consent-box\" data-testid=\"dietary-consent-banner\">"
    "<strong>Privacy & Dietary Data Notice:</strong> "
    "<strong>What is stored:</strong> Your typed dietary restrictions, food allergies, spice preference, personal budget cap, and category/dish preferences. "
    "<strong>Who sees it:</strong> Table participants see your public preference chips; any item marked <em>Private</em> is used only by the ordering engine while peers see only <em>\"A constraint was applied\"</em>. "
    "<strong>When it is deleted:</strong> All session preferences are automatically purged <strong>24 hours after session expiry</strong>, and PII-redacted parser logs are purged after <strong>7 days</strong>."
    "</div>",
    unsafe_allow_html=True,
)

consent_ack = st.checkbox(
    "I understand and consent to saving my dietary preferences for this Table Split session.",
    value=bool(st.session_state["ts_consent_ack"]),
    key="ts_consent_checkbox",
)
st.session_state["ts_consent_ack"] = consent_ack

try:
    my_state = service.get_session_state(sid, viewer_participant_id=pid)
    peer_state = service.get_session_state(sid, viewer_participant_id=peer_pid)
except SessionError as exc:
    st.error(f"Session error: {exc}")
    st.stop()

current_version = int(my_state["version"])
st.caption(f"Session ID: `{sid}` · Version: `{current_version}` · Status: `{my_state['status']}`")

col_chips, col_freetext = st.columns([1.15, 1.0], gap="large")

# ── Part A: Primary Structured Chips Intake ──────────────────────────────────
with col_chips:
    st.subheader("1. Quick Chips (Primary Intake)")
    with st.form("chip_intake_form"):
        selected_diets = st.multiselect(
            "Dietary Rules (Always HARD)",
            options=["vegetarian", "vegan", "jain", "halal", "gluten_free"],
            format_func=lambda x: x.replace("_", " ").title(),
        )
        selected_allergens = st.multiselect(
            "Allergens to Strictly Avoid (Always HARD)",
            options=[
                "milk",
                "dairy",
                "peanuts",
                "tree_nuts",
                "cereals_gluten",
                "eggs",
                "soy",
                "crustaceans",
                "fish",
                "sulphite",
                "sesame",
                "mustard",
            ],
            format_func=lambda x: x.replace("_", " ").title(),
        )

        budget_col1, budget_col2 = st.columns([1.4, 1.0])
        with budget_col1:
            budget_rupees = st.number_input(
                "Personal Share Budget Cap (₹, 0 = no cap)",
                min_value=0,
                max_value=50000,
                value=0,
                step=50,
            )
        with budget_col2:
            budget_strength = st.selectbox("Budget Strength", options=["HARD", "SOFT"], index=0)

        spice_range = st.select_slider(
            "Preferred Spice Range (SOFT)",
            options=[0, 1, 2, 3, 4],
            value=(1, 3),
            format_func=lambda lvl: f"{lvl} ({SPICE_RANK_LABELS[lvl]})",
        )

        cat_options = {c.id: c.name for c in vocab.categories}
        selected_categories = st.multiselect(
            "Must-Include Menu Categories (SOFT)",
            options=list(cat_options.keys()),
            format_func=lambda cid: cat_options[cid],
        )
        cat_weight = st.slider("Category Preference Weight", min_value=1, max_value=5, value=3)

        dish_options = {d.id: f"{d.dish_name} ({d.id})" for d in vocab.dishes}
        disliked_dishes = st.multiselect(
            "Disliked Dishes (SOFT)",
            options=list(dish_options.keys()),
            format_func=lambda did: dish_options[did],
        )
        freeform_note = st.text_input("Optional Short Food Note (SOFT)", max_chars=160)
        mark_private = st.checkbox(
            "Mark as private (engine uses constraint; peers only see 'A constraint was applied')",
            value=False,
        )
        mark_ready = st.checkbox("Mark me as Ready", value=True)
        submit_chips = st.form_submit_button("Save Preference Chips", type="primary")

    if submit_chips:
        if not consent_ack:
            st.warning("Please accept the privacy & dietary consent notice above before saving preferences.")
        else:
            to_save: list[dict] = []
            for d in selected_diets:
                to_save.append({
                    "kind": "diet",
                    "strength": "HARD",
                    "weight": 5,
                    "value": {"diet": d},
                    "source": "chip",
                    "confirmed_by_user": True,
                    "is_private": mark_private,
                })
            for a in selected_allergens:
                to_save.append({
                    "kind": "allergen",
                    "strength": "HARD",
                    "weight": 5,
                    "value": {"allergen": a},
                    "source": "chip",
                    "confirmed_by_user": True,
                    "is_private": mark_private,
                })
            if budget_rupees > 0:
                to_save.append({
                    "kind": "budget_max_paise",
                    "strength": budget_strength,
                    "weight": 4,
                    "value": {"max_paise": int(budget_rupees) * 100},
                    "source": "chip",
                    "confirmed_by_user": True,
                    "is_private": mark_private,
                })
            to_save.append({
                "kind": "spice_range",
                "strength": "SOFT",
                "weight": 2,
                "value": {"min": int(spice_range[0]), "max": int(spice_range[1])},
                "source": "chip",
                "confirmed_by_user": True,
                "is_private": mark_private,
            })
            for cid in selected_categories:
                to_save.append({
                    "kind": "must_include_category",
                    "strength": "SOFT",
                    "weight": int(cat_weight),
                    "value": {"category_id": cid},
                    "source": "chip",
                    "confirmed_by_user": True,
                    "is_private": mark_private,
                })
            for did in disliked_dishes:
                to_save.append({
                    "kind": "dislike_item",
                    "strength": "SOFT",
                    "weight": 2,
                    "value": {"item_id": did, "label": vocab.dish_name_map().get(did, did)},
                    "source": "chip",
                    "confirmed_by_user": True,
                    "is_private": mark_private,
                })
            if freeform_note.strip():
                to_save.append({
                    "kind": "freeform",
                    "strength": "SOFT",
                    "weight": 1,
                    "value": {"note": freeform_note.strip()},
                    "source": "chip",
                    "confirmed_by_user": True,
                    "is_private": mark_private,
                })

            try:
                service.upsert_preferences(
                    sid,
                    participant_id=pid,
                    expected_version=current_version,
                    preferences=to_save,
                    is_ready=mark_ready,
                )
                st.success("Preferences saved cleanly!")
                st.rerun()
            except SessionError as exc:
                st.error(f"Could not save preferences: {exc}")

# ── Part B: Free-Text Parser + Confirmation Loop ─────────────────────────────
with col_freetext:
    st.subheader("2. Natural Language Helper (Optional)")
    if not AI_PARSER_ENABLED:
        st.info(
            "AI free-text parsing is currently disabled (`AI_PARSER_ENABLED=false`). "
            "Use the Quick Chips panel on the left at any time."
        )

    raw_text = st.text_area(
        "Describe your preferences in English, Hinglish, or Hindi",
        placeholder='e.g. "main vegan hoon, dairy bilkul nahi, kuch spicy chahiye, 250 se zyada nahi"',
        height=95,
    )
    locale_choice = st.selectbox("Language / Locale Hint", options=["en-IN", "hi-IN", "en-US"], index=0)

    if st.button("Parse Free Text into Chips", disabled=not AI_PARSER_ENABLED):
        if not consent_ack:
            st.warning("Please acknowledge the consent notice above before parsing dietary preferences.")
        elif not raw_text.strip():
            st.warning("Enter a preference phrase first, or use the chips on the left.")
        else:
            parser = GroqPreferenceParser()
            parsed = parser.parse(raw_text, locale=locale_choice, menu_vocabulary=vocab)
            record_parser_log(
                service.db_path,
                session_id=sid,
                participant_id=pid,
                locale=locale_choice,
                raw_input=raw_text,
                parsed=parsed,
                error_code=None if parsed.status == "success" else parsed.status.upper(),
            )
            st.session_state["ts_pending_parsed"] = [it.model_dump() for it in parsed.items]
            st.session_state["ts_last_parse_msg"] = parsed.friendly_message

    if st.session_state.get("ts_last_parse_msg"):
        st.warning(st.session_state["ts_last_parse_msg"])

    pending_items: list[dict] = st.session_state.get("ts_pending_parsed", [])
    if pending_items:
        st.markdown("#### Review & Confirm Parsed Chips")
        st.caption("Nothing enters the ordering engine until you click Confirm.")

        edited_items: list[dict] = []
        keep_flags: list[bool] = []

        for idx, item in enumerate(pending_items):
            kind = item["kind"]
            conf = float(item.get("confidence", 1.0))
            label = item.get("display_label") or format_chip_label(kind, item["strength"], item["value"], vocab)

            with st.container(border=True):
                if conf < LOW_CONFIDENCE_THRESHOLD:
                    st.warning(f"Did you mean **{label}**? (Confidence: {conf:.2f} — please edit or confirm)")
                else:
                    st.markdown(f"**{label}** (Confidence: {conf:.2f})")

                c1, c2, c3 = st.columns([1.0, 1.0, 1.0])
                with c1:
                    keep = st.checkbox("Confirm", value=True, key=f"parse_keep_{idx}")
                with c2:
                    if kind in HARD_ONLY_KINDS:
                        strength = "HARD"
                        st.text_input("Strength", value="HARD (locked)", disabled=True, key=f"parse_str_{idx}")
                    else:
                        strength = st.selectbox(
                            "Edit Strength",
                            options=["HARD", "SOFT"],
                            index=0 if item["strength"] == "HARD" else 1,
                            key=f"parse_str_{idx}",
                        )
                with c3:
                    priv_item = st.checkbox("Private", value=False, key=f"parse_priv_{idx}")

                keep_flags.append(keep)
                edited_items.append({
                    "kind": kind,
                    "strength": strength,
                    "weight": int(item.get("weight", 1)),
                    "value": item["value"],
                    "source": "freetext_parsed",
                    "raw_text": item.get("source_span"),
                    "parser_confidence": conf,
                    "confirmed_by_user": True,
                    "is_private": priv_item,
                })

        if st.button("Confirm & Save Parsed Preferences", type="primary"):
            confirmed_batch = [it for it, keep in zip(edited_items, keep_flags, strict=True) if keep]
            if not confirmed_batch:
                st.session_state["ts_pending_parsed"] = []
                st.info("Discarded unconfirmed suggestions.")
                st.rerun()
            else:
                try:
                    service.upsert_preferences(
                        sid,
                        participant_id=pid,
                        expected_version=current_version,
                        preferences=confirmed_batch,
                        is_ready=True,
                    )
                    st.session_state["ts_pending_parsed"] = []
                    st.session_state["ts_last_parse_msg"] = None
                    st.success(f"Confirmed and saved {len(confirmed_batch)} preference(s)!")
                    st.rerun()
                except SessionError as exc:
                    st.error(f"Could not save confirmed preferences: {exc}")

st.divider()

# ── Live State Comparison: Owner View vs Peer View ───────────────────────────
v_owner, v_peer = st.columns(2)
my_part = next(p for p in my_state["participants"] if p["id"] == pid)
peer_view_of_me = next(p for p in peer_state["participants"] if p["id"] == pid)

with v_owner:
    st.markdown("### Your Saved Preferences (Owner View)")
    if not my_part["preferences"]:
        st.caption("No confirmed preferences saved yet.")
    else:
        chips_html = ""
        for p in my_part["preferences"]:
            lbl = format_chip_label(p["kind"], p["strength"], p["value"], vocab)
            priv_tag = " · 🔒 Private" if p["is_private"] else ""
            css_cls = "ts-chip-hard" if p["strength"] == "HARD" else "ts-chip-soft"
            chips_html += f'<span class="ts-chip {css_cls}">{html.escape(lbl + priv_tag)}</span>'
        st.markdown(chips_html, unsafe_allow_html=True)

with v_peer:
    st.markdown("### What Other Participants See (Peer View)")
    if not peer_view_of_me["preferences"] and peer_view_of_me["private_constraints_count"] == 0:
        st.caption("No constraints shared yet.")
    else:
        peer_chips = ""
        for p in peer_view_of_me["preferences"]:
            lbl = format_chip_label(p["kind"], p["strength"], p["value"], vocab)
            css_cls = "ts-chip-hard" if p["strength"] == "HARD" else "ts-chip-soft"
            peer_chips += f'<span class="ts-chip {css_cls}">{html.escape(lbl)}</span>'
        if peer_view_of_me["private_constraints_count"] > 0:
            notice = peer_view_of_me["private_constraint_notice"] or "A constraint was applied"
            cnt = peer_view_of_me["private_constraints_count"]
            peer_chips += f'<span class="ts-chip ts-chip-hard">🔒 {html.escape(notice)} ({cnt})</span>'
        st.markdown(peer_chips, unsafe_allow_html=True)

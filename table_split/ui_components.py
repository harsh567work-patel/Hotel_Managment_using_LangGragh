"""
table_split.ui_components — Pure HTML/SVG renderers for SpiceRoute Kitchen's Menu Cards.

Follows FSSAI visual symbol conventions strictly (no invented icons):
- Vegetarian: Green filled circle inside green bordered square.
- Non-Vegetarian: Brown filled triangle inside brown bordered square.
  (Includes egg-containing dishes per egg rule.)
- Unverified dietary data: Displays explicit "Dietary info not verified" text
  without inventing a synthetic icon.
"""

from __future__ import annotations

import html
from typing import Literal

from table_split.dietary import DietaryItem, classify_fssai_symbol
from table_split.enums import TriState

FSSAI_VEG_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 18 18" '
    'role="img" aria-label="Vegetarian" title="Vegetarian (FSSAI Veg Symbol)">'
    '<rect x="1" y="1" width="16" height="16" rx="2" fill="none" stroke="#16A34A" stroke-width="2"/>'
    '<circle cx="9" cy="9" r="4.5" fill="#16A34A"/>'
    "</svg>"
)

FSSAI_NON_VEG_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 18 18" '
    'role="img" aria-label="Non-Vegetarian" title="Non-Vegetarian (FSSAI Non-Veg Symbol)">'
    '<rect x="1" y="1" width="16" height="16" rx="2" fill="none" stroke="#8B4513" stroke-width="2"/>'
    '<polygon points="9,4 14,13 4,13" fill="#8B4513"/>'
    "</svg>"
)


def render_fssai_symbol_svg(classification: Literal["veg", "non_veg", "unverified"]) -> str:
    """Returns canonical FSSAI Veg/Non-Veg SVG, or empty string if unverified (never invents symbols)."""
    if classification == "veg":
        return FSSAI_VEG_SVG
    if classification == "non_veg":
        return FSSAI_NON_VEG_SVG
    return ""


def format_allergen_text(item: DietaryItem) -> str:
    """Formats customer-facing allergen summary text from explicit tri-state fields."""
    present = item.declared_allergens()
    unknowns = item.get_unknown_fields()

    parts: list[str] = []
    if present:
        parts.append("Allergens: " + ", ".join(present))
    elif item.is_verified and not unknowns:
        parts.append("Allergens: None declared")
    else:
        parts.append("Allergens: Unverified / Check staff notes")

    if item.diet_egg == TriState.YES or item.allergen_eggs == TriState.YES:
        parts.append("(Egg rule: classified Non-Veg)")

    return " · ".join(parts)


def render_menu_item_card_html(item: DietaryItem) -> str:
    """Renders a self-contained HTML item card matching Ember Luxe theme with FSSAI symbols & allergen text."""
    classification = classify_fssai_symbol(item)
    symbol_svg = render_fssai_symbol_svg(classification)

    safe_name = html.escape(item.dish_name or item.item_id)
    safe_cat = html.escape(item.category or "Menu")
    safe_desc = html.escape(item.description or "")
    rupees = item.price_paise // 100
    paise = item.price_paise % 100
    price_str = f"₹{rupees}" if paise == 0 else f"₹{rupees}.{paise:02d}"

    allergen_text = html.escape(format_allergen_text(item))

    diet_badges: list[str] = []
    if item.is_verified:
        if item.diet_vegan == TriState.YES:
            diet_badges.append('<span class="ts-badge ts-badge-green">Vegan</span>')
        if item.diet_jain == TriState.YES:
            diet_badges.append('<span class="ts-badge ts-badge-green">Jain</span>')
        if item.diet_gluten_free == TriState.YES:
            diet_badges.append('<span class="ts-badge ts-badge-blue">Gluten-Free</span>')
        if item.diet_halal == TriState.YES:
            diet_badges.append('<span class="ts-badge ts-badge-blue">Halal</span>')

    unverified_banner = ""
    if not item.is_verified:
        unverified_banner = (
            '<div class="ts-unverified-banner">'
            "Dietary info not verified — excluded from Table Split proposals when dietary/allergen constraints are active"
            "</div>"
        )
    elif item.get_unknown_fields():
        unknown_list = html.escape(", ".join(item.get_unknown_fields()[:5]))
        suffix = f" (+{len(item.get_unknown_fields()) - 5} more)" if len(item.get_unknown_fields()) > 5 else ""
        unverified_banner = (
            '<div class="ts-unverified-banner">'
            f"Dietary info not verified for: {unknown_list}{suffix}"
            "</div>"
        )

    cross_contact_html = ""
    if item.cross_contact_note:
        cross_contact_html = (
            f'<div class="ts-cross-contact">Kitchen note: {html.escape(item.cross_contact_note)}</div>'
        )

    avail_badge = (
        '<span class="ts-badge ts-badge-muted">Out of Stock</span>'
        if not item.is_available
        else ""
    )

    spice_str = ""
    if item.spice_level is not None:
        spice_str = f"<span>Spice: {'🌶️' * max(item.spice_level, 0) or 'Mild/None'}</span>"

    badges_html = " ".join([avail_badge, *diet_badges]).strip()

    return f"""
    <div class="ts-menu-card" data-item-id="{html.escape(item.item_id)}">
        <div class="ts-card-header">
            <div class="ts-title-row">
                {symbol_svg}
                <strong class="ts-dish-name">{safe_name}</strong>
                <span class="ts-cat-pill">{safe_cat}</span>
            </div>
            <span class="ts-price">{price_str}</span>
        </div>
        <p class="ts-desc">{safe_desc}</p>
        <div class="ts-meta-row">{badges_html} {spice_str}</div>
        <div class="ts-allergen-line">{allergen_text}</div>
        {unverified_banner}
        {cross_contact_html}
    </div>
    """


MENU_CARD_CSS = """
<style>
.ts-menu-card {
    background: linear-gradient(145deg, #161619 0%, #101012 100%);
    border: 1px solid rgba(255, 107, 53, 0.16);
    border-radius: 14px;
    padding: 16px 18px;
    margin-bottom: 14px;
    color: #E5E5EA;
}
.ts-card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    margin-bottom: 8px;
}
.ts-title-row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
}
.ts-dish-name {
    font-size: 16px;
    color: #FFFFFF;
}
.ts-cat-pill {
    font-size: 11px;
    color: #AEAEB2;
    background: rgba(255,255,255,0.06);
    padding: 2px 8px;
    border-radius: 999px;
}
.ts-price {
    font-weight: 800;
    color: #FF6B35;
    font-size: 16px;
    white-space: nowrap;
}
.ts-desc {
    font-size: 13.5px;
    color: #C7C7CC;
    margin: 0 0 10px 0;
    line-height: 1.5;
}
.ts-meta-row {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
    margin-bottom: 8px;
    font-size: 12px;
}
.ts-badge {
    padding: 2px 8px;
    border-radius: 6px;
    font-weight: 600;
    font-size: 11px;
}
.ts-badge-green {
    background: rgba(22, 163, 74, 0.16);
    color: #4ADE80;
    border: 1px solid rgba(22, 163, 74, 0.35);
}
.ts-badge-blue {
    background: rgba(90, 200, 250, 0.14);
    color: #70D5FF;
    border: 1px solid rgba(90, 200, 250, 0.3);
}
.ts-badge-muted {
    background: rgba(142, 142, 147, 0.2);
    color: #AEAEB2;
}
.ts-allergen-line {
    font-size: 12.5px;
    color: #FFD60A;
    font-weight: 500;
    margin-bottom: 6px;
}
.ts-unverified-banner {
    background: rgba(255, 159, 10, 0.12);
    border: 1px solid rgba(255, 159, 10, 0.35);
    color: #FFB340;
    border-radius: 8px;
    padding: 6px 10px;
    font-size: 12px;
    font-weight: 600;
    margin-top: 6px;
}
.ts-cross-contact {
    font-size: 11.5px;
    color: #8E8E93;
    margin-top: 6px;
}
</style>
"""

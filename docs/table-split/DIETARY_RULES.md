# SpiceRoute Kitchen — Plain-Language Dietary & Allergen Rules (Owner Sign-Off Sheet)

> [!IMPORTANT]
> **Purpose of This Document:** This document explains in plain, everyday English exactly how Table Split Mode decides whether a menu dish is safe to serve to diners with dietary rules or food allergies. Please review every rule below carefully before signing at the bottom.
>
> **Regulatory Notice:** Statutory food labelling terms and allergen lists are subject to human legal and food-safety auditor verification.

---

## 1. The Golden Safety Principle: "Unknown Means Unsafe"

1. Every dietary and allergen fact on a dish has three possible answers: **YES**, **NO**, or **UNKNOWN**.
2. **If a dish has not been checked and signed off by kitchen staff (`verified_by` and `verified_at` missing):**
   - Customers will see a clear warning on the dish card: **"Dietary info not verified"**.
   - If *anyone* at the table selects a dietary requirement (like Vegan or Jain) or a food allergy (like Peanuts or Dairy), **unverified dishes are automatically left out** of all Table Split food proposals.
3. **If a specific ingredient or allergen is marked `UNKNOWN`:**
   - Any diner whose restriction relies on that ingredient will **not** be offered that dish. We never guess from dish names or descriptions.
4. **No AI Guessing:** Food safety filters are strictly locked to fixed kitchen-verified rules. The AI chat assistant cannot override, relax, or invent dietary or allergen claims.

---

## 2. Dietary Rules Explained in Plain Language

### Rule A — Vegetarian (`vegetarian` / `veg`)
A dish is only served to a vegetarian diner if:
- Kitchen staff confirmed it **is vegetarian** (`YES`).
- Kitchen staff confirmed it **does not contain meat, poultry, fish, or seafood** (`NO`).
- **Egg Rule (Default ON):** Under standard Indian food labelling practice, dishes containing **egg or egg ingredients** are treated as **Non-Vegetarian** and **excluded** for vegetarians. *(Our software has an optional setting only if a customer explicitly enables ovo-vegetarian mode for their own personal preference.)*

### Rule B — Vegan (`vegan`)
A dish is only served to a vegan diner if kitchen staff confirmed ALL of the following:
- Marked **Vegan (`YES`)**.
- Strictly **Vegetarian (`YES`)** and **Non-Veg (`NO`)**.
- Contains **no dairy** (`contains_dairy = NO`) and **no milk ingredients** (`allergen_milk = NO`).
- Contains **no eggs** (`diet_egg = NO` and `allergen_eggs = NO`).
- Contains **no honey** (`contains_honey = NO`) — honey-glazed or honey-sweetened dishes are rejected for vegans.

### Rule C — Jain (`jain`)
A dish is only served to a Jain diner if kitchen staff confirmed ALL of the following:
- Marked **Jain-compliant (`YES`)**.
- Strictly **Vegetarian (`YES`)**, **Non-Veg (`NO`)**, and **Egg-free (`NO`)**.
- Contains **no onion and no garlic** (`contains_onion_garlic = NO`).
- Contains **no root vegetables** (`contains_root_veg = NO` — e.g., potato, carrot, radish, ginger, beetroot, or yam).

### Rule D — Halal (`halal`)
- A dish is only served to a Halal diner if kitchen staff explicitly confirmed **Halal (`YES`)**.
- Dishes with **Halal = `UNKNOWN`** (for example, unverified poultry or unclear alcohol/flavoring extracts) or **`NO`** are **rejected**.

### Rule E — Gluten-Free (`gluten_free`)
- A dish is only served to a gluten-free diner if kitchen staff confirmed **Gluten allergen (`allergen_cereals_gluten`) is `NO`** (wheat, maida, atta, suji, barley, rye, oats, spelt) and it is **not marked as containing gluten** (`diet_gluten_free != NO`).
- If gluten allergen status is **`UNKNOWN`**, the dish is **rejected**.

---

## 3. Food Allergen Exclusions

When a diner lists an allergy, **any dish where that allergen is `YES` or `UNKNOWN` is strictly blocked**. We track the following categories separately so one safe category never hides another:

1. **Cereals containing gluten** (wheat/maida/suji, rye, barley, oats, spelt)
2. **Crustaceans** (prawns, crab, lobster, shrimp)
3. **Milk & Dairy** (milk, paneer, butter, ghee, curd/dahi, khoya, cream, cheese — if *Milk* is `YES`, *Dairy* is automatically forced to `YES`)
4. **Eggs**
5. **Fish**
6. **Peanuts** *(tracked completely separately from tree nuts)*
7. **Tree Nuts** (cashew, almond, pistachio, walnut, hazelnut, etc.)
8. **Soybeans** (soya sauce, tofu, soya oil/chunks)
9. **Sulphites** (concentrated $\ge 10\text{ mg/kg}$ in dried fruits, wines, vinegars, or preservatives — *exact threshold subject to human auditor verification*)
10. **Sesame** *(extra non-statutory category tracked for tahini, til, hummus, and burger buns)*
11. **Mustard** *(extra non-statutory category tracked for mustard seeds, kasundi, and tadka oils)*

---

## 4. Menu Symbols Displayed to Customers

In our Customer Menu screen, SpiceRoute Kitchen uses **only** standard Indian vegetarian / non-vegetarian visual symbols — we never invent extra icons:
- **Veg Symbol (Green Circle inside a Green Square):** Shown **only** when a dish has been verified by kitchen staff as **Vegetarian (`YES`)**, **Non-Veg (`NO`)**, and **Egg-Free (`NO`)**.
- **Non-Veg Symbol (Brown Triangle inside a Brown Square):** Shown for any meat, fish, crustacean dish **AND any egg-containing dish** (per the Egg Rule).
- **Unverified / Incomplete Dishes:** No green vegetarian dot is shown. Instead, the dish card clearly warns **"Dietary info not verified"**, and lists all declared allergens alongside kitchen cross-contact notes.

---

## 5. Staff Verification & Accountability Rules

1. Every time a chef or QA manager verifies a dish via the **Staff Dietary Admin portal** or **CLI tool**, the system records their **Staff Name (`verified_by`)** and the exact **date/time (`verified_at`)**.
2. Kitchen managers can open `/table-split/dietary/report` or the Admin portal at any time to see:
   - What **percentage** of menu items are 100% verified.
   - Which unverified items are blocking Table Split recommendations, ordered with the **highest-selling dishes first** so staff verify popular items first.

---

## 6. Owner / Executive Chef Sign-Off

I have read, understood, and approved the dietary implication rules, egg-as-non-veg display rule, fail-safe UNKNOWN exclusions, and verification workflow described above for SpiceRoute Kitchen.

| Field | Entry |
|---|---|
| **Restaurant Owner / Authorized Signatory** | `________________________________________` *(To be signed by owner)* |
| **Executive Chef / Food Safety Lead** | `________________________________________` *(To be signed by chef)* |
| **Date** | `____ / ____ / 2026` |
| **Notes / Custom Exceptions** | `________________________________________` |

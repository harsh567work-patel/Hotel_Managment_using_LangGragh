# SpiceRoute Kitchen — How the Table Split Recommendation Engine Works (Owner's Guide)

> **Who this guide is for:** Restaurant owners, kitchen leads, and floor managers. No technical or math background is needed.
>
> **Golden Rule:** The Table Split recommender uses **zero AI guessing** when choosing dishes or calculating bills. Every decision follows fixed kitchen rules, verified allergen cards, and exact paise arithmetic.

---

## 1. What the Engine Takes In

When a table finishes sharing their preferences, the recommendation engine looks at:
1. **Your live menu** (prices, GST tax rate, packaging fees, how many people each dish `serves`, whether it is shareable, spice rating, availability, and staff dietary/allergen verification).
2. **Confirmed diner preferences only** (unconfirmed chat drafts never affect an order).
   - **Hard rules (Non-Negotiable):** Dietary rules (*Vegan, Vegetarian, Jain, Halal, Gluten-Free*), food allergies (*Dairy, Peanuts, Tree Nuts, Gluten, Eggs, etc.*), strict personal spending limits (*"Max ₹250"*), and any strict spice or item rule marked *Hard*.
   - **Soft preferences (Wishes, rated weight 1 to 5):** Desired spice level, favorite cuisine style, categories or dishes someone wants included (*"Must include Pizza, weight 5"*), and dishes someone dislikes.
   - **Opt-out option:** If a diner explicitly marks *"I don't need a personal meal"*, the engine will not force food onto their bill.

---

## 2. Step 1: Safety & Eligibility Filter ("Unknown Means Unsafe")

Before looking at prices or cravings, the engine builds a safe menu list **for each individual diner**:
- Any dish marked **unavailable** in the kitchen is excluded for everyone.
- If a diner has *any* dietary rule or allergy, **unverified dishes** or dishes with **`UNKNOWN`** status for that ingredient are automatically excluded for that diner.
- A dish can only be **SHARED** among diners for whom that dish is **100% safe** — and at least **2 people** must share a shared line (otherwise it is ordered as a **PERSONAL** dish for 1 diner).
  - *Example:* If one guest at the table is Vegan (no dairy), they are **never** listed as a sharer on Tandoori Paneer Pizza or Garlic Bread. The remaining non-vegan guests can still share the pizza together!
- Every diner (unless they opted out) must receive at least **one valid meal** — drinks alone (like Mango Lassi or Lime Soda) do not count as a dinner meal.

---

## 3. Step 2: Objective Ordering (What the Engine Prioritizes, in Exact Order)

When choosing the best combination of shared and personal dishes, the engine always follows this strict **5-level priority ladder**. A lower goal is **never** allowed to hurt a higher goal:

### Priority 1 — Zero Hard-Constraint Violations (Safety & Budgets First)
- Every single diner's dietary rules and allergies must be 100% respected.
- Every diner's total bill share — **food share + GST + packaging share + delivery fee share** — must stay **at or below** their hard budget cap.
- If this cannot be achieved, the engine stops and refuses to produce an unsafe or over-budget order (see **No-Solution Behaviour** below).

### Priority 2 — Protect the Least-Happy Guest ("Least Misery")
- Next, the engine looks at the **lowest satisfaction score** among all diners at the table (on a 0% to 100% scale) and pushes that lowest score as high as possible.
- *Why?* It is better for all 4 guests to be 85% happy than for 3 guests to be 100% happy while 1 guest gets nothing they wanted.
- Soft preferences with higher weight (like a **weight-5** *"Must include Pizza"*) carry 5× the influence of a weight-1 preference. Missing dish information (such as an unrated spice level) is treated as a neutral **50% (0.5)** — it never gets an unfair bonus over a confirmed match. Disliked dishes act as a strong penalty.

### Priority 3 — Raise the Table's Average Happiness
- Once the unhappiest guest is as satisfied as possible, the engine maximizes the **average satisfaction across the whole table**.

### Priority 4 — Keep Everyone's Bill Fair & Balanced ("Minimum Cost Spread")
- Among equally satisfying orders, the engine minimizes the **gap between the highest-paying diner and the lowest-paying diner**.
- This naturally encourages guests with similar tastes to split shareable dishes rather than forcing one person onto an expensive solo plate while another orders a cheap side.

### Priority 5 — Minimize Food Waste & Portion Mismatches (`serves`)
- Finally, the engine checks each dish's **`serves`** count (for example, `serves = 2`):
  - **Unused servings (waste):** Ordering a platter that feeds 4 for only 2 people leaves 2 wasted servings.
  - **Under-serving:** Splitting a 1-person dish among 3 people leaves diners short.
  - Shared lines stay at quantity `1` and match group sizes closely to `serves`.

---

## 4. Tie-Breaks & Consistency (Same Table = Same Order)

If two different dish combinations achieve the **exact same** safety, minimum satisfaction, average satisfaction, bill spread, and waste score:
- The engine breaks ties deterministically by **menu item ID** in ascending alphabetical/numeric order (for example, `D004` before `D009`).
- **Order independence:** Who joined the table first, what order guests are listed in, or how many chat messages were sent in the session **never** alters the recommended dishes or anyone's bill split.
- **Exact money conservation:** Odd paise remainders when dividing bills (such as ₹100 split 3 ways: ₹33.34, ₹33.33, ₹33.33) are assigned consistently by sorted participant ID so the sum of everyone's shares always equals the exact restaurant total down to the single paise.

---

## 5. Handling Strong "Must-Include" Wishes That Clash With Hard Rules

If a guest adds a strong soft request (such as **weight-5 Must Include Pizza** or a specific dish), the engine tries its hardest to include it.

However, if including that item is impossible without breaking someone's **hard rule** (for instance, a vegan guest asked for Pizza when every pizza on the menu has cheese, or a guest with a ₹250 budget wants a ₹420 pizza and nobody else can split it):
1. The engine **still builds a safe, within-budget order** without breaking the hard rule.
2. It attaches a structured explanation showing **which guest's rule blocked the request** (e.g. dairy allergy or ₹250 budget limit).
3. It automatically looks up and lists **safe, affordable alternatives from the same menu category** (if any exist on your menu).

---

## 6. No-Solution Behaviour (When Rules Truly Clash)

Sometimes, a group's hard rules make *any* valid order impossible — for example:
- A guest has a **₹100 hard budget**, and the cheapest safe vegan dish on the menu is **₹180**.
- A guest requires **Vegan + Gluten-Free**, and the only vegan dishes on the menu are either unverified or contain gluten.
- Someone set a **Hard "No Spice"** limit alongside a **Hard "Must Include Ghost Pepper Pizza"** rule.

When that happens, the engine returns **`NO_SOLUTION`** (never an unsafe or over-budget workaround) and does two helpful things automatically:

1. **Finds the Minimal Set of Clashing Rules ("Minimal Unsatisfiable Core"):**
   - First, it temporarily sets aside all optional soft wishes (spice preferences, favorites, dislikes) so harmless preferences don't clutter the explanation.
   - Next, it tests hard rules one by one to isolate **only the smallest group of rules that actually clash** (for example: *"Amit's Vegan rule + Amit's ₹100 budget"* — leaving out Priya's peanut allergy if peanuts weren't part of the problem).

2. **Suggests Up to 3 Ranked, Verified Ways to Fix It ("Relaxation Options"):**
   - Every suggestion is tested against your menu to confirm it actually unlocks a valid order:
     - **Raise a tight budget by an exact rupee amount** (e.g., *"Raise Amit's budget by ₹40 to ₹140"*).
     - **Drop or soften a hard non-safety rule** (e.g., *"Treat Rohan's spice limit as a soft preference"* or *"Let Rohan's pizza be optional / personal"*).
     - **Verify unverified dishes** if missing kitchen allergen sign-offs are what blocked a guest.
     - **Opt a diner out of a required meal** if they only want to join the table without ordering a full dish.

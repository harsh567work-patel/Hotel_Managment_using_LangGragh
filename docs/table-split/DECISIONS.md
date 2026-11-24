# Table Split Mode — Architecture Decision Records (ADRs) & Open Questions

This document records the foundational architectural decisions for Table Split Mode in SpiceRoute Kitchen.

---

## ADR-01: Persistence Strategy — SQLite with WAL Mode vs. Postgres vs. Redis

### Context
Table Split Mode manages multi-person synchronized ordering sessions with diners joining, editing preferences, reviewing itemised splits, and completing payments. Sessions must survive server reboots, application crashes, and hot-reloads. In-memory storage (`_sessions = {}`) violates the persistence mandate.

### Evaluated Options
1. **Redis**: In-memory key-value with snapshot/AOF. High concurrency, but adds an external daemon dependency not currently running in the local/render environment. Complex queries over nested session diner states require manual indexing.
2. **PostgreSQL**: Production-grade relational store with rich JSONB and row locking. However, requires external database provisioning and connection pooling setup that would complicate local zero-setup execution.
3. **SQLite with WAL Mode (`sqlite3`)**:
   - Built into Python standard library (`3.13`), requiring zero external daemons.
   - Write-Ahead Logging (`PRAGMA journal_mode=WAL`) enables concurrent reader connections without blocking writers.
   - ACID transactional guarantees for atomic state changes.
   - JSON columns allow storing nested domain structures with structured indexing on `session_id`, `room_code`, `status`, and `join_token_hash`.
   - File-backed at `data/table_split.db` with automated migrations.

### Decision
**Adopt SQLite with WAL Mode and foreign keys enabled as the default persistent store**, abstracted behind a repository interface (`TableSessionRepository`). If SpiceRoute Kitchen scales horizontally to multiple web workers across servers in the future, the repository interface can be swapped to PostgreSQL/SQLAlchemy without altering business logic.

---

## ADR-02: Realtime Multi-User Approach for Streamlit Frontend

### Context
Streamlit operates on an execution model where user interactions trigger a top-to-bottom script rerun in an isolated browser session. When Diner B joins a table on their phone, Diner A’s browser does not receive an automatic WebSocket push notification from Streamlit core.

### Evaluated Options
1. **External WebSocket Server**: Run a standalone WebSocket server (e.g. FastAPI WebSockets) and custom frontend JavaScript / React component. Violates the no-new-framework rule and increases surface area.
2. **Streamlit Native Polling Fragment (`@st.fragment(run_every=2)`)**:
   - Introduced in Streamlit 1.33+ (current version is 1.40+ / 1.67).
   - Allows specific UI sub-sections (e.g., Table Waiting Room, Live Bill Summary, Payment Progress) to rerun independently at a set interval (e.g., 2 seconds) without rerunning the whole page or resetting user input fields.
   - Fetches the latest session snapshot from the database or FastAPI endpoint.
   - Backed by `session_version: int`: the fragment checks if `db_version > local_version`; if unchanged, it skips DOM updates.

### Decision
**Adopt Streamlit `@st.fragment(run_every=2)` lightweight polling backed by version checking (`session_version`).** This provides seamless, real-time-like coordination across diners' mobile devices with zero new frontend frameworks.

---

## ADR-03: Session Token & Authentication Scheme

### Context
Sessions must be shareable among friends while preventing unauthorized hijacking, eavesdropping on private preferences, or unauthorized order modifications.

### Decision & Dual-Token Scheme
1. **Primary Join Link (Direct Access)**:
   - Contains a cryptographically secure 128-bit random token (`join_token_128`), generated via `secrets.token_urlsafe(16)` (22 URL-safe characters, 128 bits of entropy).
   - Allows instant entry into the room without manual room code typing.
2. **Guest Token (Diner Identity & Authorization)**:
   - When a diner joins, they receive a unique `guest_token` (`secrets.token_hex(24)`).
   - The plain token is returned to the client once.
   - In the database, only the SHA-256 hash (`guest_token_hash`) is stored.
   - Every mutation request must supply `X-Guest-Token`, which is hashed and validated against `guest_token_hash`.
3. **Secondary Short Room Code (`TAB-XXXX`)**:
   - Short 4-character uppercase alphanumeric code (e.g., `TAB-8K2M`) generated with `secrets.choice` excluding ambiguous characters (`0, O, 1, I, L`).
   - Reconciled format: Prefix `TAB-` plus 4 characters = 8 characters total (`ROOM_CODE_SUFFIX_LEN = 4`).
   - Rate-limited to prevent brute-force attacks (max 5 failed attempts per IP per 5 minutes).
   - Diners joining via short code enter a `PENDING_APPROVAL` state requiring host approval or passkey confirmation.

---

## ADR-04: Dietary Rules & Elimination of "NON_VEG" as a Safety Rule

### Context
In Indian restaurant menus and food safety guidelines, "Non-Vegetarian" indicates the presence of meat/poultry/seafood, while "Vegetarian" is an exclusion constraint (excludes meat, fish, eggs). A diner stating "I am non-veg" is expressing that they *eat* meat, not that they are medically prohibited from eating vegetarian dishes or lentils!

### Decision
1. **Dietary Rules are Exclusions (Filters)**:
   - `DietRule.VEGETARIAN`: Must not contain meat, poultry, fish, seafood.
   - `DietRule.VEGAN`: Must not contain any animal products (no meat, dairy, honey, eggs).
   - `DietRule.JAIN`: Must not contain meat, fish, eggs, or underground root vegetables (onions, garlic, potatoes).
   - `DietRule.HALAL`: Meat must be Halal-certified; strictly excludes pork and alcohol.
   - `DietRule.KOSHER`: Strictly excludes non-kosher preparations.
2. **Elimination of `NON_VEG` from Diet Rules**:
   - `NON_VEG` is removed as a safety restriction.
   - An omnivore diner simply has an empty `diet_rules` set (`set()`), meaning all dishes (veg and non-veg alike) are permissible.
   - Desiring meat is treated as a positive preference (`must_have_dish_ids` or category preference), never an allergen/dietary safety filter.

---

## ADR-05: Tri-State Ingredient & Allergen Profiling

### Context
Inferring allergens or dietary compliance from dish titles or unstructured tags creates dangerous false-negatives (e.g., assuming "Dal Tadka" has no dairy, when it may be tempered in ghee/butter).

### Decision
- Every dish has explicit, per-allergen and per-diet flags using a strict Tri-State enum:
  - `TriState.CONTAINS`: Positively verified to contain the allergen/ingredient.
  - `TriState.FREE`: Positively certified to be free of the allergen/ingredient.
  - `TriState.UNKNOWN`: Unverified or ambiguous.
- **Fail-Safe Default**: All fields default to `TriState.UNKNOWN`.
- **Hard Safety Invariant**: If a diner has an allergy or diet rule, any dish where that property is `UNKNOWN` or `CONTAINS` is **immediately rejected as UNSAFE**. Only `FREE` is permitted.
- Auditing: Dish safety records require `verified_by: Optional[str]` and `verified_at: Optional[str]`.

---

## ADR-06: Strict Integer Currency (Paise)

### Context
Floats introduce rounding noise (e.g. `₹280.00` represented as `280.00000000000006`). In bill splitting, fractional paise accumulate, producing discrepancies between item sums and individual payments.

### Decision
- **Integer Paise Everywhere**: 1 Rupee = 100 paise (e.g. ₹280 = `28000` paise).
- Strict validation rejects `float` and `bool` at the schema level.
- Formatters produce `₹X.XX` strictly for UI display.

---

## ADR-07: Shared Dish Policy & Upgrade Path

### Context
When friends share food, how are shared items selected and split?

### Decision for v1
- **Safe-for-All Invariant**: For v1, every dish categorized as a "Shared Item" must be 100% safe (allergens + diets) for **all** diners participating in that shared round.
- Cost of a shared item is split equally among all sharing diners (`price_paise // N`, with remainder paise distributed deterministically).
- **Sharer-Subset Upgrade Path (v2)**:
  In v2, diners will be able to form "sharing sub-groups" (e.g. 3 carnivores share Chicken Tikka, while 2 vegetarians share Paneer Tikka). The v1 data model supports this by tracking a `sharer_diner_ids: List[str]` on each line item, which defaults to `all_diners` in v1.

---

## ADR-08: Tax & Tip Representation

### Context
The existing SpiceRoute Kitchen codebase contains no tax or GST calculation; `total_price` was merely a raw sum.

### Decision
- Add `RESTAURANT_GST_BPS = 500` (5.00% Indian Restaurant GST in basis points, integer math).
- Tax is computed as `(subtotal_paise * RESTAURANT_GST_BPS) // 10000`.
- This is explicitly marked as a **placeholder pending accountant review**.
- Tax is allocated proportionally to diners' subtotals with remainder paise allocated deterministically so `sum(diner_tax) == total_tax`.

---

## Open Questions Ranked by Cost of a Wrong Guess

| Rank | Question | Cost of Wrong Guess | Recommended Default & Rationale |
|---|---|---|---|
| **1** | **How should kitchen cross-contamination be legally and medically declared?** Can SpiceRoute Kitchen guarantee a 100% allergen-free kitchen for severe anaphylactic allergies (e.g. aerosolized nuts/flour)? | **Catastrophic (Fatal / Legal Liability)** | **Default to strict warning banner:** "Our kitchen handles dairy, nuts, and gluten. While recipes are strictly filtered, cross-contact cannot be 100% eliminated. Diners with severe anaphylaxis must be notified before ordering." |
| **2** | **What happens if one diner refuses to pay or leaves the table mid-payment?** Should the host absorb the unpaid balance, should the order cancel, or can remaining diners vote to re-split? | **High (Financial / Customer Conflict)** | **Default:** Session remains in `PAYMENT_PENDING` with a 15-minute countdown. The host has the exclusive privilege to either: (a) re-allocate the unpaid diner's share across remaining diners, or (b) cancel the session. Kitchen order is NEVER placed until 100% of paise are authorized. |
| **3** | **Are must-have dishes considered personal or shared by default?** If Diner A requests "Butter Chicken" as a must-have, does Diner A pay 100% of it, or is it placed into the shared pool? | **Medium (UX / Budget Distortion)** | **Default:** If a must-have dish is safe for all diners and fits the shared category (Curries, Starters, Breads), it is proposed as **Shared** with opt-out; if unsafe for even one diner or classified as a Single Portion (Drinks, Desserts), it is strictly **Personal** to the requesting diner. |
| **4** | **How should tips/service charges be split?** Are tips supported, and are they discretionary or flat? | **Low (Financial)** | **Default:** Zero mandatory service charge in v1. Optional tip can be added by individual diners to their own personal split at payment time. |
| **5** | **What is the exact expiration window for an inactive session?** | **Low (Resource cleanup)** | **Default:** 180 minutes (3 hours), sufficient for a lengthy dinner service. Cleaned up via automatic TTL checks. |

---

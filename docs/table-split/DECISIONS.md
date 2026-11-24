# Table Split Mode — Architecture Decisions & Open Questions

This document records explicit architectural decisions with one-line rationales for Table Split Mode in SpiceRoute Kitchen.

---

## 1. Explicit Architectural Choices (With One-Line Rationales)

| Decision Area | Chosen Approach | One-Line Rationale |
|---|---|---|
| **Realtime Transport** | **Server-Sent Events (SSE) + HTTP POST** (with Streamlit `@st.fragment` polling fallback) | Standard HTTP/1.1 & HTTP/2 friendly, bypasses corporate/mobile firewall WebSocket blocks, and avoids introducing new WebSocket dependencies into an HTTP-based FastAPI codebase. |
| **Session ID & Token Scheme** | **Dual-Token Scheme** (128-bit URL-safe `join_token_128`, SHA-256 hashed `guest_token`, secondary `TAB-XXXX` room code) | Enables instant friction-free entry via link/QR while preventing room enumeration attacks and securing guest identity against token leakage. |
| **Solver Approach** | **Deterministic Greedy Bipartite Partitioning** (hard-safety filter $\to$ shared intersection $\to$ personal add-ons $\to$ budget bounds) | Solves safe multi-diner dish allocation in under 15 milliseconds without external MILP solver overhead or non-deterministic LLM hallucinations. |
| **Payment Model** | **Split-Authorized Escrow Model** with client-generated UUID `Idempotency-Key` headers | Ensures each diner pays their own exact share independently while holding kitchen order placement until 100% of order paise are authorized. |
| **LLM Provider Abstraction** | **LangChain `ChatGroq` with strict `with_structured_output(DinerPreference)`** behind feature flag `AI_PARSER_ENABLED` | Reuses existing Groq configuration in [config.py](file:///d:/langGRAPH/config.py) while strictly isolating the LLM to parsing free text with zero access to safety, menu selection, or money calculation. |
| **Persistence Engine** | **SQLite with Write-Ahead Logging (`PRAGMA journal_mode=WAL`)** at [data/table_split.db](file:///d:/langGRAPH/data/table_split.db) | Provides zero-daemon, zero-cost, persistent ACID transactions that survive process restarts while enabling high-throughput concurrent reads. |
| **Money Representation** | **Strict Integer Indian Paise (`StrictInt`)** | Eliminates floating-point rounding drift, ensuring $\sum \text{diner\_splits} \equiv \text{order\_total}$ down to the exact single paise. |
| **Shared Dish Policy** | **Safe-for-All for v1** (with `sharer_diner_ids` subset upgrade path in v2) | Guarantees zero cross-contamination risk at the table for v1 while laying the structural data foundation for subgroup sharing later. |
| **Tax Representation** | **`RESTAURANT_GST_BPS = 500` (5.00% GST in basis points)** | Reusable integer basis points placeholder pending formal chartered accountant verification. |

---

## 2. Open Questions Ranked by Cost of a Wrong Guess

The following questions require stakeholder alignment before production deployment, ranked strictly by the cost of making an incorrect assumption:

| Rank | Question | Cost of Wrong Guess | Recommended Default & Rationale |
|---|---|---|---|
| **1** | **What is SpiceRoute Kitchen's legal policy on airborne/cross-contact kitchen allergens?** Can the kitchen guarantee zero cross-contact for severe airborne nut or flour allergies, or must all orders carry a disclaimer? | **Critical (Catastrophic / Medical Liability)** | **Recommended Default:** Mandate a prominent modal disclaimer: *"SpiceRoute Kitchen prepares dishes in a shared facility. While our software filters ingredients with 100% strictness, diners with severe airborne anaphylaxis must inform restaurant staff directly."* |
| **2** | **What happens if one diner's payment fails or they leave the table mid-payment?** Should the entire order cancel, should the host be forced to cover the remainder, or can remaining diners vote to re-split? | **High (Financial / Customer Conflict)** | **Recommended Default:** The session enters a 15-minute `PAYMENT_PENDING` grace period. The host possesses exclusive admin capability to: (a) re-allocate the unpaid share proportionally among remaining diners, (b) pay on the diner's behalf, or (c) release the lock back to draft. The kitchen order is **NEVER** placed until 100% of paise are collected. |
| **3** | **How should must-have dishes be classified when they conflict with group preferences?** If Diner A requests Butter Chicken (contains Dairy/Chicken) as a must-have, but Diner B is Vegan, does Butter Chicken automatically become a Personal item for Diner A? | **Medium (UX / Budget Distortion)** | **Recommended Default:** Any must-have dish that is not 100% safe for all diners is automatically downgraded to a **Personal Item** billed 100% to the requesting diner, rather than failing the whole group order. |
| **4** | **What is the default policy for discretionary tips and service charge?** Can individual diners add a tip to their own split, or is tipping a group-wide flat percentage? | **Medium (Financial)** | **Recommended Default:** Zero mandatory service charge. Diners may add an optional tip to their individual bill split at payment execution time. |
| **5** | **What is the exact inactivity timeout for table sessions?** | **Low (Resource Cleanup)** | **Recommended Default:** 180 minutes (3 hours). Diners can take up to 3 hours from table creation to payment before the session transitions to `EXPIRED`. |

---

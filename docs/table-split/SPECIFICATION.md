# Table Split Mode — System Specification & 12-Stage Roadmap

This specification establishes the system invariants, data dictionary, and multi-stage delivery roadmap for Table Split Mode in SpiceRoute Kitchen.

---

## 1. System Invariants (Non-Negotiable)

1. **Integer Currency Invariant**: All monetary values are strictly integers representing Indian paise (`₹1 = 100 paise`). Floats and booleans are disallowed.
2. **Deterministic Safety Invariant**: Allergen and dietary exclusion rules are 100% deterministic code in [table_split/safety.py](file:///d:/langGRAPH/table_split/safety.py). LLMs are prohibited from safety classifications.
3. **Fail-Safe Invariant**: Any dish property whose allergen or dietary status is `UNKNOWN` fails any related hard restriction.
4. **Conservation of Money**: For any order split:
   $$\sum_{i=1}^{N} \text{diner\_total\_paise}_i \equiv \text{order\_total\_paise}$$
   Zero fractional paise are discarded or created.
5. **Optimistic Concurrency**: Every session carries a monotonically increasing `version: int`. Writes with mismatched versions return `HTTP 409 Conflict`.
6. **Immutable Audit Trail**: Every state transition generates an `AuditEvent` with timestamp, session ID, acting diner ID, action name, and payload hash.
7. **Kitchen Isolation Until Paid**: A table order is NEVER dispatched to the kitchen queue in [graph/orders.py](file:///d:/langGRAPH/graph/orders.py) until 100% of order paise have been authorized and collected.

---

## 2. Confirmed 12-Stage Delivery Roadmap

| Stage | Name | Key Deliverables & Target Modules | Verification & Quality Gate |
|---|---|---|---|
| **Stage 1** | **Discovery & Architecture** *(Current)* | Read-only audit of [api.py](file:///d:/langGRAPH/api.py), [app.py](file:///d:/langGRAPH/app.py), [graph/](file:///d:/langGRAPH/graph/); authoring [ARCHITECTURE.md](file:///d:/langGRAPH/docs/table-split/ARCHITECTURE.md), [RISKS.md](file:///d:/langGRAPH/docs/table-split/RISKS.md), [DECISIONS.md](file:///d:/langGRAPH/docs/table-split/DECISIONS.md), [SPECIFICATION.md](file:///d:/langGRAPH/docs/table-split/SPECIFICATION.md). | Clean `git status` proof; zero source files modified. |
| **Stage 2** | **Safety Substrate & Domain Models** | Strict integer paise models, tri-state allergen/diet enums, [table_split/models.py](file:///d:/langGRAPH/table_split/models.py), [table_split/enums.py](file:///d:/langGRAPH/table_split/enums.py), [table_split/safety.py](file:///d:/langGRAPH/table_split/safety.py), and [table_split/menu_catalog.py](file:///d:/langGRAPH/table_split/menu_catalog.py). | $\ge 60$ table-driven safety tests, float/bool rejection test, monotonicity property test. |
| **Stage 3** | **Persistence & Concurrency** | SQLite WAL persistence substrate in [table_split/db/persistence.py](file:///d:/langGRAPH/table_split/db/persistence.py), `version: int` optimistic locking, and audit ledger. | Concurrency collision test (`SessionConcurrencyError`), TTL expiry test. |
| **Stage 4** | **Session Lifecycle & Auth API** | FastAPI router `table_split/api_router.py` mounted in [api.py](file:///d:/langGRAPH/api.py); guest token authentication, join tokens, room codes, rate limiting. | API integration tests for `/sessions`, `/join`, `/preferences` with 401/409/429 checks. |
| **Stage 5** | **Real-Time Notification Hub** | Server-Sent Events (SSE) stream (`GET /sessions/{id}/events`) with Streamlit fragment polling fallback. | Multi-client event broadcast and reconnection tests. |
| **Stage 6** | **Deterministic Solver & Recommender** | `table_split/solver.py`: Bipartite partitioning (shared vs. personal), budget capping, must-have satisfaction. | Solver unit tests with tight budgets and conflicting diets (e.g. Jain + Halal). |
| **Stage 7** | **AI Free-Text Preference Parser** | `table_split/ai_parser.py` using Groq with `with_structured_output(DinerPreference)` behind flag `AI_PARSER_ENABLED`. | Injection resistance tests, prompt fuzzing, and deterministic fallback tests. |
| **Stage 8** | **Pricing & Zero-Penny Split Engine** | `table_split/pricing.py`: Integer paise bill splitting, 5% GST computation with remainder allocation. | Mathematical proof tests: sum of diner splits $\equiv$ total paise across odd splits. |
| **Stage 9** | **Payment Orchestration & Webhooks** | `table_split/payment.py`: Idempotent payments (`Idempotency-Key`), session lock, webhook deduplication. | Double-payment rejection test, webhook duplicate replay test, partial payment recovery test. |
| **Stage 10** | **Kitchen Integration Bridge** | Bridge from Table Split into [graph/orders.py](file:///d:/langGRAPH/graph/orders.py) and [graph/nodes/place_order.py](file:///d:/langGRAPH/graph/nodes/place_order.py) upon 100% payment completion. | End-to-end bridge test: payment complete $\to$ `ORD-XXXXXXXX` created in kitchen queue. |
| **Stage 11** | **Mobile Streamlit UI** | [pages/1_Table_Split.py](file:///d:/langGRAPH/pages/1_Table_Split.py): Mobile-responsive UI at 360px viewport; join flow, preference sliders, waiting room, split review. | Browser inspection test at 360px, 768px, 1440px viewports; keyboard accessibility. |
| **Stage 12** | **End-to-End Hardening & Security Audit** | Comprehensive regression, multi-user concurrency stress tests, WCAG AA contrast check, final documentation. | Full test suite execution, zero lint/mypy errors, staging readiness sign-off. |

---

## 3. Data Dictionary Reference

- **`TriState`**: `CONTAINS` (dish contains attribute), `FREE` (dish is free of attribute), `UNKNOWN` (unverified; fails hard checks).
- **`Allergen`**: `DAIRY`, `TREE_NUTS`, `PEANUTS`, `GLUTEN`, `SOY`, `SHELLFISH`, `FISH`, `EGG`, `SESAME`, `MUSTARD`.
- **`DietRule`**: `VEGETARIAN`, `VEGAN`, `JAIN`, `HALAL`, `KOSHER` (omnivore is represented by empty set `set()`).
- **`SpiceLevel`**: `NONE` (0), `MILD` (1), `MEDIUM` (2), `SPICY` (3), `EXTRA_SPICY` (4).
- **`PreferenceStrength`**: `HARD` (strict filter), `SOFT` (negotiable preference).
- **`SessionStatus`**: `OPEN_FOR_PREFERENCES`, `PROPOSED`, `LOCKED`, `PAYMENT_PENDING`, `COMPLETED`, `CANCELLED`, `EXPIRED`.
- **`PaymentStatus`**: `UNPAID`, `INITIATED`, `AUTHORIZED`, `PAID`, `FAILED`, `REFUNDED`.

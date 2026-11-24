# Table Split Mode — Technical Specification & Data Dictionary

## 1. System Invariants (Non-Negotiable)

1. **Integer Currency Invariant**: All monetary values are strictly integers representing Indian paise (`₹1 = 100 paise`). Floats and booleans are disallowed.
2. **Deterministic Safety Invariant**: Allergen and dietary exclusion rules are 100% deterministic code. LLMs are prohibited from safety classifications.
3. **Fail-Safe Invariant**: Any dish property whose allergen or dietary status is `UNKNOWN` fails any related hard restriction.
4. **Conservation of Money**: For any order split:
   $$\sum_{i=1}^{N} \text{diner\_total\_paise}_i = \text{order\_total\_paise}$$
   No fractional paise are discarded or created.
5. **Optimistic Concurrency**: Every session carries a monotonically increasing `version: int`. Writes with mismatched versions are rejected.
6. **Immutable Audit Trail**: Every state transition generates an `AuditEvent` with timestamp, session ID, acting diner ID, action name, and payload hash.

---

## 2. Data Dictionary

### Core Enums
- `TriState`: `CONTAINS`, `FREE`, `UNKNOWN`
- `Allergen`: `DAIRY`, `TREE_NUTS`, `PEANUTS`, `GLUTEN`, `SOY`, `SHELLFISH`, `FISH`, `EGG`, `SESAME`, `MUSTARD`
- `DietRule`: `VEGETARIAN`, `VEGAN`, `JAIN`, `HALAL`, `KOSHER`
- `SpiceLevel`: `NONE` (0), `MILD` (1), `MEDIUM` (2), `SPICY` (3), `EXTRA_SPICY` (4)
- `PreferenceStrength`: `HARD`, `SOFT`
- `SessionStatus`: `OPEN_FOR_PREFERENCES`, `PROPOSED`, `LOCKED`, `PAYMENT_PENDING`, `COMPLETED`, `CANCELLED`, `EXPIRED`
- `PaymentStatus`: `UNPAID`, `INITIATED`, `AUTHORIZED`, `PAID`, `FAILED`, `REFUNDED`

---

# SpiceRoute Kitchen — Project Rules

## Test Execution
- Always run tests with `python -m pytest` (never bare `pytest`) so the repository root is on `sys.path`.

## Table Split Safety & Database Invariants
- Keep all dietary/allergen safety filtering strictly deterministic (`table_split/safety.py`, `table_split/dietary.py`). LLMs may only parse free text into validated schemas and never decide dish safety or pricing.
- Keep all monetary amounts in strict integer Indian paise (`int`, rejecting `bool` and `float`).
- When ordering SQLite rows with integer-second timestamps (`joined_at`, `created_at`) where ties can occur in the same second, break ties deterministically with `rowid ASC` rather than UUID strings.

## Streamlit UI Rendering
- Inside `st.markdown(..., unsafe_allow_html=True)`, never put 4 or more leading spaces before HTML tags or `<style>` blocks so CommonMark does not render them as literal code blocks.

## Lint & Type Verification
- Before completing changes under `table_split/`, `scripts/`, or `tests/test_table_split_stage*.py`, verify zero errors with `ruff check` and `mypy --follow-imports=silent --ignore-missing-imports`.

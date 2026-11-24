"""
table_split — Multi-person collaborative ordering engine for SpiceRoute Kitchen.
"""

from table_split.db.persistence import SessionConcurrencyError, TableSessionRepository
from table_split.enums import (
    Allergen,
    DietRule,
    PaymentStatus,
    PreferenceStrength,
    SessionStatus,
    SpiceLevel,
    TriState,
)
from table_split.menu_catalog import load_menu_catalog
from table_split.models import (
    AuditEvent,
    Diner,
    DinerPreference,
    DishSafetyProfile,
    DishVerification,
    MenuItem,
    OrderLineItem,
    ProposedOrder,
    TableSession,
)
from table_split.safety import (
    filter_safe_menu_for_diner,
    find_safe_dishes_for_group,
    find_safe_shared_dishes,
    is_dish_safe_for_diner,
)
from table_split.security import (
    generate_guest_token,
    generate_join_token_128,
    generate_room_code,
    hash_token,
    verify_token,
)

__all__ = [
    "Allergen",
    "AuditEvent",
    "DietRule",
    "Diner",
    "DinerPreference",
    "DishSafetyProfile",
    "DishVerification",
    "MenuItem",
    "OrderLineItem",
    "PaymentStatus",
    "PreferenceStrength",
    "ProposedOrder",
    "SessionConcurrencyError",
    "SessionStatus",
    "SpiceLevel",
    "TableSession",
    "TableSessionRepository",
    "TriState",
    "filter_safe_menu_for_diner",
    "find_safe_dishes_for_group",
    "find_safe_shared_dishes",
    "generate_guest_token",
    "generate_join_token_128",
    "generate_room_code",
    "hash_token",
    "is_dish_safe_for_diner",
    "load_menu_catalog",
    "verify_token",
]

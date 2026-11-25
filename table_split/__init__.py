"""
table_split — Multi-person collaborative ordering engine for SpiceRoute Kitchen.
"""

from table_split.db.dietary_repo import DietaryRepository
from table_split.db.persistence import SessionConcurrencyError, TableSessionRepository
from table_split.dietary import (
    DietaryCheckResult,
    DietaryItem,
    HardDietaryConstraints,
    classify_fssai_symbol,
    filter_items_for_proposal,
    is_item_allowed,
    isItemAllowed,
)
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
    "DietaryCheckResult",
    "DietaryItem",
    "DietaryRepository",
    "Diner",
    "DinerPreference",
    "DishSafetyProfile",
    "DishVerification",
    "HardDietaryConstraints",
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
    "classify_fssai_symbol",
    "filter_items_for_proposal",
    "filter_safe_menu_for_diner",
    "find_safe_dishes_for_group",
    "find_safe_shared_dishes",
    "generate_guest_token",
    "generate_join_token_128",
    "generate_room_code",
    "hash_token",
    "isItemAllowed",
    "is_dish_safe_for_diner",
    "is_item_allowed",
    "load_menu_catalog",
    "verify_token",
]

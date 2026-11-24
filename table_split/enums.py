"""
table_split.enums — Foundational enums and tri-state indicators for Table Split Mode.
All values are strings for JSON serialization and strict schema adherence.
"""

from enum import Enum


class TriState(str, Enum):
    """
    Tri-state classification for allergens and dietary traits.
    Default must always be UNKNOWN.
    In deterministic safety evaluation, UNKNOWN fails any hard restriction.
    Values: YES, NO, UNKNOWN (with CONTAINS/FREE compatibility).
    """
    YES = "YES"
    NO = "NO"
    UNKNOWN = "UNKNOWN"
    CONTAINS = "CONTAINS"
    FREE = "FREE"


class Allergen(str, Enum):
    """
    Standard culinary allergens tracked for food safety.
    """
    DAIRY = "DAIRY"
    TREE_NUTS = "TREE_NUTS"
    PEANUTS = "PEANUTS"
    GLUTEN = "GLUTEN"
    SOY = "SOY"
    SHELLFISH = "SHELLFISH"
    FISH = "FISH"
    EGG = "EGG"
    SESAME = "SESAME"
    MUSTARD = "MUSTARD"


class DietRule(str, Enum):
    """
    Dietary rules that function as exclusion filters.
    NON_VEG is excluded because omnivores have no dietary restrictions.
    """
    VEGETARIAN = "VEGETARIAN"
    VEGAN = "VEGAN"
    JAIN = "JAIN"
    HALAL = "HALAL"
    KOSHER = "KOSHER"


class SpiceLevel(str, Enum):
    """
    Spice level tolerance and dish rating.
    """
    NONE = "NONE"
    MILD = "MILD"
    MEDIUM = "MEDIUM"
    SPICY = "SPICY"
    EXTRA_SPICY = "EXTRA_SPICY"

    @property
    def rank(self) -> int:
        ranks = {
            SpiceLevel.NONE: 0,
            SpiceLevel.MILD: 1,
            SpiceLevel.MEDIUM: 2,
            SpiceLevel.SPICY: 3,
            SpiceLevel.EXTRA_SPICY: 4,
        }
        return ranks[self]

    def is_acceptable_for(self, max_allowed: "SpiceLevel") -> bool:
        """Returns True if this spice level is <= max_allowed tolerance."""
        return self.rank <= max_allowed.rank


class PreferenceStrength(str, Enum):
    """
    Distinguishes non-negotiable hard safety constraints from negotiable soft preferences.
    """
    HARD = "HARD"
    SOFT = "SOFT"


class SessionStatus(str, Enum):
    """
    Lifecycle state machine for a multi-person dining session.
    """
    OPEN_FOR_PREFERENCES = "OPEN_FOR_PREFERENCES"
    PROPOSED = "PROPOSED"
    LOCKED = "LOCKED"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class PaymentStatus(str, Enum):
    """
    Per-diner payment progress state.
    """
    UNPAID = "UNPAID"
    INITIATED = "INITIATED"
    AUTHORIZED = "AUTHORIZED"
    PAID = "PAID"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"

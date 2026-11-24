"""
table_split.models — Strict domain models for Table Split Mode.
Implements:
- Strict integer paise money (strictly rejecting float and bool)
- Tri-state dish allergen & dietary verification
- Diner preferences with hardness and privacy flags
- Multi-diner session state with versioning and audit logging
"""

from typing import Any

from pydantic import BaseModel, Field, field_validator

from table_split.enums import (
    Allergen,
    DietRule,
    PaymentStatus,
    PreferenceStrength,
    SessionStatus,
    SpiceLevel,
    TriState,
)


def validate_strict_paise(v: Any) -> int:
    """
    Enforces strict integer paise.
    Rejects floats (e.g. 280.0), booleans (True/False), strings, and negative values.
    """
    # In Python, bool is a subclass of int (isinstance(True, int) is True).
    # We must explicitly reject bool.
    if isinstance(v, bool):
        raise TypeError("Currency in paise must be a strict integer, not a boolean.")
    if not isinstance(v, int):
        raise TypeError(f"Currency in paise must be an integer, got {type(v).__name__} ({v!r}).")
    if v < 0:
        raise ValueError(f"Currency in paise cannot be negative, got {v}.")
    return v


class DishVerification(BaseModel):
    """
    Metadata recording culinary verification of ingredients and preparation.
    """
    verified_by: str | None = None
    verified_at: str | None = None
    verification_notes: str | None = None


class DishSafetyProfile(BaseModel):
    """
    Tri-state profile for all allergens and dietary rules.
    Default for every field is strictly UNKNOWN.
    """
    allergens: dict[Allergen, TriState] = Field(
        default_factory=lambda: {a: TriState.UNKNOWN for a in Allergen}
    )
    diets: dict[DietRule, TriState] = Field(
        default_factory=lambda: {d: TriState.UNKNOWN for d in DietRule}
    )
    spice_level: SpiceLevel = SpiceLevel.MILD
    verification: DishVerification = Field(default_factory=DishVerification)

    @property
    def is_verified(self) -> bool:
        """A profile is verified if verified_by is provided."""
        return self.verification.verified_by is not None


class MenuItem(BaseModel):
    """
    Normalized menu dish representation with integer paise pricing and tri-state safety.
    """
    id: str
    dish_name: str
    category: str
    description: str
    price_paise: int = Field(..., description="Item price in integer Indian paise (₹1 = 100 paise)")
    prep_time_minutes: int = 15
    is_available: bool = True
    safety_profile: DishSafetyProfile = Field(default_factory=DishSafetyProfile)
    tags: list[str] = Field(default_factory=list)

    @field_validator("price_paise", mode="before")
    @classmethod
    def check_paise(cls, v: Any) -> int:
        return validate_strict_paise(v)

    @property
    def price_rupees_formatted(self) -> str:
        """Formatted display string, e.g. ₹280.00 or ₹280."""
        rupees = self.price_paise / 100.0
        if self.price_paise % 100 == 0:
            return f"₹{self.price_paise // 100}"
        return f"₹{rupees:.2f}"


class PreferenceRule(BaseModel):
    """
    Individual preference rule tagged with strength (HARD vs SOFT) and privacy.
    """
    rule_id: str
    description: str
    strength: PreferenceStrength = PreferenceStrength.HARD
    is_private: bool = False


class DinerPreference(BaseModel):
    """
    Preferences stated by a single diner.
    """
    diet_rules: set[DietRule] = Field(
        default_factory=set,
        description="Set of dietary exclusions (e.g. {VEGAN, HALAL}). Empty set means omnivore.",
    )
    allergies: set[Allergen] = Field(
        default_factory=set,
        description="Allergens that MUST be avoided strictly.",
    )
    max_spice: SpiceLevel = Field(
        default=SpiceLevel.EXTRA_SPICY,
        description="Maximum acceptable spice level.",
    )
    max_budget_paise: int | None = Field(
        default=None,
        description="Optional upper budget in integer paise for this diner's total share.",
    )
    must_have_dish_ids: list[str] = Field(
        default_factory=list,
        description="Dishes this diner strongly desires.",
    )
    disliked_dish_ids: list[str] = Field(
        default_factory=list,
        description="Dishes this diner wants to avoid.",
    )
    strength: PreferenceStrength = Field(
        default=PreferenceStrength.HARD,
        description="Whether budget and spice preferences are HARD constraints or SOFT guidelines.",
    )
    is_private: bool = Field(
        default=False,
        description="If True, hides diner's specific budget and individual allergies from group view.",
    )

    @field_validator("max_budget_paise", mode="before")
    @classmethod
    def check_budget_paise(cls, v: Any) -> int | None:
        if v is None:
            return None
        return validate_strict_paise(v)


class Diner(BaseModel):
    """
    A diner participating in a shared table session.
    """
    diner_id: str
    name: str
    is_host: bool = False
    guest_token_hash: str = Field(..., description="SHA-256 hash of the guest token")
    preferences: DinerPreference | None = None
    payment_status: PaymentStatus = PaymentStatus.UNPAID
    paid_amount_paise: int = 0
    joined_at: int = Field(..., description="Unix timestamp (seconds)")

    @field_validator("paid_amount_paise", mode="before")
    @classmethod
    def check_paid_amount(cls, v: Any) -> int:
        return validate_strict_paise(v)


class OrderLineItem(BaseModel):
    """
    An ordered item indicating whether it is shared or personal, and who shares it.
    """
    item_id: str
    dish_id: str
    dish_name: str
    unit_price_paise: int
    quantity: int = 1
    is_shared: bool = True
    sharer_diner_ids: list[str] = Field(default_factory=list)

    @field_validator("unit_price_paise", mode="before")
    @classmethod
    def check_item_price(cls, v: Any) -> int:
        return validate_strict_paise(v)

    @property
    def line_total_paise(self) -> int:
        return self.unit_price_paise * self.quantity


class ProposedOrder(BaseModel):
    """
    Computed group order proposal before confirmation and lock.
    """
    items: list[OrderLineItem] = Field(default_factory=list)
    subtotal_paise: int = 0
    tax_paise: int = 0
    grand_total_paise: int = 0
    diner_splits_paise: dict[str, int] = Field(default_factory=dict)
    fairness_explanation: str | None = None
    unmet_preferences: list[str] = Field(default_factory=list)

    @field_validator("subtotal_paise", "tax_paise", "grand_total_paise", mode="before")
    @classmethod
    def check_order_paise(cls, v: Any) -> int:
        return validate_strict_paise(v)


class TableSession(BaseModel):
    """
    Full state of a multi-person Table Split session.
    """
    session_id: str
    room_code: str = Field(..., description="Secondary human-readable code e.g. TAB-8K2M")
    join_token_128: str = Field(..., description="Primary 128-bit high-entropy secret join token")
    host_diner_id: str
    version: int = Field(default=1, description="Monotonically increasing version for optimistic locking")
    status: SessionStatus = SessionStatus.OPEN_FOR_PREFERENCES
    diners: dict[str, Diner] = Field(default_factory=dict)
    proposed_order: ProposedOrder | None = None
    created_at: int
    updated_at: int
    expires_at: int

    @field_validator("version", mode="before")
    @classmethod
    def check_version(cls, v: Any) -> int:
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ValueError(f"Version must be a positive integer, got {v!r}.")
        return v

    def is_expired(self, current_time: int) -> bool:
        return current_time >= self.expires_at or self.status == SessionStatus.EXPIRED


class AuditEvent(BaseModel):
    """
    Immutable audit log entry for every state mutation.
    """
    event_id: str
    timestamp: int
    session_id: str
    diner_id: str | None = None
    action: str
    payload_hash: str
    details: str | None = None

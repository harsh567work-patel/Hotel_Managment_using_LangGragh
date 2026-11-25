"""
table_split.db.models — SQLAlchemy 2.0 Typed Declarative Domain Models.

Enforces:
- Strict relational constraints, non-negative monetary amounts (paise), valid quantities and spice levels.
- Hard constraint requirement for diet and allergen preferences:
  CHECK ((kind NOT IN ('diet', 'allergen')) OR (strength = 'HARD'))
- FSSAI 2020 9-category allergen tri-states and derived dairy rule.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    validates,
)

from table_split.enums import TriState


class Base(DeclarativeBase):
    pass


def derive_dairy_status(
    allergen_milk: str | TriState,
    diet_vegan: str | TriState = TriState.UNKNOWN,
) -> TriState:
    """
    Derives dairy presence based on regulatory and culinary invariants:
    - Milk allergen YES strictly implies contains_dairy YES.
    - Diet vegan YES implies contains_dairy NO (and allergen milk NO).
    - If milk allergen is NO and vegan is YES/NO, dairy is NO.
    - If milk allergen is UNKNOWN and item is not certified vegan, dairy is UNKNOWN.
    """
    milk_val = allergen_milk.value if isinstance(allergen_milk, TriState) else str(allergen_milk).upper()
    vegan_val = diet_vegan.value if isinstance(diet_vegan, TriState) else str(diet_vegan).upper()

    if milk_val == TriState.YES.value:
        return TriState.YES
    if vegan_val == TriState.YES.value and milk_val != TriState.YES.value:
        return TriState.NO
    if milk_val == TriState.NO.value:
        return TriState.NO
    return TriState.UNKNOWN


# ---------------------------------------------------------------------------
# 1. Menu Item Extension
# ---------------------------------------------------------------------------
class MenuItemExtension(Base):
    __tablename__ = "menu_item_extensions"

    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # Diet tags (TriState: YES / NO / UNKNOWN)
    diet_veg: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)
    diet_non_veg: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)
    diet_egg: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)
    diet_vegan: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)
    diet_jain: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)
    diet_halal: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)
    diet_gluten_free: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)

    # Jain ingredient specific tri-states + Honey flag
    contains_onion_garlic: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)
    contains_root_veg: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)
    contains_honey: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)

    # Statutory Allergen Categories + Non-Statutory Voluntary Categories (TriState: YES / NO / UNKNOWN)
    # [Note: Regulatory clause numbers to be verified by a human food safety / legal auditor]
    allergen_cereals_gluten: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)
    allergen_crustaceans: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)
    allergen_milk: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)
    allergen_eggs: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)
    allergen_fish: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)
    allergen_peanuts: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)
    allergen_tree_nuts: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)
    allergen_soy: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)
    allergen_sulphite: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)
    allergen_sesame: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)
    allergen_mustard: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)

    # Derived rule: milk allergen YES implies dairy YES
    contains_dairy: Mapped[str] = mapped_column(String(10), default=TriState.UNKNOWN.value, nullable=False)

    # Culinary & portion attributes
    spice_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    serves: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    shareable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sales_volume: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Financial & tax attributes in integer paise / basis points
    price_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    tax_rate_bps: Mapped[int] = mapped_column(Integer, default=500, nullable=False)  # 500 bps = 5% GST
    packaging_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Auditing & safety confirmation
    verified_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verified_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cross_contact_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("spice_level IS NULL OR (spice_level >= 0 AND spice_level <= 4)", name="ck_spice_level_0_to_4"),
        CheckConstraint("serves > 0", name="ck_serves_positive"),
        CheckConstraint("price_paise >= 0", name="ck_price_paise_non_negative"),
        CheckConstraint("tax_rate_bps >= 0", name="ck_tax_rate_bps_non_negative"),
        CheckConstraint("packaging_paise >= 0", name="ck_packaging_paise_non_negative"),
        CheckConstraint("sales_volume >= 0", name="ck_sales_volume_non_negative"),
        CheckConstraint(
            "diet_veg IN ('YES', 'NO', 'UNKNOWN') AND "
            "diet_non_veg IN ('YES', 'NO', 'UNKNOWN') AND "
            "diet_egg IN ('YES', 'NO', 'UNKNOWN') AND "
            "diet_vegan IN ('YES', 'NO', 'UNKNOWN') AND "
            "diet_jain IN ('YES', 'NO', 'UNKNOWN') AND "
            "diet_halal IN ('YES', 'NO', 'UNKNOWN') AND "
            "diet_gluten_free IN ('YES', 'NO', 'UNKNOWN') AND "
            "contains_onion_garlic IN ('YES', 'NO', 'UNKNOWN') AND "
            "contains_root_veg IN ('YES', 'NO', 'UNKNOWN') AND "
            "contains_honey IN ('YES', 'NO', 'UNKNOWN')",
            name="ck_diet_tristate",
        ),
        CheckConstraint(
            "allergen_cereals_gluten IN ('YES', 'NO', 'UNKNOWN') AND "
            "allergen_crustaceans IN ('YES', 'NO', 'UNKNOWN') AND "
            "allergen_milk IN ('YES', 'NO', 'UNKNOWN') AND "
            "allergen_eggs IN ('YES', 'NO', 'UNKNOWN') AND "
            "allergen_fish IN ('YES', 'NO', 'UNKNOWN') AND "
            "allergen_peanuts IN ('YES', 'NO', 'UNKNOWN') AND "
            "allergen_tree_nuts IN ('YES', 'NO', 'UNKNOWN') AND "
            "allergen_soy IN ('YES', 'NO', 'UNKNOWN') AND "
            "allergen_sulphite IN ('YES', 'NO', 'UNKNOWN') AND "
            "allergen_sesame IN ('YES', 'NO', 'UNKNOWN') AND "
            "allergen_mustard IN ('YES', 'NO', 'UNKNOWN') AND "
            "contains_dairy IN ('YES', 'NO', 'UNKNOWN')",
            name="ck_allergen_tristate",
        ),
        # Derived invariant at DB level: If allergen_milk is YES, contains_dairy must be YES
        CheckConstraint(
            "allergen_milk != 'YES' OR contains_dairy = 'YES'",
            name="ck_milk_implies_dairy",
        ),
        # Vegan integrity constraints
        CheckConstraint(
            "diet_vegan != 'YES' OR ("
            "diet_egg = 'NO' AND "
            "allergen_milk = 'NO' AND "
            "contains_dairy = 'NO' AND "
            "diet_non_veg != 'YES')",
            name="ck_vegan_implies_no_animal_products",
        ),
        CheckConstraint(
            "NOT (diet_vegan = 'YES' AND diet_non_veg = 'YES')",
            name="ck_vegan_and_non_veg_exclusive",
        ),
        # Jain integrity constraint
        CheckConstraint(
            "diet_jain != 'YES' OR ("
            "contains_onion_garlic != 'YES' AND "
            "contains_root_veg != 'YES' AND "
            "diet_non_veg != 'YES')",
            name="ck_jain_rules",
        ),
    )

    @validates("allergen_milk", "diet_vegan", "contains_dairy", "diet_non_veg", "diet_egg", "contains_honey")
    def validate_dietary_rules(self, key: str, value: Any) -> Any:
        # Enforce derived rule in Python model before flush
        if key == "allergen_milk" and value == "YES":
            self.contains_dairy = "YES"

        vegan = value if key == "diet_vegan" else self.__dict__.get("diet_vegan")
        non_veg = value if key == "diet_non_veg" else self.__dict__.get("diet_non_veg")
        egg = value if key == "diet_egg" else self.__dict__.get("diet_egg")
        milk = value if key == "allergen_milk" else self.__dict__.get("allergen_milk")
        dairy = value if key == "contains_dairy" else self.__dict__.get("contains_dairy")
        honey = value if key == "contains_honey" else self.__dict__.get("contains_honey")

        if vegan == "YES" and non_veg == "YES":
            raise ValueError("Dish cannot be both vegan=YES and non_veg=YES")
        if vegan == "YES":
            if egg is not None and egg != "NO":
                raise ValueError("Vegan dish must have diet_egg='NO'")
            if milk is not None and milk != "NO":
                raise ValueError("Vegan dish must have allergen_milk='NO'")
            if dairy is not None and dairy != "NO":
                raise ValueError("Vegan dish must have contains_dairy='NO'")
            if honey is not None and honey == "YES":
                raise ValueError("Vegan dish cannot have contains_honey='YES'")
            if non_veg is not None and non_veg == "YES":
                raise ValueError("Vegan dish cannot have diet_non_veg='YES'")

        return value


# ---------------------------------------------------------------------------
# 2. Table Sessions
# ---------------------------------------------------------------------------
class TableSession(Base):
    __tablename__ = "table_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    restaurant_id: Mapped[str] = mapped_column(String(64), default="spiceroute_default", nullable=False)
    host_participant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="collecting", nullable=False)
    join_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    requires_host_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_participants: Mapped[int] = mapped_column(Integer, default=12, nullable=False)
    deadline_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    split_mode: Mapped[str] = mapped_column(String(32), default="by_consumption", nullable=False)
    payment_mode: Mapped[str] = mapped_column(String(32), default="each_pays_own", nullable=False)
    delivery_address_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sensitive_data_purged_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[int] = mapped_column(
        Integer, default=lambda: int(datetime.now(UTC).timestamp()), nullable=False
    )

    participants: Mapped[list[Participant]] = relationship(
        "Participant", back_populates="session", cascade="all, delete-orphan"
    )
    proposals: Mapped[list[Proposal]] = relationship(
        "Proposal", back_populates="session", cascade="all, delete-orphan"
    )
    payments: Mapped[list[Payment]] = relationship(
        "Payment", back_populates="session", cascade="all, delete-orphan"
    )
    events: Mapped[list[SessionEvent]] = relationship(
        "SessionEvent", back_populates="session", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("max_participants > 0", name="ck_max_participants_positive"),
        CheckConstraint("version >= 1", name="ck_session_version_positive"),
        CheckConstraint(
            "status IN ('draft', 'collecting', 'proposing', 'locked', 'paying', 'placed', 'cancelled', 'expired')",
            name="ck_session_status_valid",
        ),
        CheckConstraint(
            "split_mode IN ('by_consumption', 'equal')",
            name="ck_split_mode_valid",
        ),
        CheckConstraint(
            "payment_mode IN ('each_pays_own', 'host_pays_all')",
            name="ck_payment_mode_valid",
        ),
        Index("idx_table_sessions_join_token_hash", "join_token_hash", unique=True),
    )


# ---------------------------------------------------------------------------
# 3. Participants
# ---------------------------------------------------------------------------
class Participant(Base):
    __tablename__ = "participants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("table_sessions.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    guest_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="member", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    is_ready: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    joined_at: Mapped[int] = mapped_column(
        Integer, default=lambda: int(datetime.now(UTC).timestamp()), nullable=False
    )

    session: Mapped[TableSession] = relationship("TableSession", back_populates="participants")
    preferences: Mapped[list[Preference]] = relationship(
        "Preference", back_populates="participant", cascade="all, delete-orphan"
    )
    allocations: Mapped[list[Allocation]] = relationship(
        "Allocation", back_populates="participant", cascade="all, delete-orphan"
    )
    payments: Mapped[list[Payment]] = relationship(
        "Payment", back_populates="participant", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("role IN ('host', 'member')", name="ck_participant_role_valid"),
        CheckConstraint("status IN ('pending', 'active', 'left', 'removed')", name="ck_participant_status_valid"),
        Index("idx_participants_session_id", "session_id"),
        Index("idx_participants_guest_token_hash", "guest_token_hash"),
    )


# ---------------------------------------------------------------------------
# 4. Preferences
# ---------------------------------------------------------------------------
class Preference(Base):
    __tablename__ = "preferences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    participant_id: Mapped[str] = mapped_column(String(36), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    strength: Mapped[str] = mapped_column(String(10), nullable=False)
    weight: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)  # JSON encoded value
    source: Mapped[str] = mapped_column(String(32), default="chip", nullable=False)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    parser_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    confirmed_by_user: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_private: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    participant: Mapped[Participant] = relationship("Participant", back_populates="preferences")

    __table_args__ = (
        CheckConstraint(
            "kind IN ('diet', 'allergen', 'budget_max_paise', 'spice_min', 'spice_max', 'cuisine_like', 'dislike_item', 'must_include_category', 'must_include_item', 'freeform')",
            name="ck_preference_kind_valid",
        ),
        CheckConstraint("strength IN ('HARD', 'SOFT')", name="ck_preference_strength_valid"),
        CheckConstraint("weight >= 1 AND weight <= 5", name="ck_preference_weight_range"),
        CheckConstraint("source IN ('chip', 'freetext_parsed', 'host_set')", name="ck_preference_source_valid"),
        # Strict clinical safety rule: allergen and diet constraints MUST be HARD
        CheckConstraint(
            "(kind NOT IN ('diet', 'allergen')) OR (strength = 'HARD')",
            name="ck_diet_allergen_must_be_hard",
        ),
        Index("idx_preferences_participant_id", "participant_id"),
    )

    @validates("kind", "strength")
    def validate_safety_strength(self, key: str, value: Any) -> Any:
        current_kind = value if key == "kind" else self.__dict__.get("kind")
        current_strength = value if key == "strength" else self.__dict__.get("strength")

        if (
            current_kind in ("diet", "allergen")
            and current_strength is not None
            and current_strength != "HARD"
        ):
            raise ValueError(f"Preference of kind '{current_kind}' must have strength 'HARD'")
        return value


# ---------------------------------------------------------------------------
# 5. Proposals
# ---------------------------------------------------------------------------
class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("table_sessions.id", ondelete="CASCADE"), nullable=False)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False)
    engine_version: Mapped[str] = mapped_column(String(32), default="1.0.0", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    constraints_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    explanation_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[int] = mapped_column(
        Integer, default=lambda: int(datetime.now(UTC).timestamp()), nullable=False
    )

    session: Mapped[TableSession] = relationship("TableSession", back_populates="proposals")
    lines: Mapped[list[ProposalLine]] = relationship(
        "ProposalLine", back_populates="proposal", cascade="all, delete-orphan"
    )
    allocations: Mapped[list[Allocation]] = relationship(
        "Allocation", back_populates="proposal", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("session_version >= 1", name="ck_proposal_session_version_positive"),
        CheckConstraint("status IN ('draft', 'active', 'superseded', 'approved')", name="ck_proposal_status_valid"),
        Index("idx_proposals_session_id", "session_id"),
    )


# ---------------------------------------------------------------------------
# 6. Proposal Lines
# ---------------------------------------------------------------------------
class ProposalLine(Base):
    __tablename__ = "proposal_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    proposal_id: Mapped[str] = mapped_column(String(36), ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False)
    menu_item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="SHARED", nullable=False)
    consumer_participant_ids: Mapped[str] = mapped_column(Text, default="[]", nullable=False)  # JSON array
    unit_price_paise: Mapped[int] = mapped_column(Integer, nullable=False)

    proposal: Mapped[Proposal] = relationship("Proposal", back_populates="lines")

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_proposal_line_quantity_positive"),
        CheckConstraint("unit_price_paise >= 0", name="ck_proposal_line_unit_price_paise_non_negative"),
        CheckConstraint("kind IN ('SHARED', 'PERSONAL')", name="ck_proposal_line_kind_valid"),
        Index("idx_proposal_lines_proposal_id", "proposal_id"),
        Index("idx_proposal_lines_menu_item_id", "menu_item_id"),
    )

    def get_consumer_ids(self) -> list[str]:
        return json.loads(self.consumer_participant_ids or "[]")

    def set_consumer_ids(self, ids: list[str]) -> None:
        self.consumer_participant_ids = json.dumps(ids)


# ---------------------------------------------------------------------------
# 7. Allocations
# ---------------------------------------------------------------------------
class Allocation(Base):
    __tablename__ = "allocations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    proposal_id: Mapped[str] = mapped_column(String(36), ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False)
    participant_id: Mapped[str] = mapped_column(String(36), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False)
    items_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tax_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    delivery_share_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    packaging_share_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    discount_share_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rounding_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_paise: Mapped[int] = mapped_column(Integer, nullable=False)

    proposal: Mapped[Proposal] = relationship("Proposal", back_populates="allocations")
    participant: Mapped[Participant] = relationship("Participant", back_populates="allocations")

    __table_args__ = (
        CheckConstraint("items_paise >= 0", name="ck_alloc_items_paise_non_negative"),
        CheckConstraint("tax_paise >= 0", name="ck_alloc_tax_paise_non_negative"),
        CheckConstraint("delivery_share_paise >= 0", name="ck_alloc_delivery_share_paise_non_negative"),
        CheckConstraint("packaging_share_paise >= 0", name="ck_alloc_packaging_share_paise_non_negative"),
        CheckConstraint("discount_share_paise >= 0", name="ck_alloc_discount_share_paise_non_negative"),
        CheckConstraint("total_paise >= 0", name="ck_alloc_total_paise_non_negative"),
        Index("idx_allocations_proposal_id", "proposal_id"),
        Index("idx_allocations_participant_id", "participant_id"),
    )


# ---------------------------------------------------------------------------
# 8. Payments
# ---------------------------------------------------------------------------
class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("table_sessions.id", ondelete="CASCADE"), nullable=False)
    participant_id: Mapped[str] = mapped_column(String(36), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False)
    gateway: Mapped[str] = mapped_column(String(32), default="razorpay", nullable=False)
    gateway_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="created", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[int] = mapped_column(
        Integer, default=lambda: int(datetime.now(UTC).timestamp()), nullable=False
    )

    session: Mapped[TableSession] = relationship("TableSession", back_populates="payments")
    participant: Mapped[Participant] = relationship("Participant", back_populates="payments")

    __table_args__ = (
        CheckConstraint("amount_paise >= 0", name="ck_payment_amount_paise_non_negative"),
        CheckConstraint(
            "status IN ('created', 'pending', 'captured', 'failed', 'refunded', 'partially_refunded')",
            name="ck_payment_status_valid",
        ),
        Index("idx_payments_session_id", "session_id"),
        Index("idx_payments_participant_id", "participant_id"),
        Index("idx_payments_idempotency_key", "idempotency_key", unique=True),
    )


# ---------------------------------------------------------------------------
# 9. Session Events (Append-only Audit Log)
# ---------------------------------------------------------------------------
class SessionEvent(Base):
    __tablename__ = "session_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("table_sessions.id", ondelete="CASCADE"), nullable=False)
    participant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("participants.id", ondelete="SET NULL"), nullable=True
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    session_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[int] = mapped_column(
        Integer, default=lambda: int(datetime.now(UTC).timestamp()), nullable=False
    )

    session: Mapped[TableSession] = relationship("TableSession", back_populates="events")

    __table_args__ = (
        Index("idx_session_events_session_id", "session_id"),
        Index("idx_session_events_created_at", "created_at"),
        Index("idx_session_events_session_seq", "session_id", "seq"),
    )

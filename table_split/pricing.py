"""
table_split.pricing — Pure zero-I/O pricing interface (Stage 7 contract) and
deterministic integer-paise stub implementation (Stage 6).

INVARIANTS:
1. ZERO I/O: No database, file system, network, clock, or environment variable access.
2. Strict Integer Paise: Every monetary quantity rejects bool and float via validate_strict_paise.
3. Conservation of Money (Zero-Penny Remainder):
   For every bucket (items, tax, packaging, delivery, grand total), the sum of
   per-participant shares strictly equals the order total down to the single paise.
4. Permutation Invariance: Remainder paise are assigned in sorted participant_id
   ascending order so input participant order never alters anyone's bill.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from table_split.models import validate_strict_paise

DEFAULT_GST_BPS = 500  # 5.00% GST in basis points


def _reject_bool_int(v: Any, field_name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise TypeError(f"{field_name} must be a strict integer, got {type(v).__name__} ({v!r}).")
    return v


def calculate_line_tax_paise(line_items_paise: int, tax_rate_bps: int) -> int:
    """Computes integer GST paise using deterministic half-up rounding from basis points."""
    items = validate_strict_paise(line_items_paise)
    bps = validate_strict_paise(tax_rate_bps)
    return (items * bps + 5000) // 10000


def split_integer_paise(amount_paise: int, recipient_ids: Sequence[str]) -> dict[str, int]:
    """
    Splits `amount_paise` equally across `recipient_ids` with zero-penny remainder.
    Remainder paise are assigned deterministically by sorted `recipient_id` ascending.
    """
    amount = validate_strict_paise(amount_paise)
    if not recipient_ids:
        if amount == 0:
            return {}
        raise ValueError("Cannot split positive amount_paise across empty recipient_ids.")

    if len(set(recipient_ids)) != len(recipient_ids):
        raise ValueError("recipient_ids must not contain duplicate participant IDs.")

    ordered = sorted(recipient_ids)
    n = len(ordered)
    base, rem = divmod(amount, n)
    return {
        pid: base + (1 if idx < rem else 0)
        for idx, pid in enumerate(ordered)
    }


class PricingLineInput(BaseModel):
    """Canonical proposal line passed to a PricingEngine implementation."""

    model_config = ConfigDict(extra="forbid")

    menu_item_id: str
    dish_name: str = ""
    quantity: int = Field(default=1, ge=1)
    kind: Literal["SHARED", "PERSONAL"]
    consumer_participant_ids: list[str]
    unit_price_paise: int
    tax_rate_bps: int = DEFAULT_GST_BPS
    packaging_paise: int = 0

    @field_validator("quantity", mode="before")
    @classmethod
    def _check_quantity(cls, v: Any) -> int:
        qty = _reject_bool_int(v, "quantity")
        if qty < 1:
            raise ValueError(f"quantity must be >= 1, got {qty}.")
        return qty

    @field_validator("unit_price_paise", "tax_rate_bps", "packaging_paise", mode="before")
    @classmethod
    def _check_paise(cls, v: Any) -> int:
        return validate_strict_paise(v)

    @model_validator(mode="after")
    def _check_consumers(self) -> PricingLineInput:
        if len(set(self.consumer_participant_ids)) != len(self.consumer_participant_ids):
            raise ValueError("consumer_participant_ids must not contain duplicates.")
        if self.kind == "SHARED" and len(self.consumer_participant_ids) < 2:
            raise ValueError("SHARED line must have at least 2 consumer_participant_ids.")
        if self.kind == "PERSONAL" and len(self.consumer_participant_ids) != 1:
            raise ValueError("PERSONAL line must have exactly 1 consumer_participant_id.")
        return self


class ParticipantAllocation(BaseModel):
    """Exact per-participant financial allocation in integer paise."""

    model_config = ConfigDict(extra="forbid")

    participant_id: str
    items_paise: int = 0
    tax_paise: int = 0
    delivery_share_paise: int = 0
    packaging_share_paise: int = 0
    discount_share_paise: int = 0
    rounding_paise: int = 0
    total_paise: int = 0

    @field_validator(
        "items_paise",
        "tax_paise",
        "delivery_share_paise",
        "packaging_share_paise",
        "discount_share_paise",
        "total_paise",
        mode="before",
    )
    @classmethod
    def _check_non_negative_paise(cls, v: Any) -> int:
        return validate_strict_paise(v)

    @field_validator("rounding_paise", mode="before")
    @classmethod
    def _check_rounding_paise(cls, v: Any) -> int:
        return _reject_bool_int(v, "rounding_paise")

    @model_validator(mode="after")
    def _verify_sum_equation(self) -> ParticipantAllocation:
        expected = (
            self.items_paise
            + self.tax_paise
            + self.delivery_share_paise
            + self.packaging_share_paise
            - self.discount_share_paise
            + self.rounding_paise
        )
        if self.total_paise != expected:
            raise ValueError(
                f"Allocation total_paise ({self.total_paise}) != sum of components ({expected})."
            )
        return self


class LinePricingBreakdown(BaseModel):
    """Line-level itemized pricing and per-participant split."""

    model_config = ConfigDict(extra="forbid")

    menu_item_id: str
    kind: Literal["SHARED", "PERSONAL"]
    quantity: int
    unit_price_paise: int
    line_items_paise: int
    line_tax_paise: int
    line_packaging_paise: int
    line_total_paise: int
    per_participant_items_paise: dict[str, int]
    per_participant_tax_paise: dict[str, int]
    per_participant_packaging_paise: dict[str, int]
    per_participant_total_paise: dict[str, int]


class ProposalPricingResult(BaseModel):
    """Complete bill breakdown and zero-penny conserved participant allocations."""

    model_config = ConfigDict(extra="forbid")

    pricing_version: str
    subtotal_paise: int = 0
    tax_paise: int = 0
    packaging_paise: int = 0
    delivery_fee_paise: int = 0
    discount_paise: int = 0
    grand_total_paise: int = 0
    line_breakdowns: list[LinePricingBreakdown] = Field(default_factory=list)
    allocations: dict[str, ParticipantAllocation] = Field(default_factory=dict)

    @field_validator(
        "subtotal_paise",
        "tax_paise",
        "packaging_paise",
        "delivery_fee_paise",
        "discount_paise",
        "grand_total_paise",
        mode="before",
    )
    @classmethod
    def _check_totals_paise(cls, v: Any) -> int:
        return validate_strict_paise(v)

    @model_validator(mode="after")
    def _verify_conservation(self) -> ProposalPricingResult:
        expected_grand = (
            self.subtotal_paise
            + self.tax_paise
            + self.packaging_paise
            + self.delivery_fee_paise
            - self.discount_paise
        )
        if self.grand_total_paise != expected_grand:
            raise ValueError(
                f"grand_total_paise ({self.grand_total_paise}) != expected ({expected_grand})."
            )
        if self.allocations:
            sum_items = sum(a.items_paise for a in self.allocations.values())
            sum_tax = sum(a.tax_paise for a in self.allocations.values())
            sum_pkg = sum(a.packaging_share_paise for a in self.allocations.values())
            sum_del = sum(a.delivery_share_paise for a in self.allocations.values())
            sum_disc = sum(a.discount_share_paise for a in self.allocations.values())
            sum_round = sum(a.rounding_paise for a in self.allocations.values())
            sum_total = sum(a.total_paise for a in self.allocations.values())
            if (
                sum_items != self.subtotal_paise
                or sum_tax != self.tax_paise
                or sum_pkg != self.packaging_paise
                or sum_del != self.delivery_fee_paise
                or sum_disc != self.discount_paise
                or sum_total != self.grand_total_paise
            ):
                raise ValueError(
                    "Conservation of money violated across participant allocations "
                    f"(sum_total={sum_total}, grand_total={self.grand_total_paise}, rounding={sum_round})."
                )
        elif self.grand_total_paise != 0:
            raise ValueError("Non-zero grand_total_paise requires participant allocations.")
        return self


@runtime_checkable
class PricingEngine(Protocol):
    """
    Stage 7 Pricing & Split Engine Interface.
    Stage 6 provides StubPricingEngine; Stage 7 extends it with coupons, discounts,
    and full rounding/settlement rules without altering solver boundaries.
    """

    pricing_version: str

    def estimate_delivery_shares(
        self,
        participant_ids: Sequence[str],
        delivery_fee_paise: int = 0,
    ) -> dict[str, int]: ...

    def estimate_line_participant_upper_bound(
        self,
        *,
        unit_price_paise: int,
        tax_rate_bps: int,
        packaging_paise: int,
        quantity: int,
        num_sharers: int,
    ) -> int: ...

    def estimate_line_participant_lower_bound(
        self,
        *,
        unit_price_paise: int,
        tax_rate_bps: int,
        packaging_paise: int,
        quantity: int,
        num_sharers: int,
    ) -> int: ...

    def calculate_proposal_pricing(
        self,
        lines: Sequence[PricingLineInput],
        participant_ids: Sequence[str],
        *,
        delivery_fee_paise: int = 0,
        default_tax_rate_bps: int = DEFAULT_GST_BPS,
    ) -> ProposalPricingResult: ...


class StubPricingEngine:
    """
    Stage 6 deterministic integer-paise pricing stub implementing the Stage 7 PricingEngine interface.
    Splits each line's items, tax, and packaging equally among its consumers, and splits
    optional delivery_fee_paise equally across all table participants with zero-penny remainder.
    """

    def __init__(
        self,
        *,
        default_tax_rate_bps: int = DEFAULT_GST_BPS,
        pricing_version: str = "0.6.0-stub",
    ) -> None:
        self.default_tax_rate_bps = validate_strict_paise(default_tax_rate_bps)
        self.pricing_version = pricing_version

    def estimate_delivery_shares(
        self,
        participant_ids: Sequence[str],
        delivery_fee_paise: int = 0,
    ) -> dict[str, int]:
        fee = validate_strict_paise(delivery_fee_paise)
        if not participant_ids:
            if fee == 0:
                return {}
            raise ValueError("delivery_fee_paise > 0 requires at least one participant.")
        return split_integer_paise(fee, participant_ids)

    def estimate_line_participant_upper_bound(
        self,
        *,
        unit_price_paise: int,
        tax_rate_bps: int,
        packaging_paise: int,
        quantity: int,
        num_sharers: int,
    ) -> int:
        if num_sharers < 1:
            raise ValueError("num_sharers must be >= 1.")
        qty = _reject_bool_int(quantity, "quantity")
        if qty < 1:
            raise ValueError("quantity must be >= 1.")
        unit = validate_strict_paise(unit_price_paise)
        bps = validate_strict_paise(tax_rate_bps)
        pkg = validate_strict_paise(packaging_paise)

        items = unit * qty
        tax = calculate_line_tax_paise(items, bps)
        pack = pkg * qty
        return (
            (items + num_sharers - 1) // num_sharers
            + (tax + num_sharers - 1) // num_sharers
            + (pack + num_sharers - 1) // num_sharers
        )

    def estimate_line_participant_lower_bound(
        self,
        *,
        unit_price_paise: int,
        tax_rate_bps: int,
        packaging_paise: int,
        quantity: int,
        num_sharers: int,
    ) -> int:
        if num_sharers < 1:
            raise ValueError("num_sharers must be >= 1.")
        qty = _reject_bool_int(quantity, "quantity")
        if qty < 1:
            raise ValueError("quantity must be >= 1.")
        unit = validate_strict_paise(unit_price_paise)
        bps = validate_strict_paise(tax_rate_bps)
        pkg = validate_strict_paise(packaging_paise)

        items = unit * qty
        tax = calculate_line_tax_paise(items, bps)
        pack = pkg * qty
        return (items // num_sharers) + (tax // num_sharers) + (pack // num_sharers)

    def calculate_proposal_pricing(
        self,
        lines: Sequence[PricingLineInput],
        participant_ids: Sequence[str],
        *,
        delivery_fee_paise: int = 0,
        default_tax_rate_bps: int | None = None,
    ) -> ProposalPricingResult:
        fee = validate_strict_paise(delivery_fee_paise)
        if default_tax_rate_bps is not None:
            _ = validate_strict_paise(default_tax_rate_bps)

        if len(set(participant_ids)) != len(participant_ids):
            raise ValueError("participant_ids must not contain duplicate participant IDs.")

        ordered_pids = sorted(participant_ids)
        pid_set = set(ordered_pids)

        items_by_p: dict[str, int] = {pid: 0 for pid in ordered_pids}
        tax_by_p: dict[str, int] = {pid: 0 for pid in ordered_pids}
        pkg_by_p: dict[str, int] = {pid: 0 for pid in ordered_pids}
        del_by_p = self.estimate_delivery_shares(ordered_pids, fee)

        subtotal_paise = 0
        total_tax_paise = 0
        total_pkg_paise = 0
        breakdowns: list[LinePricingBreakdown] = []

        for line in lines:
            unknown_consumers = [pid for pid in line.consumer_participant_ids if pid not in pid_set]
            if unknown_consumers:
                raise ValueError(f"Line {line.menu_item_id} references unknown participant(s): {unknown_consumers}")

            line_items = line.unit_price_paise * line.quantity
            effective_bps = line.tax_rate_bps
            line_tax = calculate_line_tax_paise(line_items, effective_bps)
            line_pkg = line.packaging_paise * line.quantity
            line_total = line_items + line_tax + line_pkg

            subtotal_paise += line_items
            total_tax_paise += line_tax
            total_pkg_paise += line_pkg

            item_splits = split_integer_paise(line_items, line.consumer_participant_ids)
            tax_splits = split_integer_paise(line_tax, line.consumer_participant_ids)
            pkg_splits = split_integer_paise(line_pkg, line.consumer_participant_ids)

            per_p_total: dict[str, int] = {}
            for pid in sorted(line.consumer_participant_ids):
                items_by_p[pid] += item_splits[pid]
                tax_by_p[pid] += tax_splits[pid]
                pkg_by_p[pid] += pkg_splits[pid]
                per_p_total[pid] = item_splits[pid] + tax_splits[pid] + pkg_splits[pid]

            breakdowns.append(
                LinePricingBreakdown(
                    menu_item_id=line.menu_item_id,
                    kind=line.kind,
                    quantity=line.quantity,
                    unit_price_paise=line.unit_price_paise,
                    line_items_paise=line_items,
                    line_tax_paise=line_tax,
                    line_packaging_paise=line_pkg,
                    line_total_paise=line_total,
                    per_participant_items_paise=item_splits,
                    per_participant_tax_paise=tax_splits,
                    per_participant_packaging_paise=pkg_splits,
                    per_participant_total_paise=per_p_total,
                )
            )

        allocations: dict[str, ParticipantAllocation] = {}
        for pid in ordered_pids:
            d_share = del_by_p.get(pid, 0)
            p_total = items_by_p[pid] + tax_by_p[pid] + pkg_by_p[pid] + d_share
            allocations[pid] = ParticipantAllocation(
                participant_id=pid,
                items_paise=items_by_p[pid],
                tax_paise=tax_by_p[pid],
                delivery_share_paise=d_share,
                packaging_share_paise=pkg_by_p[pid],
                discount_share_paise=0,
                rounding_paise=0,
                total_paise=p_total,
            )

        grand_total = subtotal_paise + total_tax_paise + total_pkg_paise + fee
        return ProposalPricingResult(
            pricing_version=self.pricing_version,
            subtotal_paise=subtotal_paise,
            tax_paise=total_tax_paise,
            packaging_paise=total_pkg_paise,
            delivery_fee_paise=fee,
            discount_paise=0,
            grand_total_paise=grand_total,
            line_breakdowns=breakdowns,
            allocations=allocations,
        )

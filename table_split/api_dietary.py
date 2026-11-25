"""
table_split.api_dietary — FastAPI endpoints for Stage 3 Dietary Verification & Data-Quality Reporting.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from config import TABLE_SPLIT_DB_PATH
from table_split.db.dietary_repo import DietaryRepository
from table_split.dietary import ALL_TRISTATE_FIELDS

router = APIRouter(tags=["Dietary & Verification"])


def get_dietary_repository() -> DietaryRepository:
    """FastAPI dependency returning the configured DietaryRepository (overridable in tests)."""
    return DietaryRepository(db_path=TABLE_SPLIT_DB_PATH)


RepoDep = Annotated[DietaryRepository, Depends(get_dietary_repository)]


class VerifyDietaryItemRequest(BaseModel):
    verified_by: str = Field(..., min_length=1, description="Name/ID of staff member confirming dietary info")
    verified_at: int | None = Field(default=None, description="Optional Unix epoch seconds (defaults to current time)")
    updates: dict[str, Any] = Field(
        default_factory=dict,
        description="Tri-state field edits (YES/NO/UNKNOWN) and culinary metadata (spice_level, serves, shareable, cross_contact_note, sales_volume)",
    )


@router.get("/table-split/dietary/report")
@router.get("/dietary/report", include_in_schema=False)
def get_dietary_data_quality_report(repo: RepoDep) -> dict[str, Any]:
    """
    Data-quality report endpoint returning verification completeness percentage
    and the list of items blocking Table Split safety ordered by sales_volume descending.
    """
    return repo.generate_data_quality_report()


@router.get("/table-split/dietary/items")
def list_dietary_items(
    repo: RepoDep,
    only_blocking: Annotated[
        bool,
        Query(description="When true, only return unverified items or items with UNKNOWN fields"),
    ] = False,
) -> dict[str, Any]:
    """Lists normalized dietary & allergen records across all catalog dishes."""
    items = repo.list_items()
    if only_blocking:
        items = [it for it in items if not it.is_fully_verified]

    # Sort highest sales volume first for staff prioritization
    items_sorted = sorted(items, key=lambda it: (-it.sales_volume, it.item_id))
    return {
        "count": len(items_sorted),
        "items": [
            {
                **it.model_dump(mode="json"),
                "is_verified": it.is_verified,
                "is_fully_verified": it.is_fully_verified,
                "unknown_fields": it.get_unknown_fields(),
                "declared_allergens": it.declared_allergens(),
            }
            for it in items_sorted
        ],
    }


@router.put("/table-split/dietary/items/{item_id}/verify")
def verify_dietary_item(
    item_id: str,
    payload: VerifyDietaryItemRequest,
    repo: RepoDep,
) -> dict[str, Any]:
    """Updates dietary/allergen fields for a dish and records verified_by and verified_at."""
    unknown_keys = set(payload.updates.keys()) - set(ALL_TRISTATE_FIELDS) - {
        "spice_level",
        "serves",
        "shareable",
        "cross_contact_note",
        "sales_volume",
        "tax_rate_bps",
        "packaging_paise",
    }
    if unknown_keys:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported update fields: {sorted(unknown_keys)}",
        )

    try:
        updated = repo.verify_item(
            item_id=item_id,
            updates=payload.updates,
            verified_by=payload.verified_by,
            verified_at=payload.verified_at,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "status": "verified",
        "item": {
            **updated.model_dump(mode="json"),
            "is_verified": updated.is_verified,
            "is_fully_verified": updated.is_fully_verified,
            "unknown_fields": updated.get_unknown_fields(),
        },
    }

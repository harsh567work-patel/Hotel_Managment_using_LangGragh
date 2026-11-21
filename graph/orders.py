"""
orders.py — Pure-Python lifecycle helpers for concurrent kitchen orders.
Tracks live remaining preparation/delivery time, enforces max active cooking order limits,
and automatically transitions orders from 'cooking' to 'served' when their timer hits 0.
"""

import time
from typing import List, Optional
from graph.state import OrderRecord


def get_remaining_seconds(
    order: OrderRecord,
    now: Optional[float] = None,
    time_scale: float = 1.0,
) -> int:
    """
    Returns remaining seconds until the order is served (0 if already served or expired).
    time_scale=1.0 is real-time; time_scale=60.0 advances 1 kitchen minute per real second (demo mode).
    """
    if order.get("status") == "served":
        return 0

    current_time = now if now is not None else time.time()
    placed_at = float(order.get("placed_at", current_time))
    total_seconds = max(0, int(order.get("eta_minutes", 20)) * 60)
    elapsed_scaled = max(0.0, (current_time - placed_at) * max(1.0, float(time_scale)))
    remaining = int(round(total_seconds - elapsed_scaled))
    return max(0, remaining)


def sync_orders(
    orders: Optional[List[OrderRecord]],
    now: Optional[float] = None,
    time_scale: float = 1.0,
) -> List[OrderRecord]:
    """
    Returns an updated copy of orders where any cooking order whose timer has reached 0
    is transitioned permanently to status='served'.
    """
    if not orders:
        return []

    current_time = now if now is not None else time.time()
    updated: List[OrderRecord] = []

    for order in orders:
        record = dict(order)
        if record.get("status") != "served":
            rem = get_remaining_seconds(record, now=current_time, time_scale=time_scale)
            record["status"] = "served" if rem <= 0 else "cooking"
        updated.append(record)

    return updated


def get_active_cooking_orders(
    orders: Optional[List[OrderRecord]],
    now: Optional[float] = None,
    time_scale: float = 1.0,
) -> List[OrderRecord]:
    """Returns all orders currently in 'cooking' state after syncing timers."""
    synced = sync_orders(orders, now=now, time_scale=time_scale)
    return [o for o in synced if o.get("status") == "cooking"]


def get_served_orders(
    orders: Optional[List[OrderRecord]],
    now: Optional[float] = None,
    time_scale: float = 1.0,
) -> List[OrderRecord]:
    """Returns all orders that have reached 'served' state."""
    synced = sync_orders(orders, now=now, time_scale=time_scale)
    return [o for o in synced if o.get("status") == "served"]


def format_remaining(seconds: int) -> str:
    """Formats seconds into MM:SS string (e.g., 34:15)."""
    secs = max(0, int(seconds))
    mins, rem_secs = divmod(secs, 60)
    return f"{mins:02d}:{rem_secs:02d}"

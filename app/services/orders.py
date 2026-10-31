from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from ..models import Order, StockMovement

STATUSES = ("new", "confirmed", "shipped", "received", "closed", "cancelled")

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "new": {"confirmed", "cancelled"},
    "confirmed": {"shipped", "received", "cancelled"},
    "shipped": {"received", "cancelled"},
    "received": {"closed"},
    "closed": set(),
    "cancelled": set(),
}


def allowed_next(status: str) -> list[str]:
    """Statuses offered in the UI, including the current one as a no-op."""
    return sorted(ALLOWED_TRANSITIONS.get(status, set()) | {status})


class OrderTransitionError(ValueError):
    pass


def change_status(db: Session, order: Order, new_status: str) -> None:
    if new_status == order.status:
        return
    if new_status not in STATUSES:
        raise OrderTransitionError(f"Unknown status {new_status!r}")
    if new_status not in ALLOWED_TRANSITIONS.get(order.status, set()):
        raise OrderTransitionError(f"Cannot move order from {order.status} to {new_status}")

    if new_status == "received":
        apply_stock(db, order)
    order.status = new_status
    db.flush()


def apply_stock(db: Session, order: Order) -> None:
    """Increase inventory once per order, the first time it is received."""
    if order.stock_applied:
        return
    for item in order.items:
        if item.product is None:
            continue
        item.product.stock_qty = Decimal(item.product.stock_qty) + Decimal(item.quantity)
        db.add(
            StockMovement(
                product_id=item.product.id,
                order_id=order.id,
                delta=item.quantity,
                reason=f"received order {order.order_number}",
            )
        )
    order.stock_applied = True

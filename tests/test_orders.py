from decimal import Decimal

import pytest

from app.models import Order, OrderItem, Product, StockMovement
from app.services.orders import (
    OrderTransitionError,
    allowed_next,
    change_status,
)


def make_order(db, quantity=Decimal("5")):
    product = Product(name="Widget", sku="SKU-1", stock_qty=Decimal("2"))
    order = Order(order_number="PO-1", status="new")
    db.add_all([product, order])
    db.flush()
    db.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            description="Widget",
            quantity=quantity,
            unit_price=Decimal("10"),
        )
    )
    db.commit()
    db.refresh(order)
    return order, product


def test_receiving_increases_stock_once(db):
    order, product = make_order(db)

    change_status(db, order, "confirmed")
    change_status(db, order, "received")
    db.commit()

    assert product.stock_qty == Decimal("7")
    assert order.stock_applied is True
    movements = db.query(StockMovement).all()
    assert len(movements) == 1
    assert movements[0].delta == Decimal("5")

    # Receiving is idempotent even if called again on an already received order.
    from app.services.orders import apply_stock

    apply_stock(db, order)
    db.commit()
    assert product.stock_qty == Decimal("7")


def test_invalid_transition_is_rejected(db):
    order, _ = make_order(db)

    with pytest.raises(OrderTransitionError):
        change_status(db, order, "received")


def test_allowed_next_includes_current_status():
    assert set(allowed_next("new")) == {"new", "confirmed", "cancelled"}

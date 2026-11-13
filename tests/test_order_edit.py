from __future__ import annotations

from datetime import date
from decimal import Decimal
from urllib.parse import unquote

import pytest
from sqlalchemy import select

from app.models import Attachment, EmailMessage, Order, OrderItem, Product
from app.services import ingest
from app.services.pdf_parser import ParsedItem, ParsedOrder, PdfContent


@pytest.fixture
def order(db):
    product = Product(sku="SKU-1", name="Widget", stock_qty=Decimal("5"))
    db.add(product)
    db.flush()
    order = Order(
        order_number="PO-1001",
        supplier="Acme",
        order_date=date(2026, 1, 2),
        currency="EUR",
        total_amount=Decimal("25.00"),
    )
    db.add(order)
    db.flush()
    db.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            sku="SKU-1",
            description="Widget",
            quantity=Decimal("2"),
            unit_price=Decimal("12.50"),
            line_total=Decimal("25.00"),
        )
    )
    db.commit()
    return order


def form_payload(order: Order, **overrides) -> dict[str, list[str]]:
    """One existing line plus one empty row, as the browser submits them."""
    item = order.items[0]
    values = {
        "order_number": order.order_number,
        "supplier": order.supplier or "",
        "order_date": order.order_date.isoformat() if order.order_date else "",
        "currency": order.currency,
        "item_id": str(item.id),
        "sku": item.sku or "",
        "description": item.description,
        "quantity": "2",
        "unit_price": "12.50",
        "line_total": "",
        "row_action": "keep",
    }
    values.update(overrides)

    data = {
        "order_number": [values["order_number"]],
        "supplier": [values["supplier"]],
        "order_date": [values["order_date"]],
        "currency": [values["currency"]],
    }
    for name in ("item_id", "sku", "description", "quantity", "unit_price", "line_total"):
        data[name] = [values[name], ""]
    data["row_action"] = [values["row_action"], "keep"]
    return data


def add_row(data: dict[str, list[str]], **values) -> None:
    for name in ("item_id", "sku", "description", "quantity", "unit_price", "line_total"):
        data[name].append(values.get(name, ""))
    data["row_action"].append(values.get("row_action", "keep"))


def test_edit_form_shows_the_existing_values(client, order):
    response = client.get(f"/orders/{order.id}/edit")

    assert response.status_code == 200
    assert "PO-1001" in response.text
    assert "Widget" in response.text
    assert "Save changes" in response.text


def test_edit_recalculates_the_line_and_order_totals(client, db, order):
    response = client.post(
        f"/orders/{order.id}/edit",
        data=form_payload(order, quantity="4", unit_price="12.50", supplier="Acme Ltd"),
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    db.expire_all()
    updated = db.scalar(select(Order).where(Order.id == order.id))
    item = updated.items[0]
    assert item.quantity == Decimal("4.00")
    assert item.line_total == Decimal("50.00")
    assert updated.total_amount == Decimal("50.00")
    assert updated.supplier == "Acme Ltd"


def test_edit_keeps_a_typed_line_total_when_there_is_no_unit_price(client, db, order):
    response = client.post(
        f"/orders/{order.id}/edit",
        data=form_payload(order, unit_price="", line_total="30"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    db.expire_all()
    item = db.scalar(select(OrderItem).where(OrderItem.order_id == order.id))
    assert item.unit_price is None
    assert item.line_total == Decimal("30.00")


def test_edit_adds_a_new_line_item(client, db, order):
    data = form_payload(order)
    add_row(data, sku="SKU-2", description="Gadget", quantity="3", unit_price="5")

    response = client.post(f"/orders/{order.id}/edit", data=data, follow_redirects=False)

    assert response.status_code == 303, response.text
    db.expire_all()
    items = db.scalars(
        select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id)
    ).all()
    assert [item.description for item in items] == ["Widget", "Gadget"]
    assert items[1].line_total == Decimal("15.00")
    assert db.scalar(select(Product).where(Product.sku == "SKU-2")) is not None


def test_edit_deletes_a_line_item(client, db, order):
    response = client.post(
        f"/orders/{order.id}/edit",
        data=form_payload(order, row_action="delete"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    db.expire_all()
    assert db.scalars(select(OrderItem).where(OrderItem.order_id == order.id)).all() == []
    assert db.scalar(select(Order).where(Order.id == order.id)).total_amount is None


def test_edit_rejects_a_duplicate_order_number(client, db, order):
    db.add(Order(order_number="PO-2000"))
    db.commit()

    response = client.post(
        f"/orders/{order.id}/edit",
        data=form_payload(order, order_number="PO-2000"),
    )

    assert response.status_code == 400
    assert "already belongs to another order" in response.text
    db.expire_all()
    assert db.scalar(select(Order.order_number).where(Order.id == order.id)) == "PO-1001"


def test_edit_points_at_the_bad_field_and_keeps_the_typed_values(client, order):
    response = client.post(
        f"/orders/{order.id}/edit",
        data=form_payload(order, quantity="ten"),
    )

    assert response.status_code == 400
    assert "must be a number" in response.text
    assert 'value="ten"' in response.text


def test_a_received_order_locks_quantities(client, db, order):
    order.stock_applied = True
    db.commit()

    response = client.post(
        f"/orders/{order.id}/edit",
        data=form_payload(order, quantity="99"),
    )

    assert response.status_code == 400
    assert "quantities are locked" in response.text
    db.expire_all()
    assert db.scalar(select(OrderItem.quantity).where(OrderItem.order_id == order.id)) == Decimal(
        "2.00"
    )


def test_a_received_order_can_still_be_repriced(client, db, order):
    order.stock_applied = True
    db.commit()

    response = client.post(
        f"/orders/{order.id}/edit",
        data=form_payload(order, unit_price="9.99"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    db.expire_all()
    item = db.scalar(select(OrderItem).where(OrderItem.order_id == order.id))
    assert item.quantity == Decimal("2.00")
    assert item.line_total == Decimal("19.98")


def _order_with_attachment(db, order: Order) -> Attachment:
    email = EmailMessage(message_id="<m1@example.com>", subject="PO")
    db.add(email)
    db.flush()
    attachment = Attachment(
        email_id=email.id, filename="po.pdf", path="missing.pdf", sha256="0" * 64
    )
    db.add(attachment)
    db.flush()
    order.email_id = email.id
    order.attachment_id = attachment.id
    db.commit()
    return attachment


def test_reparse_replaces_items_from_the_stored_pdf(client, db, order, monkeypatch):
    attachment = _order_with_attachment(db, order)
    monkeypatch.setattr(
        ingest,
        "extract_pdf",
        lambda path, ocr_enabled=False: PdfContent(text="Order PO-1001", method="text"),
    )
    parsed = ParsedOrder(
        order_number="PO-1001",
        total_amount=Decimal("40.00"),
        items=[
            ParsedItem(description="Widget", quantity=Decimal("2"), unit_price=Decimal("10")),
            ParsedItem(description="Gadget", quantity=Decimal("4"), unit_price=Decimal("5")),
        ],
    )
    monkeypatch.setattr(ingest, "parse_order", lambda *args, **kwargs: parsed)

    response = client.post(f"/orders/{order.id}/reparse", follow_redirects=False)

    assert response.status_code == 303
    db.expire_all()
    items = db.scalars(
        select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id)
    ).all()
    assert [item.description for item in items] == ["Widget", "Gadget"]
    assert db.get(Order, order.id).total_amount == Decimal("40.00")
    assert db.get(Attachment, attachment.id).parse_status == "parsed"


def test_reparse_is_refused_once_stock_was_applied(client, db, order, monkeypatch):
    _order_with_attachment(db, order)
    order.stock_applied = True
    db.commit()
    called = False

    def explode(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("parser should not run")

    monkeypatch.setattr(ingest, "extract_pdf", explode)

    response = client.post(f"/orders/{order.id}/reparse", follow_redirects=False)

    assert response.status_code == 303
    assert called is False
    assert "cannot be re-parsed" in unquote(response.headers["location"])

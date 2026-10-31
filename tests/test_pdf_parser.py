from decimal import Decimal

from app.services.pdf_parser import parse_order


SAMPLE = """ACME Supplies Ltd
Purchase Order No: PO-1042
Order Date: 2024-03-15

SKU-100 Widget A 10 12.50 125.00
SKU-200 Gadget B 4 x 7.25 29.00

Subtotal 154.00
Total 154.00
"""


def test_parses_order_header_and_items():
    order = parse_order(SAMPLE, supplier_hint="ACME Supplies Ltd")

    assert order.order_number == "PO-1042"
    assert order.supplier == "ACME Supplies Ltd"
    assert order.order_date.isoformat() == "2024-03-15"
    assert order.total_amount == Decimal("154.00")
    assert order.currency == "EUR"
    assert len(order.items) == 2

    first, second = order.items
    assert first.sku == "SKU-100"
    assert first.description == "Widget A"
    assert first.quantity == Decimal("10")
    assert first.unit_price == Decimal("12.50")
    assert first.line_total == Decimal("125.00")

    assert second.sku == "SKU-200"
    assert second.quantity == Decimal("4")
    assert second.line_total == Decimal("29.00")


def test_detects_currency_and_european_decimals():
    text = "Order No: PO-9\nTotal: 1.234,56 €\nWidget 2 617,28 1.234,56"
    order = parse_order(text)

    assert order.currency == "EUR"
    assert order.total_amount == Decimal("1234.56")
    assert order.items[0].unit_price == Decimal("617.28")


def test_ignores_lines_without_price():
    order = parse_order("Order No: X-1\nSome header text\nAnother note here")

    assert order.order_number == "X-1"
    assert order.items == []

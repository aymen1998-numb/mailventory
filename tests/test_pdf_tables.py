from decimal import Decimal
from pathlib import Path

import pytest

from app.services import fillable_forms
from app.services.pdf_parser import _classify_header_cell, items_from_tables, parse_order


TABLE = [
    ["SKU", "Description", "Qty", "Unit Price", "Line Total"],
    ["SKU-100", "Widget A", "10", "12.50", "125.00"],
    ["SKU-200", "Gadget B, large", "4", "7.25", "29.00"],
    ["", "Subtotal", "", "", "154.00"],
    ["", "VAT 20%", "", "", "30.80"],
]

HEADER_TEXT = """ACME Supplies Ltd
Purchase Order No: PO-2001
Order Date: 2024-04-02
"""


def test_reads_line_items_from_a_header_table():
    items = items_from_tables([TABLE])

    assert len(items) == 2
    first, second = items
    assert first.sku == "SKU-100"
    assert first.description == "Widget A"
    assert first.quantity == Decimal("10")
    assert first.unit_price == Decimal("12.50")
    assert first.line_total == Decimal("125.00")
    assert second.description == "Gadget B, large"
    assert second.line_total == Decimal("29.00")


def test_skips_total_and_tax_rows():
    descriptions = [item.description for item in items_from_tables([TABLE])]

    assert "Subtotal" not in descriptions
    assert not any("VAT" in name for name in descriptions)


def test_table_beats_flat_text_parsing():
    # The text layer splits descriptions from numbers, so the line regex finds
    # nothing; the table is the only usable structure in the document.
    text = HEADER_TEXT + "\nWidget A\nGadget B, large\n"
    order = parse_order(text, tables=[TABLE])

    assert order.order_number == "PO-2001"
    assert len(order.items) == 2
    assert order.items[1].description == "Gadget B, large"


def test_falls_back_to_text_when_no_table_matches():
    order = parse_order(
        "Order No: PO-7\nSKU-100 Widget A 10 12.50 125.00",
        tables=[[["Address", "City"], ["12 Main St", "Algiers"]]],
    )

    assert len(order.items) == 1
    assert order.items[0].description == "Widget A"


def test_derives_missing_unit_price_from_quantity_and_total():
    table = [
        ["Description", "Qty", "Amount"],
        ["Bolt M8", "50", "25.00"],
    ]
    item = items_from_tables([table])[0]

    assert item.quantity == Decimal("50")
    assert item.unit_price == Decimal("0.50")
    assert item.line_total == Decimal("25.00")


def test_header_classification_prefers_the_more_specific_column():
    assert _classify_header_cell("Unit Price") == "unit_price"
    assert _classify_header_cell("Total Price") == "line_total"
    assert _classify_header_cell("Qty") == "quantity"
    assert _classify_header_cell("Item No.") == "sku"
    assert _classify_header_cell("Notes") is None


def test_fillable_path_sits_next_to_the_source(tmp_path: Path):
    source = tmp_path / "01-abc-po.pdf"
    target = fillable_forms.fillable_path(source)

    assert target.parent == source.parent
    assert target.name == "01-abc-po-fillable.pdf"


def test_missing_command_raises_a_helpful_error(tmp_path: Path):
    source = tmp_path / "po.pdf"
    source.write_bytes(b"%PDF-1.4")

    assert fillable_forms.is_available("mailventory-no-such-command") is False
    with pytest.raises(fillable_forms.CommonFormsError) as excinfo:
        fillable_forms.prepare_fillable_form(
            source, command="mailventory-no-such-command"
        )

    assert "pipx install commonforms" in str(excinfo.value)

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Order, OrderItem
from .ingest import get_or_create_product

CENT = Decimal("0.01")


class OrderEditError(ValueError):
    """A validation problem with a message meant for the person editing."""


@dataclass
class ItemRow:
    """One editable line, as submitted by the form or read from the database."""

    item_id: int | None = None
    sku: str = ""
    description: str = ""
    quantity: str = ""
    unit_price: str = ""
    line_total: str = ""
    action: str = "keep"

    @classmethod
    def from_item(cls, item: OrderItem) -> ItemRow:
        return cls(
            item_id=item.id,
            sku=item.sku or "",
            description=item.description,
            quantity=fmt_input(item.quantity),
            unit_price=fmt_input(item.unit_price),
            line_total=fmt_input(item.line_total),
        )

    @property
    def blank(self) -> bool:
        values = (self.sku, self.description, self.quantity, self.unit_price, self.line_total)
        return not any(value.strip() for value in values)


def fmt_input(value: Decimal | None) -> str:
    """Format a stored amount for a text input (10.00 -> "10", 12.50 -> "12.5")."""
    if value is None:
        return ""
    return f"{Decimal(value).normalize():f}"


def rows_from_order(order: Order) -> list[ItemRow]:
    return [ItemRow.from_item(item) for item in sorted(order.items, key=lambda i: i.id or 0)]


def blank_rows(count: int = 2) -> list[ItemRow]:
    """Empty rows offered at the end of the edit form; ignored when left blank."""
    return [ItemRow() for _ in range(count)]


FIELDS = (
    "item_id",
    "sku",
    "description",
    "quantity",
    "unit_price",
    "line_total",
    "row_action",
)


def rows_from_form(form: Any) -> list[ItemRow]:
    """Turn the repeated item fields of a submitted form into rows."""
    columns = {name: _column(form, name) for name in FIELDS}

    rows: list[ItemRow] = []
    for index in range(len(columns["item_id"])):
        raw_id = _cell(columns["item_id"], index).strip()
        rows.append(
            ItemRow(
                item_id=int(raw_id) if raw_id else None,
                sku=_cell(columns["sku"], index),
                description=_cell(columns["description"], index),
                quantity=_cell(columns["quantity"], index),
                unit_price=_cell(columns["unit_price"], index),
                line_total=_cell(columns["line_total"], index),
                action=_cell(columns["row_action"], index) or "keep",
            )
        )
    return rows


def _column(form: Any, name: str) -> list[str]:
    if hasattr(form, "getlist"):
        values = form.getlist(name)
    else:
        values = form.get(name) or []
    return ["" if value is None else str(value) for value in values]


def _cell(values: list[str], index: int) -> str:
    return values[index] if index < len(values) else ""


def apply_edit(
    db: Session,
    order: Order,
    *,
    order_number: str,
    supplier: str,
    order_date: str,
    currency: str,
    rows: list[ItemRow],
) -> Decimal | None:
    """Validate and store an edit. Returns the recalculated order total.

    Raises ``OrderEditError`` with a message meant for the UI; the caller is
    expected to roll back.
    """
    number = order_number.strip()
    if not number:
        raise OrderEditError("Order number is required.")
    clash = db.scalar(
        select(Order.id).where(Order.order_number == number, Order.id != order.id)
    )
    if clash is not None:
        raise OrderEditError(f"Order number {number} already belongs to another order.")

    parsed_date = _parse_date(order_date)
    code = currency.strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise OrderEditError("Currency must be a three-letter code, such as EUR.")

    existing = {item.id: item for item in order.items}
    kept = [row for row in rows if row.action != "delete" and not row.blank]
    removed = [row for row in rows if row.action == "delete" and row.item_id]

    unknown = [row for row in kept if row.item_id and row.item_id not in existing]
    if unknown:
        raise OrderEditError("That line item no longer exists. Reload the order and try again.")

    if order.stock_applied:
        _check_locked(order, existing, kept, rows)

    for row in kept:
        if not row.description.strip():
            raise OrderEditError("Every kept line item needs a description.")

    values = [(row, _row_values(row, existing.get(row.item_id))) for row in kept]

    order.order_number = number
    order.supplier = supplier.strip() or None
    order.order_date = parsed_date
    order.currency = code

    for row in removed:
        db.delete(existing[row.item_id])

    for row, (quantity, unit_price, line_total) in values:
        description = row.description.strip()
        sku = row.sku.strip() or None
        item = existing.get(row.item_id) if row.item_id else None
        if order.stock_applied and item is not None and item.product is not None:
            # The stock already moved, so keep the product link and rename in place.
            item.product.name = description
            product_id = item.product_id
        else:
            # Resolve the product first: it may flush, and a half-filled item
            # would hit order_items.description NOT NULL.
            product_id = get_or_create_product(db, sku=sku, name=description).id

        if item is None:
            item = OrderItem(order_id=order.id)
            db.add(item)
        item.product_id = product_id
        item.sku = sku
        item.description = description
        item.quantity = quantity
        item.unit_price = unit_price
        item.line_total = line_total

    totals = [line_total for _, (_, _, line_total) in values if line_total is not None]
    order.total_amount = sum(totals, Decimal("0")).quantize(CENT) if totals else None
    db.flush()
    return order.total_amount


def _check_locked(
    order: Order,
    existing: dict[int, OrderItem],
    kept: list[ItemRow],
    rows: list[ItemRow],
) -> None:
    """Stock already moved, so the item set and quantities may not change."""
    kept_ids = {row.item_id for row in kept}
    submitted = [row for row in rows if not row.blank]
    if kept_ids != set(existing) or len(kept) != len(submitted):
        raise OrderEditError(
            "Stock for this order is already applied, so line items are locked. "
            "Adjust quantities on the Inventory page instead."
        )
    for row in kept:
        submitted = _decimal(row.quantity, f"Quantity for {row.description or 'a line'}")
        item = existing[row.item_id]
        if submitted != Decimal(item.quantity):
            raise OrderEditError(
                "Stock for this order is already applied, so quantities are locked. "
                f"Adjust {item.description} on the Inventory page instead."
            )


def _row_values(
    row: ItemRow, item: OrderItem | None
) -> tuple[Decimal, Decimal | None, Decimal | None]:
    label = (row.description or (item.description if item else "") or "line item").strip()
    quantity = _decimal(row.quantity, f"Quantity for {label}")
    if quantity <= 0:
        raise OrderEditError(f"Quantity for {label} must be greater than zero.")
    unit_price = _decimal(row.unit_price, f"Unit price for {label}", allow_blank=True)
    line_total = _decimal(row.line_total, f"Line total for {label}", allow_blank=True)

    if unit_price is not None:
        line_total = (quantity * unit_price).quantize(CENT)
    return quantity, unit_price, line_total


def _decimal(value: str, label: str, *, allow_blank: bool = False) -> Decimal | None:
    text = (value or "").strip().replace(",", ".")
    if not text:
        if allow_blank:
            return None
        raise OrderEditError(f"{label} is required.")
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        raise OrderEditError(f"{label} must be a number, not {value.strip()!r}.") from None
    if not parsed.is_finite():
        raise OrderEditError(f"{label} must be a finite number.")
    if parsed < 0:
        raise OrderEditError(f"{label} cannot be negative.")
    return parsed


def _parse_date(value: str) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise OrderEditError("Order date must look like 2026-10-31.") from None

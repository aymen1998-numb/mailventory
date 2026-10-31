"""PDF text extraction and heuristic purchase-order parsing.

The parser is intentionally rule based: real supplier invoices vary, so tune the
patterns below against a sample PDF instead of expecting every layout to work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pdfplumber

CURRENCY_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "DH": "MAD"}

ORDER_NUMBER_RE = re.compile(
    r"(?:purchase\s+order|order|po|invoice)\s*(?:no\.?|number|num|#)?\s*[:#-]?\s*"
    r"(?P<number>[A-Z0-9][A-Z0-9._/-]{2,})",
    re.IGNORECASE,
)
SUPPLIER_RE = re.compile(
    r"^(?:supplier|vendor|seller|from)\s*[:]\s*(?P<name>.+)$", re.IGNORECASE | re.MULTILINE
)
DATE_RE = re.compile(
    r"(?:order\s+date|date)\s*[:]?\s*"
    r"(?P<date>\d{4}-\d{2}-\d{2}|\d{1,2}[/.]\d{1,2}[/.]\d{2,4}|"
    r"\d{1,2}\s+\w+\s+\d{4}|\w+\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)
TOTAL_RE = re.compile(
    r"\b(?:grand\s+total|total\s+amount|amount\s+due|total)\s*[:]?\s*"
    r"(?P<amount>[€$£]?\s*\d[\d.,]*)",
    re.IGNORECASE,
)
ITEM_LINE_RE = re.compile(
    r"^(?:(?P<sku>[A-Z0-9][A-Z0-9._/-]{1,30})\s+)?"
    r"(?P<desc>[A-Za-z][A-Za-z0-9 ,.'&()/+-]{2,120}?)\s+"
    r"(?P<qty>\d+(?:[.,]\d+)?)\s*(?:x|×|@)?\s*"
    r"(?P<price>[€$£]?\s*\d[\d.,]*)\s*"
    r"(?P<line_total>[€$£]?\s*\d[\d.,]*)?$",
    re.IGNORECASE,
)
SKIP_LINE_RE = re.compile(
    r"^(?:subtotal|total|tax|vat|shipping|page\s+\d+|thank you)\b", re.IGNORECASE
)
NUMBER_RE = re.compile(r"-?\d[\d.,]*")

# Column headers seen in supplier line-item tables. The longest matching hint
# wins, so "unit price" beats "price" and "total price" beats "price".
TABLE_HEADER_HINTS: dict[str, tuple[str, ...]] = {
    "sku": ("item no", "part no", "reference", "article", "sku", "code", "ref"),
    "description": ("description", "designation", "product", "item", "desc", "name"),
    "quantity": ("quantity", "qty", "quantite", "units", "pcs"),
    "unit_price": ("unit price", "unit cost", "price", "prix", "rate"),
    "line_total": ("line total", "total price", "total amount", "total", "amount", "montant"),
}
# A table only counts as a line-item table when it has these columns plus money.
REQUIRED_TABLE_ROLES = ("description", "quantity")


@dataclass
class ParsedItem:
    description: str
    quantity: Decimal
    sku: str | None = None
    unit_price: Decimal | None = None
    line_total: Decimal | None = None


@dataclass
class ParsedOrder:
    currency: str = "EUR"
    order_number: str | None = None
    supplier: str | None = None
    order_date: date | None = None
    total_amount: Decimal | None = None
    items: list[ParsedItem] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return bool(self.order_number and self.items)


@dataclass
class PdfContent:
    """Everything the parser needs from one PDF: text, how it was read, and tables."""

    text: str
    method: str
    tables: list[list[list[str]]] = field(default_factory=list)


def extract_pdf(path: Path, *, ocr_enabled: bool = False) -> PdfContent:
    """Return the text layer plus any line-item tables found in the PDF.

    ``method`` is ``text``, ``ocr`` or ``empty``.
    """
    tables: list[list[list[str]]] = []
    with pdfplumber.open(str(path)) as pdf:
        text = "\n".join((page.extract_text() or "") for page in pdf.pages).strip()
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                if table:
                    tables.append(table)

    if text:
        return PdfContent(text=text, method="text", tables=tables)
    if ocr_enabled:
        # OCR output is plain text; pdfplumber found no text layer, so no tables.
        return PdfContent(text=ocr_pdf(path), method="ocr")
    return PdfContent(text="", method="empty")


def extract_pdf_text(path: Path, *, ocr_enabled: bool = False) -> tuple[str, str]:
    """Return (text, method). Kept for callers that only need the text."""
    content = extract_pdf(path, ocr_enabled=ocr_enabled)
    return content.text, content.method


def ocr_pdf(path: Path) -> str:
    """Optional OCR path. Install the ``ocr`` extras and Poppler first."""
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError as exc:  # pragma: no cover - depends on optional extras
        raise RuntimeError(
            "OCR is enabled but dependencies are missing. "
            "Run: pip install -e '.[ocr]' and install Poppler."
        ) from exc

    pages = convert_from_path(str(path))
    return "\n".join(pytesseract.image_to_string(page) for page in pages).strip()


def parse_order(
    text: str,
    *,
    supplier_hint: str | None = None,
    order_number_hint: str | None = None,
    tables: list[list[list[str]]] | None = None,
) -> ParsedOrder:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    joined = "\n".join(lines)

    currency = "EUR"
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text:
            currency = code
            break

    order = ParsedOrder(currency=currency)

    number_match = ORDER_NUMBER_RE.search(joined)
    order.order_number = (
        order_number_hint
        or (number_match.group("number").strip(" .:-") if number_match else None)
    )

    supplier_match = SUPPLIER_RE.search(joined)
    order.supplier = supplier_hint or (
        supplier_match.group("name").strip() if supplier_match else None
    )

    date_match = DATE_RE.search(joined)
    order.order_date = _parse_date(date_match.group("date")) if date_match else None

    total_match = TOTAL_RE.search(joined)
    order.total_amount = _parse_money(total_match.group("amount")) if total_match else None

    # Tables beat line matching: a real column layout survives multi-word
    # descriptions and prices that the flat-text regex would misread.
    table_items = items_from_tables(tables) if tables else []
    order.items = table_items or _parse_items(lines)
    return order


def _parse_items(lines: list[str]) -> list[ParsedItem]:
    items: list[ParsedItem] = []
    for line in lines:
        if SKIP_LINE_RE.match(line):
            continue
        match = ITEM_LINE_RE.match(line)
        if not match:
            continue
        quantity = _parse_decimal(match.group("qty"))
        price = _parse_money(match.group("price"))
        if quantity is None or quantity <= 0 or price is None:
            continue
        line_total = _parse_money(match.group("line_total"))
        items.append(
            ParsedItem(
                description=match.group("desc").strip(" .-"),
                sku=(match.group("sku") or "").strip() or None,
                quantity=quantity,
                unit_price=price,
                line_total=line_total or (quantity * price).quantize(Decimal("0.01")),
            )
        )
    return items


def items_from_tables(tables: list[list[list[str]]]) -> list[ParsedItem]:
    """Turn pdfplumber tables into line items, skipping tables without a header."""
    items: list[ParsedItem] = []
    for table in tables:
        items.extend(_items_from_table(table))
    return items


def _items_from_table(table: list[list[str | None]]) -> list[ParsedItem]:
    rows = [[_clean_cell(cell) for cell in row] for row in table]
    rows = [row for row in rows if any(row)]

    for index, row in enumerate(rows[:6]):
        roles = _column_roles(row)
        if _is_item_header(roles):
            return _items_from_rows(rows[index + 1 :], roles)
    return []


def _items_from_rows(
    rows: list[list[str]], roles: dict[str, int]
) -> list[ParsedItem]:
    items: list[ParsedItem] = []
    for row in rows:
        description = _cell(row, roles.get("description"))
        if not description or not re.search(r"[A-Za-z]", description):
            continue
        if SKIP_LINE_RE.match(description):
            continue

        quantity = _cell_number(_cell(row, roles.get("quantity")))
        unit_price = _cell_number(_cell(row, roles.get("unit_price")))
        line_total = _cell_number(_cell(row, roles.get("line_total")))

        # Fill in whichever of qty / price / total the layout left out.
        if line_total is None and quantity and unit_price is not None:
            line_total = (quantity * unit_price).quantize(Decimal("0.01"))
        if unit_price is None and quantity and line_total is not None:
            unit_price = (line_total / quantity).quantize(Decimal("0.01"))
        if quantity is None and unit_price and line_total:
            quantity = (line_total / unit_price).quantize(Decimal("0.001"))

        if not quantity or quantity <= 0:
            continue
        if unit_price is None and line_total is None:
            continue

        items.append(
            ParsedItem(
                description=description.strip(" .-"),
                sku=_sku_from_cell(_cell(row, roles.get("sku"))),
                quantity=quantity,
                unit_price=unit_price,
                line_total=line_total,
            )
        )
    return items


def _column_roles(header: list[str]) -> dict[str, int]:
    roles: dict[str, int] = {}
    for index, cell in enumerate(header):
        role = _classify_header_cell(cell)
        if role and role not in roles:
            roles[role] = index
    return roles


def _classify_header_cell(cell: str) -> str | None:
    key = _normalize_header(cell)
    if not key:
        return None
    best_role: str | None = None
    best_length = 0
    for role, hints in TABLE_HEADER_HINTS.items():
        for hint in hints:
            if hint in key and len(hint) > best_length:
                best_role, best_length = role, len(hint)
    return best_role


def _is_item_header(roles: dict[str, int]) -> bool:
    if any(role not in roles for role in REQUIRED_TABLE_ROLES):
        return False
    return "unit_price" in roles or "line_total" in roles


def _normalize_header(cell: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (cell or "").lower())).strip()


def _clean_cell(cell: str | None) -> str:
    return re.sub(r"\s+", " ", (cell or "").replace("\n", " ")).strip()


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index]


def _cell_number(cell: str) -> Decimal | None:
    if not cell:
        return None
    match = NUMBER_RE.search(cell)
    return _parse_decimal(match.group(0)) if match else None


def _sku_from_cell(cell: str) -> str | None:
    value = cell.strip()
    if not value or len(value) > 30 or " " in value:
        return None
    if not re.search(r"\d", value) and not value.isupper():
        return None
    return value


def _parse_date(value: str) -> date | None:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_decimal(value: str) -> Decimal | None:
    cleaned = value.replace(" ", "").strip()
    if not cleaned:
        return None
    # Last separator wins: "1.234,56" -> 1234.56, "1,234.56" -> 1234.56
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        whole, _, fraction = cleaned.rpartition(",")
        cleaned = f"{whole}.{fraction}" if len(fraction) in (1, 2) else cleaned.replace(",", "")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _parse_money(value: str | None) -> Decimal | None:
    if not value:
        return None
    return _parse_decimal(re.sub(r"[€$£]", "", value))

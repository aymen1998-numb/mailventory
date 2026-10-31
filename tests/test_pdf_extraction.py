"""End-to-end check that ruled supplier tables reach the order pipeline.

The PDF is generated here with raw PDF operators so the test needs no extra
dependency; pdfplumber reads the grid back through its line-based table finder.
"""

from decimal import Decimal

from app.config import Settings
from app.services.imap_client import FetchedAttachment, FetchedEmail
from app.services.ingest import ingest_emails
from app.services.pdf_parser import extract_pdf, parse_order

PAGE_W, PAGE_H = 612, 792
COL_W = [70, 220, 60, 90, 90]
ROW_H = 22
HEADER = [
    "ACME Supplies Ltd",
    "Purchase Order No: PO-2001",
    "Order Date: 2024-04-02",
]
ROWS = [
    ["SKU", "Description", "Qty", "Unit Price", "Line Total"],
    ["SKU-100", "Widget A", "10", "12.50", "125.00"],
    ["SKU-200", "Gadget B, large", "4", "7.25", "29.00"],
]


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_table_pdf() -> bytes:
    """Return a one-page PDF with a ruled line-item table."""
    left, top = 40, 720
    ops = ["0 0 0 RG 0.8 w"]

    y = top + 40
    for line in HEADER:
        ops.append(f"BT /F1 11 Tf {left} {y} Td ({_escape(line)}) Tj ET")
        y -= 16

    table_top = y - 10
    xs = [left]
    for width in COL_W:
        xs.append(xs[-1] + width)

    for row in range(len(ROWS) + 1):
        yy = table_top - row * ROW_H
        ops.append(f"{xs[0]} {yy} m {xs[-1]} {yy} l S")
    for x in xs:
        ops.append(f"{x} {table_top} m {x} {table_top - ROW_H * len(ROWS)} l S")

    for row, cells in enumerate(ROWS):
        yy = table_top - row * ROW_H - 15
        for column, cell in enumerate(cells):
            if cell:
                ops.append(f"BT /F1 9 Tf {xs[column] + 3} {yy} Td ({_escape(cell)}) Tj ET")

    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
            "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ).encode(),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def test_extracts_a_ruled_table_from_a_real_pdf(tmp_path):
    pdf_path = tmp_path / "po.pdf"
    pdf_path.write_bytes(build_table_pdf())

    content = extract_pdf(pdf_path)

    assert content.method == "text"
    assert len(content.tables) == 1
    assert content.tables[0][0][1].strip() == "Description"

    order = parse_order(content.text, tables=content.tables)
    assert order.order_number == "PO-2001"
    assert [item.sku for item in order.items] == ["SKU-100", "SKU-200"]
    assert order.items[1].description == "Gadget B, large"
    assert order.items[0].line_total == Decimal("125.00")


def test_ingest_creates_an_order_from_a_table_pdf(db, tmp_path):
    settings = Settings(attachment_dir=tmp_path)
    fetched = FetchedEmail(
        message_id="<table@example.com>",
        subject="Purchase order attached",
        sender="ACME Supplies <orders@acme.test>",
        received_at=None,
        body_text="Please see the attached order.",
        attachments=[
            FetchedAttachment(
                filename="po-2001.pdf",
                content_type="application/pdf",
                data=build_table_pdf(),
            )
        ],
    )

    report = ingest_emails(db, [fetched], settings)

    assert report.orders_created == 1
    assert report.needs_review == 0

    from sqlalchemy import select

    from app.models import Order, OrderItem

    order = db.scalar(select(Order))
    assert order is not None and order.order_number == "PO-2001"
    items = db.scalars(select(OrderItem).order_by(OrderItem.sku)).all()
    assert [item.sku for item in items] == ["SKU-100", "SKU-200"]
    assert items[0].quantity == Decimal("10")

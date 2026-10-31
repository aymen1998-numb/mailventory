from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Attachment, EmailMessage, Order, OrderItem, Product
from .imap_client import FetchedEmail
from .pdf_parser import extract_pdf_text, parse_order
from .suppliers import supplier_from_sender


@dataclass
class IngestReport:
    emails_seen: int = 0
    emails_new: int = 0
    attachments_saved: int = 0
    orders_created: int = 0
    duplicates: int = 0
    needs_review: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.emails_new} new email(s), {self.attachments_saved} PDF(s), "
            f"{self.orders_created} order(s) created, {self.needs_review} for review"
        )


def ingest_emails(db: Session, emails: list[FetchedEmail], settings: Settings) -> IngestReport:
    report = IngestReport(emails_seen=len(emails))

    for fetched in emails:
        exists = db.scalar(
            select(EmailMessage.id).where(EmailMessage.message_id == fetched.message_id)
        )
        if exists:
            report.duplicates += 1
            continue

        email = EmailMessage(
            message_id=fetched.message_id,
            subject=fetched.subject,
            sender=fetched.sender,
            received_at=fetched.received_at,
            body_text=fetched.body_text,
        )
        db.add(email)
        db.flush()
        report.emails_new += 1

        supplier = supplier_from_sender(fetched.sender)
        for index, attachment in enumerate(fetched.attachments, start=1):
            try:
                created = _ingest_attachment(
                    db, settings, email, attachment.data, attachment.filename, supplier, index
                )
            except Exception as exc:  # keep ingesting the remaining emails
                email.error = f"{attachment.filename}: {exc}"[:2000]
                report.errors.append(f"{fetched.subject or fetched.message_id}: {exc}")
                report.needs_review += 1
                continue

            report.attachments_saved += 1
            if created:
                report.orders_created += 1
            else:
                report.needs_review += 1

    db.commit()
    return report


def _ingest_attachment(
    db: Session,
    settings: Settings,
    email: EmailMessage,
    data: bytes,
    filename: str,
    supplier_hint: str | None,
    index: int,
) -> bool:
    import hashlib

    digest = hashlib.sha256(data).hexdigest()
    safe_name = _safe_filename(filename)
    target_dir = settings.attachment_dir / f"email-{email.id}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{index:02d}-{digest[:10]}-{safe_name}"
    target.write_bytes(data)

    attachment = Attachment(
        email_id=email.id,
        filename=filename,
        path=str(target),
        sha256=digest,
        size_bytes=len(data),
    )
    db.add(attachment)
    db.flush()

    try:
        text, method = extract_pdf_text(target, ocr_enabled=settings.ocr_enabled)
    except Exception as exc:
        attachment.parse_status = "error"
        attachment.parse_error = str(exc)[:2000]
        return False

    attachment.extracted_text = text
    if not text:
        attachment.parse_status = "needs_ocr"
        attachment.parse_error = "No embedded text; enable OCR to read scanned PDFs."
        return False

    parsed = parse_order(
        text,
        supplier_hint=supplier_hint,
        order_number_hint=f"MAIL-{email.id}-{index}" if not _has_order_number(text) else None,
    )
    if not parsed.items:
        attachment.parse_status = "needs_review"
        attachment.parse_error = "Could not detect line items; tune parser patterns."
        return False

    if db.scalar(select(Order.id).where(Order.order_number == parsed.order_number)):
        attachment.parse_status = "duplicate"
        return False

    total = parsed.total_amount
    if total is None:
        total = sum((item.line_total or Decimal("0") for item in parsed.items), Decimal("0"))

    order = Order(
        order_number=parsed.order_number or f"MAIL-{email.id}-{index}",
        supplier=parsed.supplier,
        order_date=parsed.order_date,
        currency=parsed.currency,
        total_amount=total,
        email_id=email.id,
        attachment_id=attachment.id,
        notes=f"Imported from {filename} ({method} extraction)",
    )
    db.add(order)
    db.flush()

    for item in parsed.items:
        product = get_or_create_product(db, sku=item.sku, name=item.description)
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                sku=item.sku,
                description=item.description,
                quantity=item.quantity,
                unit_price=item.unit_price,
                line_total=item.line_total,
            )
        )

    attachment.parse_status = "parsed"
    db.flush()
    return True


def get_or_create_product(
    db: Session, *, sku: str | None, name: str, unit: str = "pcs"
) -> Product:
    sku = (sku or "").strip() or None
    name = re.sub(r"\s+", " ", name).strip(" .-") or "Unnamed item"

    product: Product | None = None
    if sku:
        product = db.scalar(select(Product).where(Product.sku == sku))
    if product is None:
        product = db.scalar(select(Product).where(func.lower(Product.name) == name.lower()))
    if product is None:
        product = Product(sku=sku, name=name, unit=unit)
        db.add(product)
        db.flush()
    elif product.sku is None and sku:
        product.sku = sku
    return product


def _has_order_number(text: str) -> bool:
    from .pdf_parser import ORDER_NUMBER_RE

    return bool(ORDER_NUMBER_RE.search(text))


def _safe_filename(filename: str) -> str:
    name = Path(filename).name or "attachment.pdf"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]

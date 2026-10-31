from __future__ import annotations

import argparse
import sys
import time
from decimal import Decimal

from sqlalchemy import select

from .config import get_settings
from .db import SessionLocal, init_db
from .models import Order, OrderItem, SyncRun
from .services.gmail_sync import start_sync
from .services.imap_client import ImapError, fetch_emails
from .services.ingest import get_or_create_product, ingest_emails


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local Order Desk CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="Fetch new emails over IMAP and import PDF orders")
    ingest.add_argument("--limit", type=int, default=None, help="Max messages to read")

    sync = sub.add_parser("sync", help="Background-style scan of Gmail PDF emails")
    sync.add_argument("--all", action="store_true", help="Re-scan the whole mailbox")

    sub.add_parser("seed-demo", help="Insert sample products and a demo order")

    args = parser.parse_args(argv)
    init_db()
    settings = get_settings()

    if args.command == "ingest":
        try:
            emails = fetch_emails(settings, limit=args.limit)
        except ImapError as exc:
            print(f"IMAP error: {exc}", file=sys.stderr)
            return 2
        with SessionLocal() as db:
            report = ingest_emails(db, emails, settings)
        print(report.summary())
        for error in report.errors:
            print(f"  - {error}", file=sys.stderr)
        return 0

    if args.command == "sync":
        try:
            run_id = start_sync("all" if args.all else "new")
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2

        last_message = None
        status = "queued"
        while True:
            with SessionLocal() as db:
                run = db.get(SyncRun, run_id)
                if run is None:
                    return 1
                status = run.status
                message = run.message or status
            if message != last_message:
                print(message)
                last_message = message
            if status in ("completed", "failed"):
                break
            time.sleep(1)
        return 0 if status == "completed" else 1

    if args.command == "seed-demo":
        with SessionLocal() as db:
            if db.scalar(select(Order.id).where(Order.order_number == "DEMO-1001")):
                print("Demo data already present.")
                return 0
            blue = get_or_create_product(db, sku="DEMO-BLUE", name="Blue Widget")
            red = get_or_create_product(db, sku="DEMO-RED", name="Red Widget")
            blue.stock_qty = Decimal("12")
            blue.reorder_level = Decimal("5")
            red.stock_qty = Decimal("3")
            red.reorder_level = Decimal("4")  # below reorder level on purpose

            order = Order(
                order_number="DEMO-1001",
                supplier="ACME Supplies",
                status="confirmed",
                currency="EUR",
                total_amount=Decimal("115.00"),
                notes="Demo data (no Gmail needed)",
            )
            db.add(order)
            db.flush()
            db.add_all(
                [
                    OrderItem(
                        order_id=order.id,
                        product_id=blue.id,
                        sku="DEMO-BLUE",
                        description="Blue Widget",
                        quantity=Decimal("10"),
                        unit_price=Decimal("10.00"),
                        line_total=Decimal("100.00"),
                    ),
                    OrderItem(
                        order_id=order.id,
                        product_id=red.id,
                        sku="DEMO-RED",
                        description="Red Widget",
                        quantity=Decimal("2"),
                        unit_price=Decimal("7.50"),
                        line_total=Decimal("15.00"),
                    ),
                ]
            )
            db.commit()
        print("Seeded DEMO-1001 with 2 products. Open http://127.0.0.1:8000")
        return 0
    return 1



if __name__ == "__main__":
    raise SystemExit(main())

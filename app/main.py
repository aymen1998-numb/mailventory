from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .config import get_settings
from .db import SessionLocal, get_db, init_db
from .models import (
    Attachment,
    EmailMessage,
    Order,
    OrderItem,
    Product,
    StockMovement,
    SyncRun,
)
from .services import fillable_forms
from .services.credentials import mask_secret, update_env
from .services.gmail_sync import get_running_run, mark_interrupted_runs, start_sync
from .services.imap_client import ImapError, fetch_emails, test_connection
from .services.ingest import ReparseError, ingest_emails, reparse_order
from .services.order_edit import (
    OrderEditError,
    apply_edit,
    blank_rows,
    rows_from_form,
    rows_from_order,
)
from .services.orders import STATUSES, OrderTransitionError, allowed_next, change_status

BASE_DIR = Path(__file__).resolve().parent
settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with SessionLocal() as db:
        mark_interrupted_runs(db)
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "MAD": "DH"}


def fmt_money(value: Decimal | None, currency: str = "EUR") -> str:
    if value is None:
        return "—"
    return f"{Decimal(value):,.2f} {SYMBOLS.get(currency, currency)}"


def fmt_qty(value: Decimal | None) -> str:
    if value is None:
        return "0"
    return f"{Decimal(value):,.2f}".rstrip("0").rstrip(".")


def fmt_datetime(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "—"


def fmt_date(value) -> str:
    return value.isoformat() if value else "—"


templates.env.filters["money"] = fmt_money
templates.env.filters["qty"] = fmt_qty
templates.env.filters["dt"] = fmt_datetime
templates.env.filters["date"] = fmt_date
templates.env.globals["app_name"] = settings.app_name
templates.env.globals["statuses"] = STATUSES
templates.env.globals["allowed_next"] = allowed_next


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), flash: str | None = None):
    open_orders = db.scalar(
        select(func.count(Order.id)).where(Order.status.notin_(("closed", "cancelled")))
    )
    counts = {
        "orders": db.scalar(select(func.count(Order.id))) or 0,
        "open_orders": open_orders or 0,
        "emails": db.scalar(select(func.count(EmailMessage.id))) or 0,
        "products": db.scalar(select(func.count(Product.id))) or 0,
        "low_stock": db.scalar(
            select(func.count(Product.id)).where(
                Product.reorder_level > 0, Product.stock_qty <= Product.reorder_level
            )
        )
        or 0,
    }
    recent_orders = db.scalars(
        select(Order).options(selectinload(Order.items)).order_by(Order.created_at.desc()).limit(8)
    ).all()
    low_stock = db.scalars(
        select(Product)
        .where(Product.reorder_level > 0, Product.stock_qty <= Product.reorder_level)
        .order_by(Product.stock_qty)
        .limit(8)
    ).all()
    cfg = get_settings()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "counts": counts,
            "recent_orders": recent_orders,
            "low_stock": low_stock,
            "imap_configured": cfg.imap_configured,
            "flash": flash,
        },
    )


@app.get("/orders", response_class=HTMLResponse)
def orders_page(
    request: Request,
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    stmt = select(Order).options(selectinload(Order.items)).order_by(Order.created_at.desc())
    if status:
        stmt = stmt.where(Order.status == status)
    orders = db.scalars(stmt).all()
    return templates.TemplateResponse(
        request,
        "orders.html",
        {"orders": orders, "current_status": status or ""},
    )


def _order_or_404(db: Session, order_id: int) -> Order:
    order = db.scalar(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.product))
        .where(Order.id == order_id)
    )
    if order is None:
        raise HTTPException(404, "Order not found")
    return order


def _form_header(order: Order) -> dict[str, str]:
    return {
        "order_number": order.order_number,
        "supplier": order.supplier or "",
        "order_date": order.order_date.isoformat() if order.order_date else "",
        "currency": order.currency,
    }


@app.get("/orders/{order_id}", response_class=HTMLResponse)
def order_detail(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    flash: str | None = None,
):
    order = _order_or_404(db, order_id)
    attachment = db.get(Attachment, order.attachment_id) if order.attachment_id else None
    email = db.get(EmailMessage, order.email_id) if order.email_id else None
    return templates.TemplateResponse(
        request,
        "order_detail.html",
        {
            "order": order,
            "attachment": attachment,
            "email": email,
            "flash": flash,
            "rows": rows_from_order(order),
        },
    )


def _order_edit_page(
    request: Request,
    order: Order,
    *,
    rows: list | None = None,
    form: dict[str, str] | None = None,
    error: str | None = None,
    status_code: int = 200,
):
    return templates.TemplateResponse(
        request,
        "order_edit.html",
        {
            "order": order,
            "rows": rows
            if rows is not None
            else rows_from_order(order) + ([] if order.stock_applied else blank_rows(2)),
            "form": form or _form_header(order),
            "error": error,
        },
        status_code=status_code,
    )


@app.get("/orders/{order_id}/edit", response_class=HTMLResponse)
def order_edit_form(order_id: int, request: Request, db: Session = Depends(get_db)):
    return _order_edit_page(request, _order_or_404(db, order_id))


@app.post("/orders/{order_id}/edit", response_class=HTMLResponse)
async def order_edit_submit(
    order_id: int, request: Request, db: Session = Depends(get_db)
):
    order = _order_or_404(db, order_id)
    submitted = await request.form()
    header = {
        "order_number": str(submitted.get("order_number") or ""),
        "supplier": str(submitted.get("supplier") or ""),
        "order_date": str(submitted.get("order_date") or ""),
        "currency": str(submitted.get("currency") or ""),
    }
    rows = rows_from_form(submitted)

    try:
        apply_edit(db, order, rows=rows, **header)
        db.commit()
    except OrderEditError as exc:
        db.rollback()
        # Keep what was typed so the mistake can be fixed in place.
        return _order_edit_page(
            request, order, rows=rows, form=header, error=str(exc), status_code=400
        )

    flash = "Order updated. Stock was not touched."
    return RedirectResponse(f"/orders/{order.id}?flash={quote(flash)}", status_code=303)


@app.post("/orders/{order_id}/reparse")
def order_reparse(order_id: int, db: Session = Depends(get_db)):
    order = _order_or_404(db, order_id)
    back = f"/orders/{order.id}"

    if order.stock_applied:
        return RedirectResponse(
            back
            + "?flash="
            + quote(
                "Stock for this order is already applied, so it cannot be re-parsed. "
                "Correct the line items by hand instead."
            ),
            status_code=303,
        )
    attachment = db.get(Attachment, order.attachment_id) if order.attachment_id else None
    if attachment is None:
        return RedirectResponse(
            back + "?flash=" + quote("This order has no stored PDF to re-parse."),
            status_code=303,
        )

    try:
        count = reparse_order(db, order, attachment, get_settings())
        db.commit()
        message = f"Re-parsed {attachment.filename}: {count} line item(s)."
    except ReparseError as exc:
        db.rollback()
        message = str(exc)
    return RedirectResponse(back + "?flash=" + quote(message), status_code=303)


@app.post("/orders/{order_id}/status", response_class=HTMLResponse)
def order_status(
    order_id: int,
    request: Request,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    order = db.scalar(
        select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    )
    if order is None:
        raise HTTPException(404, "Order not found")

    error = None
    try:
        change_status(db, order, status)
        db.commit()
    except OrderTransitionError as exc:
        db.rollback()
        error = str(exc)

    return templates.TemplateResponse(
        request, "partials/order_row.html", {"order": order, "error": error}
    )


@app.get("/inventory", response_class=HTMLResponse)
def inventory_page(request: Request, db: Session = Depends(get_db)):
    products = db.scalars(select(Product).order_by(Product.name)).all()
    return templates.TemplateResponse(
        request, "inventory.html", {"products": products}
    )


@app.post("/inventory/{product_id}/adjust", response_class=HTMLResponse)
def adjust_inventory(
    product_id: int,
    request: Request,
    delta: float = Form(...),
    reason: str = Form(default="manual adjustment"),
    db: Session = Depends(get_db),
):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(404, "Product not found")

    change = Decimal(str(delta))
    product.stock_qty = Decimal(product.stock_qty) + change
    db.add(
        StockMovement(
            product_id=product.id,
            delta=change,
            reason=(reason or "manual adjustment")[:100],
        )
    )
    db.commit()
    return templates.TemplateResponse(
        request, "partials/inventory_row.html", {"product": product, "error": None}
    )


@app.post("/inventory/{product_id}/reorder")
def set_reorder_level(
    product_id: int,
    level: float = Form(...),
    db: Session = Depends(get_db),
):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(404, "Product not found")
    product.reorder_level = Decimal(str(level))
    db.commit()
    return RedirectResponse("/inventory", status_code=303)


@app.get("/emails", response_class=HTMLResponse)
def emails_page(request: Request, db: Session = Depends(get_db)):
    emails = db.scalars(
        select(EmailMessage)
        .options(selectinload(EmailMessage.attachments), selectinload(EmailMessage.orders))
        .order_by(EmailMessage.created_at.desc())
        .limit(100)
    ).all()
    return templates.TemplateResponse(request, "emails.html", {"emails": emails})


@app.get("/emails/{email_id}", response_class=HTMLResponse)
def email_detail(
    email_id: int,
    request: Request,
    db: Session = Depends(get_db),
    flash: str | None = None,
):
    email = db.scalar(
        select(EmailMessage)
        .options(selectinload(EmailMessage.attachments), selectinload(EmailMessage.orders))
        .where(EmailMessage.id == email_id)
    )
    if email is None:
        raise HTTPException(404, "Email not found")
    cfg = get_settings()
    fillable_ids = {
        attachment.id
        for attachment in email.attachments
        if fillable_forms.fillable_path(Path(attachment.path)).exists()
    }
    return templates.TemplateResponse(
        request,
        "email_detail.html",
        {
            "email": email,
            "flash": flash,
            "fillable_ids": fillable_ids,
            "commonforms_enabled": cfg.commonforms_enabled,
            "commonforms_installed": fillable_forms.is_available(cfg.commonforms_command),
        },
    )


def _attachment_or_404(db: Session, attachment_id: int) -> Attachment:
    attachment = db.get(Attachment, attachment_id)
    if attachment is None:
        raise HTTPException(404, "Attachment not found")
    return attachment


def _pdf_or_404(path: Path) -> Path:
    if not path.exists():
        raise HTTPException(404, "PDF is missing from disk")
    return path


@app.get("/attachments/{attachment_id}/file")
def attachment_file(attachment_id: int, db: Session = Depends(get_db)):
    attachment = _attachment_or_404(db, attachment_id)
    path = _pdf_or_404(Path(attachment.path))
    return FileResponse(path, media_type="application/pdf", filename=attachment.filename)


@app.get("/attachments/{attachment_id}/fillable-form")
def download_fillable_form(attachment_id: int, db: Session = Depends(get_db)):
    attachment = _attachment_or_404(db, attachment_id)
    path = _pdf_or_404(fillable_forms.fillable_path(Path(attachment.path)))
    return FileResponse(path, media_type="application/pdf", filename=path.name)


@app.post("/attachments/{attachment_id}/fillable-form")
def create_fillable_form(attachment_id: int, db: Session = Depends(get_db)):
    cfg = get_settings()
    attachment = _attachment_or_404(db, attachment_id)
    back = f"/emails/{attachment.email_id}"

    if not cfg.commonforms_enabled:
        return RedirectResponse(
            back
            + "?flash="
            + quote("Fillable forms are off. Enable them under Settings, in section 3."),
            status_code=303,
        )

    try:
        source = _pdf_or_404(Path(attachment.path))
        target = fillable_forms.prepare_fillable_form(source, command=cfg.commonforms_command)
        message = f"Fillable form ready: {target.name}"
    except fillable_forms.CommonFormsError as exc:
        message = str(exc)
    except HTTPException:
        raise
    except Exception as exc:  # keep the page usable if the tool misbehaves
        message = f"Could not build the fillable form: {exc}"

    return RedirectResponse(back + "?flash=" + quote(message), status_code=303)


@app.post("/ingest")
def ingest_now(db: Session = Depends(get_db)):
    cfg = get_settings()
    if not cfg.imap_configured:
        return RedirectResponse(
            "/settings?flash="
            + quote("Add your Gmail address and app password in Settings first."),
            status_code=303,
        )
    try:
        emails = fetch_emails(cfg)
        report = ingest_emails(db, emails, cfg)
        message = report.summary()
        if report.errors:
            message += " | " + "; ".join(report.errors[:2])
    except ImapError as exc:
        message = f"IMAP error: {exc}"
    return RedirectResponse("/?flash=" + quote(message), status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: Session = Depends(get_db),
    flash: str | None = None,
):
    cfg = get_settings()
    last_run = db.scalar(select(SyncRun).order_by(SyncRun.id.desc()))
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "cfg": cfg,
            "password_masked": mask_secret(cfg.gmail_app_password),
            "last_run": last_run,
            "running": get_running_run(db) is not None,
            "flash": flash,
            "env_path": BASE_DIR.parent / ".env",
            "commonforms_installed": fillable_forms.is_available(cfg.commonforms_command),
        },
    )


@app.post("/settings")
def save_settings(
    gmail_address: str = Form(""),
    gmail_app_password: str = Form(""),
    imap_mailbox: str = Form("INBOX"),
    sync_mailbox: str = Form(""),
    gmail_query: str = Form("has:attachment filename:pdf"),
    sync_max_emails: int = Form(0),
):
    address = gmail_address.strip()
    if not address:
        return RedirectResponse(
            "/settings?flash=" + quote("Gmail address is required."), status_code=303
        )

    updates = {
        "GMAIL_ADDRESS": address,
        "IMAP_MAILBOX": imap_mailbox.strip() or "INBOX",
        "SYNC_MAILBOX": sync_mailbox.strip(),
        "GMAIL_QUERY": gmail_query.strip(),
        "SYNC_MAX_EMAILS": str(max(sync_max_emails, 0)),
    }
    password = gmail_app_password.replace(" ", "").strip()
    if password:
        updates["GMAIL_APP_PASSWORD"] = password

    update_env(BASE_DIR.parent / ".env", updates)
    get_settings.cache_clear()
    return RedirectResponse(
        "/settings?flash="
        + quote("Saved locally. Use Test connection, then start a sync."),
        status_code=303,
    )


@app.post("/settings/commonforms")
def save_commonforms_settings(
    enabled: str = Form(default=""),
    command: str = Form("commonforms"),
):
    updates = {
        "COMMONFORMS_ENABLED": "true" if enabled else "false",
        "COMMONFORMS_COMMAND": command.strip() or "commonforms",
    }
    update_env(BASE_DIR.parent / ".env", updates)
    get_settings.cache_clear()
    return RedirectResponse(
        "/settings?flash="
        + quote("Fillable form settings saved."),
        status_code=303,
    )


@app.post("/settings/test")
def test_settings():
    cfg = get_settings()
    if not cfg.imap_configured:
        message = "Save your Gmail address and app password first."
    else:
        try:
            message = test_connection(cfg)
        except Exception as exc:
            message = f"Connection failed: {exc}"
    return RedirectResponse("/settings?flash=" + quote(message), status_code=303)


@app.post("/sync")
def sync_now(
    mode: str = Form("new"),
    db: Session = Depends(get_db),
):
    cfg = get_settings()
    if not cfg.imap_configured:
        return RedirectResponse(
            "/settings?flash=" + quote("Save Gmail credentials before syncing."),
            status_code=303,
        )
    if get_running_run(db) is not None:
        return RedirectResponse(
            "/settings?flash=" + quote("A sync is already running."), status_code=303
        )
    try:
        start_sync("all" if mode == "all" else "new")
        message = "Sync started. Progress updates every few seconds."
    except RuntimeError as exc:
        message = str(exc)
    return RedirectResponse("/settings?flash=" + quote(message), status_code=303)


@app.get("/sync/status", response_class=HTMLResponse)
def sync_status(request: Request, db: Session = Depends(get_db)):
    run = db.scalar(select(SyncRun).order_by(SyncRun.id.desc()))
    return templates.TemplateResponse(
        request,
        "partials/sync_status.html",
        {
            "run": run,
            "running": get_running_run(db) is not None,
            "cfg": get_settings(),
        },
    )


@app.get("/guide", response_class=HTMLResponse)
def guide_page(request: Request):
    return templates.TemplateResponse(request, "guide.html", {})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

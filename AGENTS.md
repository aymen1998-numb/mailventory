# AGENTS.md

Local-only, single-user FastAPI app: Gmail IMAP → PDF attachments → heuristic
order parser → SQLite inventory/orders. No cloud backend, no JS build step.

## Commands

- Setup: `python -m venv .venv`, `.venv\Scripts\Activate.ps1`, `pip install -e ".[dev]"`
- Run: `uvicorn app.main:app --reload --host 127.0.0.1 --port 8000` (creates tables on startup)
- Tests: `pytest`; one test: `pytest tests/test_pdf_parser.py::test_parses_order_header_and_items`
- Quick ingest: `python -m app.cli ingest --limit 5`
- Full sync: `python -m app.cli sync --all` (omit `--all` to resume from checkpoint)
- Demo data without Gmail: `python -m app.cli seed-demo` (creates `DEMO-1001` + products)
- OCR extras: `pip install -e ".[ocr]"` plus Tesseract/Poppler on PATH, then `OCR_ENABLED=true`

## Architecture

- `app/main.py` holds every HTTP route and Jinja filter. UI is server-rendered;
  HTMX is vendored at `app/static/htmx.min.js` — do not reintroduce a CDN or a
  frontend build tool.
- Gmail credentials are entered at `/settings`, stored in `.env` by
  `services/credentials.py`, and read through `get_settings()` (call
  `cache_clear()` after writes). Never return the raw password to a template.
- `services/imap_client.py` opens mail read-only, auto-detects Gmail's `\All`
  mailbox, searches with `X-GM-RAW`, and fetches by UID.
- `services/gmail_sync.py` runs one background thread per sync, updates a
  `SyncRun` row for the HTMX progress bar, and checkpoints `SyncState.last_uid`.
  `mode="all"` resets the checkpoint; `UIDVALIDITY` changes reset it automatically.
  Stale runs are marked failed at startup.
- `services/pdf_parser.py` is deliberately regex/heuristic (pdfplumber text and
  ruled tables). Supplier layouts differ: change the patterns and add a case to
  `tests/test_pdf_parser.py` instead of special-casing one PDF in the UI.
  `extract_pdf()` returns text **and** `page.extract_tables()` output; a table is
  only used when `_is_item_header()` sees description + quantity + price/total
  columns, and matched table rows then replace flat-text line parsing. New column
  vocabulary goes in `TABLE_HEADER_HINTS`, with a case in `tests/test_pdf_tables.py`.
- `services/fillable_forms.py` shells out to an externally installed `commonforms`
  command to write `<name>-fillable.pdf` next to an attachment. Upstream has no
  license file, so **never** vendor, import or pin that package here; the feature is
  gated by `COMMONFORMS_ENABLED` and never runs during ingestion.
- Ingestion saves PDFs to `data/attachments/email-<id>/`, creates products from
  line items, and generates `MAIL-<email>-<n>` when no order number is found.
- `services/orders.py` is the status machine. Stock increases exactly once when
  status becomes `received`, guarded by `Order.stock_applied`. Never adjust stock
  during ingestion.
- Config comes from `.env` via `app/config.py`; DB is `data/orderdesk.db`, both
  gitignored with `data/attachments/`.

## Gotchas

- Sync uses an in-process thread and lock. Run a single uvicorn worker
  (`--workers 1`); SQLite is in WAL mode for reader/writer overlap.
- No migrations: `init_db()` only runs `create_all`. Schema changes require
  deleting `data/` in dev or introducing a migration tool.
- Gmail needs an app password (2FA enabled); the account password fails. The
  password is stored in plain text in `.env` — keep it local and revocable.
- Ingestion is read-only, so messages stay unread and may appear on every run —
  dedupe is by `Message-ID`, not IMAP flags.
- Default Gmail query is `has:attachment filename:pdf`. An empty query scans every
  message, which is much slower and stores every email body locally.
- Scanned PDFs become `needs_ocr`, not parse errors. OCR is optional.
- The optional `commonforms` bridge writes a second PDF beside the original. Do not
  turn it into a parse path: it returns empty fields, not values.
- Local-only by design: bind `127.0.0.1`; never expose the app publicly.
- Tests use in-memory SQLite. `tests/conftest.py` imports `app.db`, which creates
  the `data/` directory, but tests never touch the real database.

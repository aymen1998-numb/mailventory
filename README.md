# Local Order Desk

A local-only, single-user "SaaS" for turning Gmail purchase-order emails into
tracked orders and inventory. Everything runs on your machine: no cloud backend,
no third-party parsing APIs, no frontend build step.

**Pipeline:** Gmail IMAP → PDF attachments → text layer **and ruled tables** →
heuristic order parser → SQLite orders, products and stock movements → HTMX UI.

## Features

- Read-only Gmail ingestion over IMAP with app passwords
- In-app Gmail login/settings page, stored locally in `.env`
- Full-mailbox background scan with progress, resumable by IMAP UID
- Stores PDFs locally under `data/attachments/`
- Table-aware parsing: ruled line-item tables win over flat-text matching
- Heuristic parsing of order number, supplier, date, line items and totals
- Product catalogue created automatically from parsed line items
- Order workflow: `new → confirmed → shipped → received → closed` (or `cancelled`)
- Stock is increased exactly once, when an order is marked `received`
- Dashboard, order table, inventory with manual adjustments, email/PDF viewer
- Optional OCR hook for scanned PDFs (Tesseract + Poppler)
- Optional “make this PDF fillable” button via an isolated `commonforms` install

## Quickstart

Requires Python 3.11+.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/settings>, enter your Gmail address and app password,
click **Test connection**, then **Sync new PDF mail** (or **Re-scan all PDF mail**
for the whole mailbox). Progress updates live on the same page.

To preview the UI without Gmail, seed demo data:

```powershell
python -m app.cli seed-demo
```

Tables are created automatically on first start in `data/orderdesk.db`.

## Gmail setup

1. Enable 2-step verification on the Google account.
2. Create an app password at <https://myaccount.google.com/apppasswords>.
3. Enter it on the **Settings** page. The app writes it to `.env` locally; you can
   also add it manually:

   ```dotenv
   GMAIL_ADDRESS=you@gmail.com
   GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
   ```

The mailbox is opened read-only, so syncing does not mark messages as read.
Duplicates are skipped by `Message-ID`.

## Scanning all mail

The Settings page offers two actions:

- **Sync new PDF mail** continues from the last processed IMAP UID.
- **Re-scan all PDF mail** resets the checkpoint and walks the whole mailbox.

The sync runs in a background thread and shows a progress bar. It auto-detects
Gmail's **All Mail** folder (using the `\All` special-use flag), so archived mail
is included and localized folder names work. A per-mailbox `UIDVALIDITY`
fingerprint resets the checkpoint safely if Gmail rebuilds the mailbox.

The default Gmail query is `has:attachment filename:pdf`. Clear it in **Advanced**
to scan every message (slower, and every email body is stored locally). Set
`SYNC_MAX_EMAILS=0` for no per-run cap; a positive value resumes on the next run.

CLI equivalent:

```powershell
python -m app.cli sync          # new mail only
python -m app.cli sync --all    # re-scan everything
```

## OCR for scanned PDFs

```powershell
pip install -e ".[ocr]"
# also install the Tesseract and Poppler binaries and put them on PATH
```

Then set `OCR_ENABLED=true` in `.env`. Without OCR, scanned PDFs are stored and
marked `needs_ocr` instead of failing.

## Fillable forms (optional)

The **Make fillable** button on an email's attachment turns a source PDF into a
copy with empty, clickable form fields. It is useful when a supplier sends a blank
order form you want to fill by hand.

This is **not** OCR and it does not extract values: parsing stays local and rule
based, and fillable copies are only ever an extra file next to the original.

The work is done by the separate
[commonforms](https://github.com/jbarrow/commonforms) command, which you install
yourself, outside this project:

```powershell
pipx install commonforms
```

Then enable it under **Settings → Fillable forms**. The default command name is
`commonforms`; override it with `COMMONFORMS_COMMAND` if `pipx` put it elsewhere.

Two deliberate constraints:

- **Nothing from commonforms is vendored, imported or pinned here.** Upstream has
  no license file at the time of writing, so this repository only calls its CLI.
- **It is off by default** and never runs during ingestion. Detection needs
  `torch`, `transformers`, `rfdetr` and friends, plus a model download on first use.

## Parser tuning

The parser is intentionally rule based (`app/services/pdf_parser.py`). Real
supplier layouts vary, so:

1. Import one email from the supplier.
2. Open **Emails → attachment → Extracted text** to see what pdfplumber read.
3. Adjust `ORDER_NUMBER_RE`, `DATE_RE`, `TOTAL_RE` or `ITEM_LINE_RE`.
4. Add a case to `tests/test_pdf_parser.py` so the layout stays supported.

For PDFs with ruled line-item tables, `extract_pdf()` also returns
`page.extract_tables()` output. A table counts as a line-item table only when its
header row has a description **and** a quantity column plus a price or total
column, so address blocks and signature boxes are ignored. Matching columns are
listed in `TABLE_HEADER_HINTS`; extend that dict (and
`tests/test_pdf_tables.py`) for a new supplier vocabulary. When a table parses,
its rows replace flat-text line matching.

Orders without a detectable order number get a generated `MAIL-<email>-<n>` id.

## Tests

```powershell
pytest
pytest tests/test_pdf_parser.py::test_parses_order_header_and_items
```

## Project layout

```
app/
  main.py                 # FastAPI routes, Jinja filters
  config.py               # .env-backed settings
  db.py                   # SQLite engine, create_all
  models.py               # emails, attachments, products, orders, stock, sync state
  cli.py                  # ingest / sync / seed-demo commands
  services/
    imap_client.py        # read-only IMAP, All Mail detection, UID search/fetch
    gmail_sync.py         # background sync runs + progress
    credentials.py        # safe .env updates
    pdf_parser.py         # pdfplumber text + tables, regex order parsing, OCR hook
    fillable_forms.py     # optional bridge to the external commonforms CLI
    ingest.py             # persistence: email -> attachment -> order -> products
    orders.py             # status machine + stock application
    suppliers.py
  templates/              # Jinja + HTMX fragments (settings + sync status)
  static/                 # CSS and vendored htmx.min.js
tests/
data/                     # SQLite DB + attachments (gitignored)
```

## Notes

- This is a local tool: bind it to `127.0.0.1` and do not expose it publicly.
- `.env`, `data/`, and the database are gitignored. The app password is stored in
  plain text in `.env`; use a dedicated app password you can revoke at any time.
- Sync uses a daemon thread, so run a single uvicorn process (no `--workers`).
- Fillable forms never run during ingestion and stay off unless
  `COMMONFORMS_ENABLED=true`. Upstream `commonforms` has no license file, so it is
  called as an external command and never copied, imported or pinned here.
- Schema changes use `create_all` only (no migrations yet); delete `data/` in
  development or add a migration tool before evolving the schema.

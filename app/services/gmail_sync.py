from __future__ import annotations

import threading
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal
from ..models import SyncRun, SyncState
from . import imap_client
from .ingest import ingest_emails

_start_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def get_running_run(db: Session) -> SyncRun | None:
    return db.scalar(
        select(SyncRun)
        .where(SyncRun.status.in_(("queued", "running")))
        .order_by(SyncRun.id.desc())
    )


def mark_interrupted_runs(db: Session) -> None:
    runs = db.scalars(
        select(SyncRun).where(SyncRun.status.in_(("queued", "running")))
    ).all()
    for run in runs:
        run.status = "failed"
        run.message = "Interrupted by app restart"
        run.finished_at = _now()
    if runs:
        db.commit()


def start_sync(mode: str) -> int:
    with _start_lock:
        with SessionLocal() as db:
            if get_running_run(db) is not None:
                raise RuntimeError("A sync is already running")
            run = SyncRun(
                status="queued",
                mode=mode,
                total=0,
                processed=0,
                imported=0,
                errors=0,
            )
            db.add(run)
            db.commit()
            run_id = run.id
    threading.Thread(
        target=_run_sync,
        args=(run_id,),
        daemon=True,
        name=f"gmail-sync-{run_id}",
    ).start()
    return run_id


def _run_sync(run_id: int) -> None:
    settings = get_settings()
    with SessionLocal() as db:
        run = db.get(SyncRun, run_id)
        if run is None:
            return
        run.status = "running"
        run.started_at = _now()
        db.commit()

        try:
            with imap_client.imap_session(settings) as client:
                mailbox = imap_client.resolve_sync_mailbox(client, settings)
                uidvalidity = imap_client.select_mailbox(client, mailbox)

                state = db.scalar(select(SyncState).where(SyncState.mailbox == mailbox))
                if state is None:
                    state = SyncState(mailbox=mailbox)
                    db.add(state)
                    db.flush()
                if state.uidvalidity != uidvalidity:
                    state.uidvalidity = uidvalidity
                    state.last_uid = 0
                if run.mode == "all":
                    state.last_uid = 0
                db.commit()

                uids = imap_client.search_uids(
                    client, settings, gmail_query=settings.gmail_query, fallback="ALL"
                )
                if run.mode != "all":
                    uids = [uid for uid in uids if uid > state.last_uid]
                if settings.sync_max_emails > 0:
                    uids = uids[: settings.sync_max_emails]

                run.total = len(uids)
                run.message = f"Scanning {mailbox}"
                db.commit()

                for uid in uids:
                    email = imap_client.fetch_email(client, uid, settings)
                    if email is not None:
                        report = ingest_emails(db, [email], settings)
                        run.imported += report.orders_created
                        run.errors += len(report.errors)
                    state.last_uid = max(state.last_uid, uid)
                    run.processed += 1
                    run.message = f"{run.processed}/{run.total} emails scanned"
                    db.commit()

            run.status = "completed"
            run.finished_at = _now()
            run.message = (
                f"Done: {run.processed} email(s) scanned, "
                f"{run.imported} order(s) imported."
            )
            db.commit()
        except Exception as exc:  # keep the error visible in the UI
            db.rollback()
            run = db.get(SyncRun, run_id)
            if run is not None:
                run.status = "failed"
                run.message = str(exc)[:2000]
                run.finished_at = _now()
                db.commit()

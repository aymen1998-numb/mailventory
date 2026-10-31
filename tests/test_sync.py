from app.models import SyncRun
from app.services.gmail_sync import get_running_run, mark_interrupted_runs


def test_mark_interrupted_runs_fails_stale_jobs(db):
    db.add_all(
        [
            SyncRun(status="running", mode="all"),
            SyncRun(status="queued", mode="new"),
            SyncRun(status="completed", mode="new"),
        ]
    )
    db.commit()

    mark_interrupted_runs(db)
    db.expire_all()

    statuses = [run.status for run in db.query(SyncRun).order_by(SyncRun.id)]
    assert statuses == ["failed", "failed", "completed"]
    assert get_running_run(db) is None

from app.config import Settings
from app.services.imap_client import FetchedEmail
from app.services.ingest import ingest_emails


def test_duplicate_message_ids_are_skipped(db, tmp_path):
    settings = Settings(attachment_dir=tmp_path)
    fetched = FetchedEmail(
        message_id="<abc@example.com>",
        subject="Order",
        sender="ACME <orders@acme.test>",
        received_at=None,
        body_text="see attached",
    )

    first = ingest_emails(db, [fetched], settings)
    second = ingest_emails(db, [fetched], settings)

    assert first.emails_new == 1
    assert second.emails_new == 0
    assert second.duplicates == 1

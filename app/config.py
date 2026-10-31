from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Local Order Desk"
    database_url: str = f"sqlite:///{(DATA_DIR / 'orderdesk.db').as_posix()}"

    # Gmail over IMAP: requires 2-step verification + a Google app password.
    gmail_address: str = ""
    gmail_app_password: str = ""
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_mailbox: str = "INBOX"
    imap_search: str = "UNSEEN"
    imap_fetch_limit: int = 20

    # Full-mailbox sync: empty sync_mailbox means auto-detect Gmail's All Mail.
    sync_mailbox: str = ""
    gmail_query: str = "has:attachment filename:pdf"
    sync_max_emails: int = 2000

    attachment_dir: Path = DATA_DIR / "attachments"
    max_attachment_mb: int = 20

    # Scanned PDFs stay unparsed until OCR extras are installed (see README).
    ocr_enabled: bool = False

    # Optional: shell out to an isolated `commonforms` install to turn an
    # attachment into a PDF with empty fillable fields. Nothing is vendored, and
    # upstream has no license file, so this stays opt-in. See README.
    commonforms_enabled: bool = False
    commonforms_command: str = "commonforms"

    @property
    def imap_configured(self) -> bool:
        return bool(self.gmail_address and self.gmail_app_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()

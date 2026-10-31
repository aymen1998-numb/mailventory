"""Minimal Gmail-over-IMAP reader.

Gmail app passwords require 2-step verification and are revoked when the Google
account password changes. Use a dedicated app password, never the account one.
"""

from __future__ import annotations

import hashlib
import html
import imaplib
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime

from ..config import Settings


class ImapError(RuntimeError):
    pass


@dataclass
class FetchedAttachment:
    filename: str
    content_type: str
    data: bytes


@dataclass
class FetchedEmail:
    message_id: str
    subject: str
    sender: str
    received_at: datetime | None
    body_text: str
    attachments: list[FetchedAttachment] = field(default_factory=list)


@contextmanager
def imap_session(settings: Settings):
    if not settings.imap_configured:
        raise ImapError(
            "Gmail IMAP is not configured. Add your address and app password first."
        )

    client = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=30)
    try:
        try:
            client.login(settings.gmail_address, settings.gmail_app_password)
        except imaplib.IMAP4.error as exc:
            raise ImapError(f"Gmail login failed: {exc}") from exc
        yield client
    finally:
        try:
            client.logout()
        except imaplib.IMAP4.error:
            pass


def quote_mailbox(name: str) -> str:
    if name.startswith('"') and name.endswith('"'):
        return name
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def select_mailbox(client: imaplib.IMAP4, name: str) -> int:
    status, _ = client.select(quote_mailbox(name), readonly=True)
    if status != "OK":
        raise ImapError(f"Cannot open mailbox {name!r}")
    _, values = client.response("UIDVALIDITY")
    try:
        return int(values[0]) if values and values[0] else 0
    except (TypeError, ValueError):
        return 0


def list_mailboxes(client: imaplib.IMAP4) -> list[tuple[str, str]]:
    status, data = client.list()
    if status != "OK":
        return []
    mailboxes: list[tuple[str, str]] = []
    for raw in data:
        if not isinstance(raw, (bytes, bytearray)):
            continue
        line = raw.decode("utf-8", errors="replace")
        match = re.match(r"\((?P<flags>[^)]*)\)\s+(?:\"[^\"]*\"|NIL)\s+(?P<name>.+)$", line)
        if not match:
            continue
        name = match.group("name").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]
        mailboxes.append((name, match.group("flags")))
    return mailboxes


def resolve_sync_mailbox(client: imaplib.IMAP4, settings: Settings) -> str:
    """Explicit setting wins; otherwise use Gmail's ``\\All`` mail folder."""
    if settings.sync_mailbox.strip():
        return settings.sync_mailbox.strip()
    for name, flags in list_mailboxes(client):
        if "\\All" in flags:
            return name
    return settings.imap_mailbox or "INBOX"


def search_uids(
    client: imaplib.IMAP4,
    settings: Settings,
    *,
    gmail_query: str | None = None,
    fallback: str | None = None,
) -> list[int]:
    if gmail_query and "gmail" in settings.imap_host.lower():
        query = gmail_query.replace('"', "").strip()
        status, data = client.uid("search", None, "X-GM-RAW", f'"{query}"')
    else:
        status, data = client.uid("search", None, fallback or settings.imap_search)
    if status != "OK":
        raise ImapError(f"IMAP search failed: {status}")
    if not data or not data[0]:
        return []
    return sorted(int(part) for part in data[0].split() if part.isdigit())


def fetch_email(
    client: imaplib.IMAP4, uid: int, settings: Settings
) -> FetchedEmail | None:
    status, payload = client.uid("fetch", str(uid), "(RFC822)")
    if status != "OK":
        return None
    raw = next(
        (
            part[1]
            for part in payload
            if isinstance(part, tuple) and isinstance(part[1], bytes)
        ),
        None,
    )
    if raw is None:
        return None
    return _parse_message(raw, settings.max_attachment_mb * 1024 * 1024)


def fetch_emails(settings: Settings, *, limit: int | None = None) -> list[FetchedEmail]:
    """Incremental (UNSEEN by default) fetch used by ``app.cli ingest``."""
    limit = limit or settings.imap_fetch_limit
    emails: list[FetchedEmail] = []
    with imap_session(settings) as client:
        select_mailbox(client, settings.imap_mailbox)
        uids = search_uids(client, settings)
        for uid in reversed(uids[-limit:]):
            email = fetch_email(client, uid, settings)
            if email is not None:
                emails.append(email)
    return emails


def test_connection(settings: Settings) -> str:
    with imap_session(settings) as client:
        mailbox = resolve_sync_mailbox(client, settings)
        select_mailbox(client, mailbox)
        uids = search_uids(client, settings, gmail_query=settings.gmail_query, fallback="ALL")
        cap = settings.sync_max_emails
        limit_text = "no limit" if cap <= 0 else f"up to {cap}"
        return (
            f"Connected. {len(uids)} matching email(s) in {mailbox!r}; "
            f"next sync: {limit_text}."
        )


def _parse_message(raw: bytes, max_bytes: int) -> FetchedEmail:
    message = message_from_bytes(raw)
    message_id = (message.get("Message-ID") or "").strip()
    if not message_id:
        message_id = f"<sha256-{hashlib.sha256(raw).hexdigest()}>"

    return FetchedEmail(
        message_id=message_id,
        subject=_decode_header(message.get("Subject", "")),
        sender=_decode_header(message.get("From", "")),
        received_at=_parse_date(message.get("Date")),
        body_text=_extract_body(message),
        attachments=_extract_pdfs(message, max_bytes),
    )


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError):
        return value


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _extract_body(message: Message) -> str:
    plain: list[str] = []
    html_parts: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() != "text":
            continue
        disposition = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        if part.get_content_subtype() == "plain":
            plain.append(text)
        else:
            html_parts.append(text)

    if plain:
        return "\n".join(plain).strip()
    if html_parts:
        stripped = re.sub(r"<[^>]+>", " ", "\n".join(html_parts))
        return re.sub(r"[ \t]+", " ", html.unescape(stripped)).strip()
    return ""


def _extract_pdfs(message: Message, max_bytes: int) -> list[FetchedAttachment]:
    attachments: list[FetchedAttachment] = []
    for part in message.walk():
        filename = part.get_filename()
        content_type = (part.get_content_type() or "").lower()
        is_pdf = content_type == "application/pdf" or (
            filename and filename.lower().endswith(".pdf")
        )
        if not is_pdf:
            continue
        payload = part.get_payload(decode=True) or b""
        if not payload or len(payload) > max_bytes:
            continue
        attachments.append(
            FetchedAttachment(
                filename=_decode_header(filename) or "attachment.pdf",
                content_type="application/pdf",
                data=payload,
            )
        )
    return attachments

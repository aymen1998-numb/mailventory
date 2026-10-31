import imaplib

import pytest

from app.config import Settings
from app.services import imap_client


def _settings() -> Settings:
    return Settings(gmail_address="you@gmail.com", gmail_app_password="abcd" * 4)


def test_invalid_credentials_points_at_app_passwords():
    message = imap_client.describe_login_error(
        imaplib.IMAP4.error("[ALERT] Invalid credentials (Failure)")
    )

    assert "Invalid credentials" in message
    assert "app password" in message
    assert "myaccount.google.com/apppasswords" in message


def test_unknown_login_error_is_passed_through():
    message = imap_client.describe_login_error(imaplib.IMAP4.error("something new"))

    assert message == "something new"


def test_imap_session_wraps_login_failures(monkeypatch):
    class FakeClient:
        def login(self, user, password):
            raise imaplib.IMAP4.error("[ALERT] Invalid credentials (Failure)")

        def logout(self):
            pass

    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda *a, **k: FakeClient())

    with pytest.raises(imap_client.ImapError) as excinfo:
        with imap_client.imap_session(_settings()):
            pass

    assert "Gmail login failed" in str(excinfo.value)
    assert "app password" in str(excinfo.value)


def test_unreachable_host_is_explained(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("[Errno 11001] getaddrinfo failed")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", boom)

    with pytest.raises(imap_client.ImapError) as excinfo:
        with imap_client.imap_session(_settings()):
            pass

    assert "Could not reach imap.gmail.com:993" in str(excinfo.value)


def test_missing_credentials_are_reported_before_connecting(monkeypatch):
    def fail_if_called(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("should not open a connection")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", fail_if_called)

    with pytest.raises(imap_client.ImapError) as excinfo:
        with imap_client.imap_session(Settings(gmail_address="", gmail_app_password="")):
            pass

    assert "not configured" in str(excinfo.value)

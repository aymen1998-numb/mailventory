from app.services.credentials import mask_secret, update_env


def test_update_env_preserves_comments_and_other_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# local settings\nGMAIL_ADDRESS=old@example.com\nFOO=bar\n",
        encoding="utf-8",
    )

    update_env(
        env,
        {
            "GMAIL_ADDRESS": "new@example.com",
            "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop",
            "SYNC_MAILBOX": "",
        },
    )

    text = env.read_text(encoding="utf-8")
    assert "# local settings" in text
    assert "GMAIL_ADDRESS=new@example.com" in text
    assert 'GMAIL_APP_PASSWORD="abcd efgh ijkl mnop"' in text
    assert "FOO=bar" in text
    assert "SYNC_MAILBOX=" in text


def test_update_env_creates_missing_file(tmp_path):
    env = tmp_path / ".env"
    update_env(env, {"GMAIL_ADDRESS": "me@example.com"})
    assert env.read_text(encoding="utf-8") == "GMAIL_ADDRESS=me@example.com\n"


def test_mask_secret_keeps_only_edges():
    assert mask_secret("") == ""
    assert mask_secret("abcd") == "****"
    assert mask_secret("abcdefgh") == "ab****gh"

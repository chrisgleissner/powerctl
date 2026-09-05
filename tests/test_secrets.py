from __future__ import annotations

import json
import os
import stat

import pytest

from powerctl.errors import PowerctlError
from powerctl.secrets import (
    Credentials,
    Redactor,
    credentials_path,
    forget_credentials,
    load_credentials,
    scrub,
    store_credentials,
)


def test_env_credentials_take_precedence(monkeypatch):
    store_credentials("fake", "file-user", "file-pass")
    monkeypatch.setenv("POWERCTL_FAKE_USERNAME", "env-user")
    monkeypatch.setenv("POWERCTL_FAKE_PASSWORD", "env-pass")
    creds = load_credentials("fake")
    assert creds is not None
    assert creds.username == "env-user"
    assert creds.source.startswith("env:")


def test_stored_file_is_owner_only():
    path = store_credentials("fake", "user@example.com", "hunter2hunter2")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["fake"]["username"] == "user@example.com"


def test_wide_open_credential_file_is_rejected():
    path = store_credentials("fake", "user", "password123")
    os.chmod(path, 0o644)
    with pytest.raises(PowerctlError, match="readable by group or other"):
        load_credentials("fake")


def test_forget_removes_only_that_backend():
    store_credentials("fake", "user", "password123")
    store_credentials("other", "user2", "password456")
    assert forget_credentials("fake") is True
    data = json.loads(credentials_path().read_text())
    assert "fake" not in data
    assert "other" in data


def test_credentials_never_repr_the_password():
    creds = Credentials("user", "super-secret-value")
    assert "super-secret-value" not in repr(creds)
    assert "super-secret-value" not in str(creds)
    assert "super-secret-value" not in f"{creds}"


def test_redactor_replaces_known_secret():
    redactor = Redactor()
    redactor.add("super-secret-value")
    assert redactor("token=super-secret-value") == "token=***"


def test_redactor_ignores_very_short_values():
    redactor = Redactor()
    redactor.add("ab")
    assert redactor("ab cd") == "ab cd"


def test_scrub_drops_credential_keys():
    payload = {
        "host": "192.0.2.10",
        "credentials": {"username": "u", "password": "p"},
        "connection_type": {"encryption_type": "KLAP"},
        "nested": [{"credentials_hash": "abc"}],
    }
    cleaned = scrub(payload)
    assert cleaned["credentials"] == "***"
    assert cleaned["nested"][0]["credentials_hash"] == "***"
    assert cleaned["connection_type"] == {"encryption_type": "KLAP"}

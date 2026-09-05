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


def test_config_home_follows_the_environment(monkeypatch, tmp_path):
    from powerctl.secrets import config_home

    monkeypatch.delenv("POWERCTL_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_home() == tmp_path / "powerctl"

    monkeypatch.setenv("POWERCTL_HOME", str(tmp_path / "explicit"))
    assert config_home() == tmp_path / "explicit"


def test_credentials_fall_back_to_another_scope():
    store_credentials("kasa", "user@example.com", "password123")
    creds = load_credentials("tplink")
    assert creds is not None and creds.username == "user@example.com"


def test_credentials_of_an_unknown_scope_are_none():
    store_credentials("kasa", "user@example.com", "password123")
    assert load_credentials("other", fallbacks=()) is None


def test_incomplete_entry_is_an_error():
    import json

    path = store_credentials("kasa", "user@example.com", "password123")
    path.write_text(json.dumps({"kasa": {"username": "user@example.com"}}))
    with pytest.raises(PowerctlError, match="lacks username or password"):
        load_credentials("kasa", fallbacks=())


def test_unreadable_credential_file_is_an_error():
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    path.chmod(0o600)
    with pytest.raises(PowerctlError, match="cannot read"):
        load_credentials("kasa")


def test_a_corrupt_file_is_replaced_on_write():
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    path.chmod(0o600)
    store_credentials("kasa", "user@example.com", "password123")
    assert load_credentials("kasa") is not None


def test_forgetting_an_unknown_scope():
    store_credentials("kasa", "user@example.com", "password123")
    assert forget_credentials("nothing") is False


def test_forgetting_without_a_file():
    assert forget_credentials("kasa") is False


def test_redactor_registers_a_credential_password():
    from powerctl.secrets import Redactor

    redactor = Redactor()
    redactor.add_credentials(None)
    redactor.add_credentials(Credentials("user", "password123"))
    assert redactor("password123") == "***"


def test_scrub_leaves_other_values_alone():
    assert scrub(42) == 42
    assert scrub(["a", 1]) == ["a", 1]

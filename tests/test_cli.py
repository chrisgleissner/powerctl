from __future__ import annotations

import json

import pytest

from powerctl import cli
from powerctl.backends.base import DeviceRecord
from powerctl.errors import EXIT_OK, EXIT_REFUSED
from powerctl.registry import Registry


@pytest.fixture
def registry_with_plug(fake_backend):
    reg = Registry(
        devices=[
            DeviceRecord(
                backend="fake",
                host="192.0.2.10",
                alias="Lab Plug",
                model="FAKE100",
                device_type="plug",
                mac="AA:BB:CC:DD:EE:FF",
                supports_switching=True,
                supports_energy=True,
            )
        ]
    )
    reg.save()
    return reg


def test_off_without_yes_exits_refused(registry_with_plug, fake_backend, capsys):
    code = cli.main(["off", "Lab Plug", "--backend", "fake"])
    assert code == EXIT_REFUSED
    assert "--yes" in capsys.readouterr().err
    assert not any(call[0] == "switch" for call in fake_backend.calls)


def test_off_with_yes_switches(registry_with_plug, fake_backend):
    assert cli.main(["off", "Lab Plug", "--backend", "fake", "--yes"]) == EXIT_OK
    assert ("switch", "192.0.2.10", "off") in fake_backend.calls


def test_status_json_reports_power(registry_with_plug, fake_backend, capsys):
    assert cli.main(["status", "Lab Plug", "--backend", "fake", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "on"
    assert payload["energy"]["power_w"] == 12.5


def test_list_shows_registry(registry_with_plug, capsys):
    assert cli.main(["list", "--backend", "fake"]) == EXIT_OK
    assert "Lab Plug" in capsys.readouterr().out


def test_protect_then_off_is_refused(registry_with_plug, fake_backend, capsys):
    assert cli.main(["protect", "Lab Plug", "--backend", "fake"]) == EXIT_OK
    capsys.readouterr()
    code = cli.main(["off", "Lab Plug", "--backend", "fake", "--yes"])
    assert code == EXIT_REFUSED
    assert "protected list" in capsys.readouterr().err


def test_cycle_json_lists_events(registry_with_plug, fake_backend, capsys):
    code = cli.main(
        ["cycle", "Lab Plug", "--backend", "fake", "--yes", "--off-seconds", "0", "--json"]
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["final_state"] == "on"
    assert [event["step"] for event in payload["events"]][:2] == ["initial_state", "power_off"]


def test_doctor_flags_a_wide_open_credential_file(capsys):
    from powerctl.secrets import store_credentials
    import os

    path = store_credentials("fake", "user", "password123")
    os.chmod(path, 0o644)
    code = cli.main(["doctor"])
    assert code == 1
    assert "chmod 600" in capsys.readouterr().out


def test_cli_output_redacts_registered_secrets(capsys):
    from powerctl.secrets import REDACTOR

    REDACTOR.add("swordfish-secret")
    cli.out("password=swordfish-secret")
    assert "swordfish-secret" not in capsys.readouterr().out

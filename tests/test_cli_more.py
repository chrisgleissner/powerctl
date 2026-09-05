"""CLI behaviour: output shapes, exit codes and the safety flags."""

from __future__ import annotations

import json

import pytest

from powerctl import cli, core
from powerctl.backends.base import DeviceRecord
from powerctl.errors import EXIT_OK, EXIT_REFUSED, PowerctlError
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
                last_seen="2026-01-01T00:00:00+00:00",
            )
        ]
    )
    reg.save()
    return reg


def test_discover_lists_devices(registry_with_plug, fake_backend, capsys):
    assert cli.main(["discover", "--backend", "fake", "--timeout", "1"]) == EXIT_OK
    printed = capsys.readouterr().out
    assert "Lab Plug" in printed
    assert "Saved" in printed


def test_discover_json_is_machine_readable(fake_backend, capsys):
    assert cli.main(["discover", "--backend", "fake", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["host"] == "192.0.2.10"


def test_discover_reports_devices_that_did_not_answer(fake_backend, capsys):
    reg = Registry(
        devices=[
            DeviceRecord(backend="fake", host="192.0.2.77", alias="Bench", supports_switching=True)
        ]
    )
    reg.save()
    cli.main(["discover", "--backend", "fake"])
    assert "did not answer this scan" in capsys.readouterr().out


def test_discover_hides_devices_without_a_relay_by_default(fake_backend, capsys, monkeypatch):
    async def mixed_discover(*, target, timeout, credentials):
        return [
            DeviceRecord(backend="fake", host="192.0.2.10", alias="Plug", supports_switching=True),
            DeviceRecord(backend="fake", host="192.0.2.20", model="X20", error="mesh node"),
        ]

    monkeypatch.setattr(fake_backend, "discover", mixed_discover)
    cli.main(["discover", "--backend", "fake", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert [rec["host"] for rec in payload] == ["192.0.2.10"]

    cli.main(["discover", "--backend", "fake", "--all-devices", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert [rec["host"] for rec in payload] == ["192.0.2.10", "192.0.2.20"]


def test_discover_with_sweep_announces_itself(fake_backend, capsys, monkeypatch):
    async def no_candidates(hosts, ports, **kwargs):
        return []

    monkeypatch.setattr(core, "open_ports", no_candidates)
    cli.main(["discover", "--backend", "fake", "--sweep", "192.0.2.0/30"])
    assert "Sweeping the local subnet" in capsys.readouterr().out


def test_probe_json_output(fake_backend, capsys):
    assert cli.main(["probe", "192.0.2.10", "--backend", "fake", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["alias"] == "Lab Plug"


def test_status_of_several_devices_reports_each_error(registry_with_plug, fake_backend, capsys):
    code = cli.main(["status", "Lab Plug", "missing", "--backend", "fake"])
    printed = capsys.readouterr().out
    assert code == 1
    assert "Lab Plug" in printed and "error" in printed


def test_status_json_of_several_devices(registry_with_plug, fake_backend, capsys):
    cli.main(["status", "Lab Plug", "missing", "--backend", "fake", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["alias"] == "Lab Plug"
    assert "error" in payload[1]


def test_status_all_uses_the_registry(registry_with_plug, fake_backend, capsys):
    assert cli.main(["status", "--all", "--backend", "fake"]) == EXIT_OK
    assert "Lab Plug" in capsys.readouterr().out


def test_status_without_a_registry_is_a_usage_error(fake_backend, capsys):
    assert cli.main(["status", "--all", "--backend", "fake"]) == 2
    assert "registry is empty" in capsys.readouterr().err


def test_status_with_features(registry_with_plug, fake_backend, capsys, monkeypatch):
    original = fake_backend.status

    async def with_features(record, *, credentials, child=None, with_features=False):
        status = await original(record, credentials=credentials, child=child)
        status.features = {"led": True}
        return status

    monkeypatch.setattr(fake_backend, "status", with_features)
    cli.main(["status", "Lab Plug", "--backend", "fake", "--features"])
    assert "led = True" in capsys.readouterr().out


def test_on_switches_and_prints_state(registry_with_plug, fake_backend, capsys):
    assert cli.main(["on", "Lab Plug", "--backend", "fake"]) == EXIT_OK
    assert "[on]" in capsys.readouterr().out


def test_off_json(registry_with_plug, fake_backend, capsys):
    assert cli.main(["off", "Lab Plug", "--backend", "fake", "--yes", "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["state"] == "off"


def test_dry_run_reports_without_switching(registry_with_plug, fake_backend, capsys):
    code = cli.main(["off", "Lab Plug", "--backend", "fake", "--yes", "--dry-run"])
    assert code == EXIT_OK
    assert "WOULD run" in capsys.readouterr().out
    assert not any(call[0] == "switch" for call in fake_backend.calls)


def test_dry_run_reports_a_refusal(registry_with_plug, fake_backend, capsys):
    cli.main(["protect", "Lab Plug", "--backend", "fake"])
    capsys.readouterr()
    code = cli.main(["off", "Lab Plug", "--backend", "fake", "--yes", "--dry-run"])
    assert code == EXIT_REFUSED
    assert "REFUSED" in capsys.readouterr().out
    assert not any(call[0] == "switch" for call in fake_backend.calls)


def test_dry_run_json_of_a_cycle(registry_with_plug, fake_backend, capsys):
    cli.main(["cycle", "Lab Plug", "--backend", "fake", "--yes", "--dry-run", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "device": "Lab Plug",
        "host": "192.0.2.10",
        "mac": "AA:BB:CC:DD:EE:FF",
        "action": "cycle",
        "would_run": True,
        "protected": False,
        "critical": False,
    }


def test_cycle_prints_its_events(registry_with_plug, fake_backend, capsys):
    code = cli.main(["cycle", "Lab Plug", "--backend", "fake", "--yes", "--off-seconds", "0"])
    printed = capsys.readouterr().out
    assert code == EXIT_OK
    assert "power_off" in printed and "Final state: on" in printed


def test_critical_protection_cannot_be_overridden(registry_with_plug, fake_backend, capsys):
    cli.main(["protect", "Lab Plug", "--backend", "fake", "--critical"])
    capsys.readouterr()
    code = cli.main(["off", "Lab Plug", "--backend", "fake", "--yes", "--force-protected"])
    assert code == EXIT_REFUSED
    assert "critical" in capsys.readouterr().err
    assert not any(call[0] == "switch" for call in fake_backend.calls)


def test_critical_protection_survives_being_addressed_by_address(
    registry_with_plug, fake_backend, capsys
):
    cli.main(["protect", "Lab Plug", "--backend", "fake", "--critical"])
    capsys.readouterr()
    code = cli.main(["off", "192.0.2.10", "--backend", "fake", "--yes", "--force-protected"])
    assert code == EXIT_REFUSED
    assert not any(call[0] == "switch" for call in fake_backend.calls)


def test_unprotect_refuses_a_critical_device(registry_with_plug, fake_backend, capsys):
    cli.main(["protect", "Lab Plug", "--backend", "fake", "--critical"])
    capsys.readouterr()
    assert cli.main(["unprotect", "Lab Plug", "--backend", "fake"]) == 1
    assert "edit" in capsys.readouterr().err


def test_unprotect_removes_an_ordinary_protection(registry_with_plug, fake_backend, capsys):
    cli.main(["protect", "Lab Plug", "--backend", "fake"])
    capsys.readouterr()
    assert cli.main(["unprotect", "Lab Plug", "--backend", "fake"]) == EXIT_OK
    assert "Removed" in capsys.readouterr().out


def test_unprotect_of_an_unknown_name(registry_with_plug, capsys):
    assert cli.main(["unprotect", "nothing"]) == EXIT_OK
    assert "was not on the protected list" in capsys.readouterr().out


def test_protect_an_unknown_name(fake_backend, capsys):
    assert cli.main(["protect", "Future Plug", "--backend", "fake"]) == EXIT_OK
    printed = capsys.readouterr().out
    assert "not in the registry yet" in printed


def test_protected_listing(registry_with_plug, fake_backend, capsys):
    cli.main(["protect", "Lab Plug", "--backend", "fake", "--critical"])
    capsys.readouterr()
    assert cli.main(["protected"]) == EXIT_OK
    printed = capsys.readouterr().out
    assert "Lab Plug" in printed and "[critical]" in printed


def test_protected_listing_json(registry_with_plug, fake_backend, capsys):
    cli.main(["protect", "Lab Plug", "--backend", "fake"])
    capsys.readouterr()
    cli.main(["protected", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["mac"] == "AA:BB:CC:DD:EE:FF"


def test_protected_listing_when_empty(capsys):
    assert cli.main(["protected"]) == EXIT_OK
    assert "No protected devices" in capsys.readouterr().out


def test_backends_listing(capsys):
    assert cli.main(["backends"]) == EXIT_OK
    assert "kasa:" in capsys.readouterr().out


def test_backends_listing_json(capsys):
    cli.main(["backends", "--json"])
    names = {entry["name"] for entry in json.loads(capsys.readouterr().out)}
    assert {"kasa", "tapo"} <= names


def test_login_stores_credentials(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": "user@example.com")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "password123")
    assert cli.main(["login", "--backend", "kasa"]) == EXIT_OK
    printed = capsys.readouterr().out
    assert "Stored credentials for 'tplink'" in printed
    assert "password123" not in printed


def test_login_needs_a_username(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert cli.main(["login"]) == 2
    assert "no account name" in capsys.readouterr().err


def test_login_needs_a_password(monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "")
    assert cli.main(["login", "--username", "user@example.com"]) == 2
    assert "no password" in capsys.readouterr().err


def test_logout_removes_credentials(monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "password123")
    cli.main(["login", "--username", "user@example.com"])
    capsys.readouterr()
    assert cli.main(["logout"]) == EXIT_OK
    assert "Removed stored credentials" in capsys.readouterr().out


def test_logout_without_credentials(capsys):
    assert cli.main(["logout"]) == EXIT_OK
    assert "No stored credentials" in capsys.readouterr().out


def test_doctor_json(capsys):
    assert cli.main(["doctor", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True


def test_doctor_reports_credential_source(monkeypatch, capsys):
    monkeypatch.setenv("POWERCTL_TPLINK_USERNAME", "user@example.com")
    monkeypatch.setenv("POWERCTL_TPLINK_PASSWORD", "password123")
    cli.main(["doctor"])
    printed = capsys.readouterr().out
    assert "user@example.com" in printed
    assert "password123" not in printed


def test_errors_are_reported_with_their_exit_code(monkeypatch, capsys):
    async def boom(args):
        raise PowerctlError("something went wrong")

    monkeypatch.setattr(cli, "run", boom)
    assert cli.main(["list"]) == 1
    assert "something went wrong" in capsys.readouterr().err


def test_interrupt_is_reported(monkeypatch, capsys):
    async def interrupt(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run", interrupt)
    assert cli.main(["list"]) == 130
    assert "interrupted" in capsys.readouterr().err


def test_status_of_a_device_without_energy(fake_backend, capsys, monkeypatch):
    reg = Registry(
        devices=[
            DeviceRecord(
                backend="fake", host="192.0.2.10", alias="Lab Plug", supports_switching=True
            )
        ]
    )
    reg.save()

    async def no_energy(record, *, credentials, child=None, with_features=False):
        status = await fake_backend.__class__.status(
            fake_backend, record, credentials=credentials, child=child
        )
        status.energy = None
        return status

    monkeypatch.setattr(fake_backend, "status", no_energy)
    cli.main(["status", "Lab Plug", "--backend", "fake"])
    assert "energy: not reported" in capsys.readouterr().out


def test_list_json(registry_with_plug, capsys):
    cli.main(["list", "--backend", "fake", "--json"])
    assert json.loads(capsys.readouterr().out)[0]["alias"] == "Lab Plug"


def test_list_of_an_empty_registry(capsys):
    assert cli.main(["list"]) == EXIT_OK
    assert "No devices found" in capsys.readouterr().out


def test_status_all_skips_devices_without_a_relay(fake_backend, capsys):
    reg = Registry(
        devices=[
            DeviceRecord(
                backend="fake", host="192.0.2.10", alias="Lab Plug", supports_switching=True
            ),
            DeviceRecord(backend="fake", host="192.0.2.20", model="X20", error="mesh node"),
        ]
    )
    reg.save()
    assert cli.main(["status", "--all", "--backend", "fake"]) == EXIT_OK
    printed = capsys.readouterr().out
    assert "Lab Plug" in printed
    assert "192.0.2.20" not in printed

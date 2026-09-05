from __future__ import annotations

import json

import pytest

from powerctl.backends.base import DeviceRecord
from powerctl.errors import DeviceNotFound
from powerctl.registry import Registry, looks_like_host


def record(**kwargs) -> DeviceRecord:
    base = {
        "backend": "fake",
        "host": "192.0.2.10",
        "alias": "Lab Plug",
        "mac": "AA:BB:CC:DD:EE:FF",
    }
    base.update(kwargs)
    return DeviceRecord(**base)


def test_save_and_load_roundtrip():
    reg = Registry(devices=[record()])
    path = reg.save()
    again = Registry.load(path)
    assert again.devices[0].alias == "Lab Plug"
    assert again.devices[0].host == "192.0.2.10"


def test_saved_registry_contains_no_credentials():
    reg = Registry(
        devices=[
            record(
                connect_hint={
                    "host": "192.0.2.10",
                    "credentials": {"username": "u", "password": "hunter2hunter2"},
                }
            )
        ]
    )
    path = reg.save()
    text = path.read_text()
    assert "hunter2hunter2" not in text
    assert json.loads(text)["devices"][0]["connect_hint"]["credentials"] == "***"


def test_upsert_matches_on_mac_after_address_change():
    reg = Registry(devices=[record()])
    reg.upsert(record(host="192.0.2.99"))
    assert len(reg.devices) == 1
    assert reg.devices[0].host == "192.0.2.99"


def test_find_by_alias_host_and_mac():
    reg = Registry(devices=[record()])
    assert reg.find("lab plug") is not None
    assert reg.find("192.0.2.10") is not None
    assert reg.find("aa-bb-cc-dd-ee-ff") is not None
    assert reg.find("nothing") is None


def test_find_by_unique_prefix():
    reg = Registry(devices=[record()])
    assert reg.find("lab") is not None


def test_ambiguous_prefix_raises():
    reg = Registry(
        devices=[record(), record(host="192.0.2.11", alias="Lab Bench", mac="AA:BB:CC:DD:EE:00")]
    )
    with pytest.raises(DeviceNotFound, match="ambiguous"):
        reg.find("lab")


def test_protect_and_unprotect():
    reg = Registry(devices=[record()])
    assert reg.is_protected(reg.devices[0]) is False
    reg.protect(name="Lab Plug")
    assert reg.is_protected(reg.devices[0]) is True
    assert reg.unprotect("lab plug") is True
    assert reg.is_protected(reg.devices[0]) is False


def test_looks_like_host():
    assert looks_like_host("192.0.2.50")
    assert looks_like_host("plug.lan")
    assert not looks_like_host("Lab Plug")


def test_protection_matches_every_identifier_of_the_device():
    reg = Registry(devices=[record()])
    reg.protect(record=reg.devices[0])
    for name in ("Lab Plug", "192.0.2.10", "AA:BB:CC:DD:EE:FF", "aa-bb-cc-dd-ee-ff"):
        probe = record(alias=None, mac=None, host="0.0.0.0")
        probe = DeviceRecord(backend="fake", host=name)
        assert reg.is_protected(probe) is True, name


def test_protection_survives_a_registry_without_the_device():
    reg = Registry(devices=[record()])
    reg.protect(record=reg.devices[0])
    reg.save()
    rebuilt = Registry.load(reg.path)
    rebuilt.devices = []
    anonymous = DeviceRecord(backend="fake", host="192.0.2.10")
    assert rebuilt.is_protected(anonymous) is True


def test_protection_file_is_separate_from_the_device_cache():
    from powerctl.registry import protection_path

    reg = Registry(devices=[record()])
    reg.protect(record=reg.devices[0])
    path = reg.save()
    path.unlink()  # delete the device cache only
    assert protection_path().exists()
    assert Registry.load(path).is_protected(reg.devices[0]) is True


def test_legacy_string_protections_are_migrated():
    from powerctl.registry import Protection

    reg = Registry(devices=[record()], protected=[Protection.from_any("Lab Plug")])
    assert reg.is_protected(reg.devices[0]) is True
    reg.protect(record=reg.devices[0])
    assert reg.is_protected(DeviceRecord(backend="fake", host="192.0.2.10")) is True


def test_unreadable_registry_is_an_error(tmp_path):
    from powerctl.errors import PowerctlError

    path = tmp_path / "devices.json"
    path.write_text("{not json")
    with pytest.raises(PowerctlError, match="cannot read registry"):
        Registry.load(path)


def test_unreadable_protection_file_is_an_error(isolated_home):
    from powerctl.errors import PowerctlError
    from powerctl.registry import protection_path

    protection_path().parent.mkdir(parents=True, exist_ok=True)
    protection_path().write_text("{not json")
    with pytest.raises(PowerctlError, match="cannot read"):
        Registry.load()


def test_protection_file_of_the_wrong_shape_is_an_error(isolated_home):
    from powerctl.errors import PowerctlError
    from powerctl.registry import protection_path

    protection_path().parent.mkdir(parents=True, exist_ok=True)
    protection_path().write_text('{"protected": "Lab Plug"}')
    with pytest.raises(PowerctlError, match="expected a list"):
        Registry.load()


def test_protection_file_may_be_a_bare_list(isolated_home):
    from powerctl.registry import protection_path

    protection_path().parent.mkdir(parents=True, exist_ok=True)
    protection_path().write_text('["Lab Plug"]')
    assert Registry.load().is_protected(record()) is True


def test_missing_registry_still_loads_protections(isolated_home):
    reg = Registry(devices=[record()])
    reg.protect(record=reg.devices[0])
    reg.save_protected()
    assert Registry.load().is_protected(record()) is True


def test_critical_protection_cannot_be_removed_by_unprotect():
    from powerctl.errors import PowerctlError

    reg = Registry(devices=[record()])
    reg.protect(record=reg.devices[0], critical=True)
    with pytest.raises(PowerctlError, match="protected as critical"):
        reg.unprotect("Lab Plug")
    assert reg.unprotect("Lab Plug", allow_critical=True) is True


def test_criticality_is_not_lost_by_protecting_again():
    reg = Registry(devices=[record()])
    reg.protect(record=reg.devices[0], critical=True)
    reg.protect(record=reg.devices[0])
    assert reg.is_critical(record()) is True


def test_protecting_again_learns_new_identifiers():
    reg = Registry()
    reg.protect(name="Lab Plug")
    reg.protect(record=record())
    entry = reg.protection_for(record())
    assert entry is not None and entry.mac == "AA:BB:CC:DD:EE:FF"


def test_protect_needs_something_to_protect():
    with pytest.raises(ValueError, match="needs a record or a name"):
        Registry().protect()


def test_replace_backend_swaps_one_adapter_only():
    reg = Registry(devices=[record(), record(backend="other", host="192.0.2.20", mac=None)])
    reg.replace_backend("fake", [record(host="192.0.2.30", mac=None)])
    assert sorted(rec.host for rec in reg.devices) == ["192.0.2.20", "192.0.2.30"]


def test_to_dict_reports_devices_and_protections():
    reg = Registry(devices=[record()])
    reg.protect(record=reg.devices[0], critical=True)
    payload = reg.to_dict()
    assert payload["devices"][0]["alias"] == "Lab Plug"
    assert payload["protected"][0]["critical"] is True


def test_find_can_be_limited_to_one_adapter():
    reg = Registry(devices=[record()])
    assert reg.find("Lab Plug", backend="other") is None


def test_find_reports_an_address_claimed_by_two_adapters():
    reg = Registry(
        devices=[record(), record(backend="other", alias="Lab Plug", mac="AA:BB:CC:DD:EE:00")]
    )
    with pytest.raises(DeviceNotFound, match="several devices"):
        reg.find("Lab Plug")

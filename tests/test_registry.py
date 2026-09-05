from __future__ import annotations

import json

import pytest

from powerctl.backends.base import DeviceRecord
from powerctl.errors import DeviceNotFound
from powerctl.registry import Registry, looks_like_host


def record(**kwargs) -> DeviceRecord:
    base = dict(backend="fake", host="192.0.2.10", alias="Lab Plug", mac="AA:BB:CC:DD:EE:FF")
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
    reg.protect("Lab Plug")
    assert reg.is_protected(reg.devices[0]) is True
    assert reg.unprotect("lab plug") is True
    assert reg.is_protected(reg.devices[0]) is False


def test_looks_like_host():
    assert looks_like_host("192.0.2.50")
    assert looks_like_host("plug.lan")
    assert not looks_like_host("Lab Plug")

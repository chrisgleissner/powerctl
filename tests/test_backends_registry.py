"""The adapter registry itself."""

from __future__ import annotations

import pytest

from powerctl import backends
from powerctl.backends.base import Backend, DeviceRecord, DeviceStatus, EnergyReading
from powerctl.errors import UsageError


def test_known_adapters_are_listed():
    assert backends.backend_names() == ["kasa", "tapo"]


def test_unknown_adapter_is_a_usage_error():
    with pytest.raises(UsageError, match="unknown backend"):
        backends.get_backend("nope")


def test_adapters_are_instantiated_once():
    first = backends.get_backend("kasa")
    assert backends.get_backend("kasa") is first


def test_all_backends_returns_every_importable_adapter():
    names = {backend.name for backend in backends.all_backends()}
    assert {"kasa", "tapo"} <= names


def test_all_backends_skips_an_adapter_whose_dependency_is_missing(monkeypatch):
    monkeypatch.setitem(backends._LOADERS, "broken", "powerctl.backends.does_not_exist")
    monkeypatch.delitem(backends._CACHE, "broken", raising=False)
    names = {backend.name for backend in backends.all_backends()}
    assert "broken" not in names


def test_credential_scope_defaults_to_the_adapter_name():
    class Solo(Backend):
        name = "solo"

        async def discover(self, *, target, timeout, credentials):
            return []

        async def status(self, record, *, credentials, child=None, with_features=False):
            raise NotImplementedError

        async def switch(self, record, *, on, credentials, child=None):
            raise NotImplementedError

    assert Solo().scope == "solo"
    assert backends.get_backend("kasa").scope == "tplink"
    assert backends.get_backend("tapo").scope == "tplink"


async def test_probe_is_optional_for_an_adapter():
    class Solo(Backend):
        name = "solo"

        async def discover(self, *, target, timeout, credentials):
            return []

        async def status(self, record, *, credentials, child=None, with_features=False):
            raise NotImplementedError

        async def switch(self, record, *, on, credentials, child=None):
            raise NotImplementedError

    with pytest.raises(NotImplementedError):
        await Solo().probe("192.0.2.10", credentials=None)


def test_record_roundtrip_ignores_unknown_fields():
    record = DeviceRecord(backend="kasa", host="192.0.2.10", alias="Lab")
    restored = DeviceRecord.from_dict({**record.to_dict(), "unexpected": 1})
    assert restored == record


def test_status_to_dict_shapes_the_json():
    status = DeviceStatus(
        backend="kasa",
        host="192.0.2.10",
        is_on=False,
        energy=EnergyReading(power_w=1.0),
        child="Left",
        children=[DeviceStatus(backend="kasa", host="192.0.2.10", is_on=True)],
    )
    payload = status.to_dict()
    assert payload["state"] == "off"
    assert payload["child"] == "Left"
    assert payload["energy"]["power_w"] == 1.0
    assert payload["children"][0]["state"] == "on"


def test_status_without_a_switch_reports_no_state():
    assert DeviceStatus(backend="kasa", host="192.0.2.10").to_dict()["state"] is None

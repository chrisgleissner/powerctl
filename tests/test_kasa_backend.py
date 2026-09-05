"""Kasa adapter tests. python-kasa is never called: every device is a stub."""

from __future__ import annotations

import pytest
from kasa import AuthenticationError, KasaException, Module, UnsupportedDeviceError

from powerctl.backends import kasa_backend as kb
from powerctl.backends.base import DeviceRecord
from powerctl.errors import AuthRequired, DeviceNotFound, PowerctlError
from powerctl.secrets import Credentials


class StubType:
    def __init__(self, value):
        self.value = value


class StubEnergy:
    def __init__(self, **values):
        self._values = values

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._values:
            raise AttributeError(name)
        return self._values[name]


class StubModules(dict):
    pass


class StubFeature:
    def __init__(self, value):
        self._value = value

    @property
    def value(self):
        if isinstance(self._value, Exception):
            raise self._value
        return self._value


class StubConnectionType:
    def __init__(self, family="IOT.SMARTPLUGSWITCH"):
        self.device_family = StubType(family)


class StubConfig:
    def __init__(self, family="IOT.SMARTPLUGSWITCH", host="192.0.2.10"):
        self.connection_type = StubConnectionType(family)
        self._host = host

    def to_dict_control_credentials(self, credentials_hash=None):
        return {
            "host": self._host,
            "timeout": 5,
            "credentials": {"username": "u", "password": "p"},
            "connection_type": {"device_family": self.connection_type.device_family.value},
        }


class StubDevice:
    def __init__(
        self,
        *,
        host="192.0.2.10",
        alias="Lab Plug",
        model="KP115",
        device_type="plug",
        mac="AA:BB:CC:DD:EE:FF",
        device_id="ID1",
        is_on=True,
        energy=None,
        children=None,
        features=None,
        family="IOT.SMARTPLUGSWITCH",
        update_error=None,
    ):
        self.host = host
        self.alias = alias
        self.model = model
        self.device_type = StubType(device_type)
        self.mac = mac
        self.device_id = device_id
        self.is_on = is_on
        self.modules = StubModules({Module.Energy: energy} if energy else {})
        self.children = children or []
        self.features = features or {}
        self.config = StubConfig(family, host)
        self._update_error = update_error
        self.updates = 0
        self.disconnected = False
        self.switched: list[bool] = []

    async def update(self):
        self.updates += 1
        if self._update_error is not None:
            raise self._update_error

    async def turn_on(self):
        self.is_on = True
        self.switched.append(True)

    async def turn_off(self):
        self.is_on = False
        self.switched.append(False)

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
def backend():
    return kb.KasaBackend()


@pytest.fixture
def creds():
    return Credentials("user@example.com", "password123")


def test_connect_hint_has_no_credentials():
    hint = kb._connect_hint(StubDevice())
    assert "credentials" not in hint
    assert "credentials_hash" not in hint
    assert hint["host"] == "192.0.2.10"


def test_energy_reads_every_supported_field():
    energy = StubEnergy(
        current_consumption=12.5,
        voltage=230.0,
        current=0.05,
        consumption_today=1.5,
        consumption_this_month=20.0,
        consumption_total=100.0,
    )
    reading = kb._energy_of(StubDevice(energy=energy))
    assert reading.power_w == 12.5
    assert reading.total_kwh == 100.0


def test_energy_tolerates_missing_fields():
    reading = kb._energy_of(StubDevice(energy=StubEnergy(current_consumption=1.0)))
    assert reading.power_w == 1.0
    assert reading.voltage_v is None


def test_energy_is_none_without_the_module():
    assert kb._energy_of(StubDevice()) is None


def test_has_energy_looks_at_children():
    child = StubDevice(energy=StubEnergy(current_consumption=1.0))
    assert kb._has_energy(StubDevice(children=[child])) is True


def test_record_mapping_marks_switchable_types():
    record = kb._record_from_device(StubDevice())
    assert record.supports_switching is True
    assert record.backend == "kasa"
    assert record.mac == "AA:BB:CC:DD:EE:FF"


def test_record_of_a_sensor_is_not_switchable():
    record = kb._record_from_device(StubDevice(device_type="sensor"))
    assert record.supports_switching is False


def test_status_includes_children_and_safe_features():
    child = StubDevice(host="192.0.2.10", alias="Left", device_type="stripsocket")
    device = StubDevice(
        children=[child],
        features={"state": StubFeature(True), "broken": StubFeature(KasaException("no"))},
    )
    status = kb._status_from_device(device, with_features=True)
    assert [c.alias for c in status.children] == ["Left"]
    assert status.features == {"state": True}


def test_select_child_by_alias_and_index():
    children = [StubDevice(alias="Left"), StubDevice(alias="Right")]
    device = StubDevice(children=children)
    assert kb._select_child(device, "right").alias == "Right"
    assert kb._select_child(device, "0").alias == "Left"


def test_select_child_errors():
    with pytest.raises(DeviceNotFound, match="no child sockets"):
        kb._select_child(StubDevice(), "0")
    device = StubDevice(children=[StubDevice(alias="Left")])
    with pytest.raises(DeviceNotFound, match="no index"):
        kb._select_child(device, "5")
    with pytest.raises(DeviceNotFound, match="no socket"):
        kb._select_child(device, "Middle")


def test_discovery_field_handles_dicts_and_objects():
    assert kb._discovery_field({"device_type": "SMART.TAPOPLUG"}, "device_type") == "SMART.TAPOPLUG"
    assert kb._discovery_field({"result": {"device_model": "P110M"}}, "device_model") == "P110M"
    assert kb._discovery_field(None, "device_type") is None

    class Obj:
        device_type = "IOT.SMARTPLUGSWITCH"

    assert kb._discovery_field(Obj(), "device_type") == "IOT.SMARTPLUGSWITCH"


def test_is_iot_separates_the_two_adapters():
    assert kb.KasaBackend._is_iot(StubDevice()) is True
    assert kb.KasaBackend._is_iot(StubDevice(family="SMART.TAPOPLUG")) is False


def _patch_connect(monkeypatch, result):
    async def fake_connect(*, config=None, host=None):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(kb.Device, "connect", staticmethod(fake_connect))


async def test_status_reads_state(backend, creds, monkeypatch):
    device = StubDevice(energy=StubEnergy(current_consumption=7.0))
    _patch_connect(monkeypatch, device)
    status = await backend.status(
        DeviceRecord(backend="kasa", host="192.0.2.10", connect_hint={"host": "192.0.2.10"}),
        credentials=creds,
    )
    assert status.is_on is True
    assert status.energy.power_w == 7.0
    assert device.disconnected is True


async def test_status_of_one_socket(backend, creds, monkeypatch):
    child = StubDevice(alias="Left", is_on=False, device_type="stripsocket")
    _patch_connect(monkeypatch, StubDevice(children=[child], device_type="strip"))
    status = await backend.status(
        DeviceRecord(backend="kasa", host="192.0.2.10", connect_hint={"host": "192.0.2.10"}),
        credentials=creds,
        child="Left",
    )
    assert status.is_on is False


async def test_switch_off_and_on(backend, creds, monkeypatch):
    device = StubDevice()
    _patch_connect(monkeypatch, device)
    record = DeviceRecord(backend="kasa", host="192.0.2.10", connect_hint={"host": "192.0.2.10"})
    await backend.switch(record, on=False, credentials=creds)
    assert device.switched == [False]
    await backend.switch(record, on=True, credentials=creds)
    assert device.switched == [False, True]


async def test_switch_refuses_a_device_without_a_relay(backend, creds, monkeypatch):
    _patch_connect(monkeypatch, StubDevice(device_type="sensor"))
    with pytest.raises(PowerctlError, match="no switch"):
        await backend.switch(
            DeviceRecord(backend="kasa", host="192.0.2.10", connect_hint={"host": "192.0.2.10"}),
            on=False,
            credentials=creds,
        )


async def test_connect_reports_authentication_failures(backend, creds, monkeypatch):
    _patch_connect(monkeypatch, AuthenticationError("bad password"))
    with pytest.raises(AuthRequired, match="requires TP-Link credentials"):
        await backend.status(
            DeviceRecord(backend="kasa", host="192.0.2.10", connect_hint={"host": "192.0.2.10"}),
            credentials=creds,
        )


async def test_connect_falls_back_to_discovery(backend, creds, monkeypatch):
    _patch_connect(monkeypatch, KasaException("cached parameters are stale"))
    device = StubDevice()

    async def fake_single(host, credentials=None, **kwargs):
        return device

    monkeypatch.setattr(kb.Discover, "discover_single", staticmethod(fake_single))
    status = await backend.status(
        DeviceRecord(backend="kasa", host="192.0.2.10", connect_hint={"host": "192.0.2.10"}),
        credentials=creds,
    )
    assert status.alias == "Lab Plug"


async def test_connect_reports_an_unreachable_device(backend, creds, monkeypatch):
    _patch_connect(monkeypatch, KasaException("stale"))

    async def fake_single(host, credentials=None, **kwargs):
        raise KasaException("no answer")

    monkeypatch.setattr(kb.Discover, "discover_single", staticmethod(fake_single))
    with pytest.raises(DeviceNotFound, match="cannot reach"):
        await backend.status(
            DeviceRecord(backend="kasa", host="192.0.2.10", connect_hint={"host": "192.0.2.10"}),
            credentials=creds,
        )


async def test_owns_rejects_a_tapo_device(backend, creds, monkeypatch):
    async def unsupported(host, credentials=None, **kwargs):
        raise UnsupportedDeviceError("TPAP", host=host)

    monkeypatch.setattr(kb.Discover, "discover_single", staticmethod(unsupported))
    assert await backend._owns("192.0.2.10", creds) is False
    assert await backend.probe("192.0.2.10", credentials=creds) is None


async def test_owns_accepts_an_iot_device(backend, creds, monkeypatch):
    async def found(host, credentials=None, **kwargs):
        return StubDevice()

    monkeypatch.setattr(kb.Discover, "discover_single", staticmethod(found))
    assert await backend._owns("192.0.2.10", creds) is True


async def test_owns_assumes_ownership_when_discovery_fails(backend, creds, monkeypatch):
    async def fails(host, credentials=None, **kwargs):
        raise KasaException("no answer")

    monkeypatch.setattr(kb.Discover, "discover_single", staticmethod(fails))
    assert await backend._owns("192.0.2.10", creds) is True


async def test_probe_returns_a_record(backend, creds, monkeypatch):
    device = StubDevice()

    async def found(host, credentials=None, **kwargs):
        return device

    async def try_all(host, credentials=None, timeout=None):
        return device

    monkeypatch.setattr(kb.Discover, "discover_single", staticmethod(found))
    monkeypatch.setattr(kb.Discover, "try_connect_all", staticmethod(try_all))
    record = await backend.probe("192.0.2.10", credentials=creds)
    assert record is not None and record.alias == "Lab Plug"


async def test_discover_maps_devices_and_skips_tapo(backend, creds, monkeypatch):
    iot = StubDevice()
    tapo = StubDevice(host="192.0.2.20", family="SMART.TAPOPLUG")

    async def fake_discover(**kwargs):
        callback = kwargs.get("on_unsupported")
        if callback:
            await callback(
                UnsupportedDeviceError(
                    "unsupported",
                    host="192.0.2.30",
                    discovery_result={"device_type": "HOMEWIFISYSTEM", "device_model": "X20"},
                )
            )
            await callback(
                UnsupportedDeviceError(
                    "unsupported",
                    host="192.0.2.40",
                    discovery_result={"device_type": "SMART.TAPOPLUG", "device_model": "P110M"},
                )
            )
        return {"192.0.2.10": iot, "192.0.2.20": tapo}

    monkeypatch.setattr(kb.Discover, "discover", staticmethod(fake_discover))
    records = await backend.discover(target="192.0.2.255", timeout=1, credentials=creds)
    hosts = [rec.host for rec in records]
    # The Tapo device belongs to the other adapter and is not listed twice; the
    # mesh node is listed with an explanation instead of a protocol error.
    assert hosts == ["192.0.2.10", "192.0.2.30"]
    assert records[1].error == "Deco mesh node, no switchable outlet"
    assert tapo.disconnected is True


async def test_discover_marks_a_device_needing_credentials(backend, creds, monkeypatch):
    device = StubDevice(update_error=AuthenticationError("need login"))

    async def fake_discover(**kwargs):
        return {"192.0.2.10": device}

    monkeypatch.setattr(kb.Discover, "discover", staticmethod(fake_discover))
    records = await backend.discover(target="192.0.2.255", timeout=1, credentials=None)
    assert records[0].needs_credentials is True


async def test_discover_records_a_query_failure(backend, creds, monkeypatch):
    device = StubDevice(update_error=KasaException("timeout"))

    async def fake_discover(**kwargs):
        return {"192.0.2.10": device}

    monkeypatch.setattr(kb.Discover, "discover", staticmethod(fake_discover))
    records = await backend.discover(target="192.0.2.255", timeout=1, credentials=creds)
    assert records[0].error == "timeout"


def test_get_backend_returns_the_adapter():
    assert kb.get_backend().name == "kasa"


async def test_explain_probe_failure_reports_rejected_credentials(backend, creds, monkeypatch):
    async def refuse(*, config=None, host=None):
        raise AuthenticationError("handshake rejected")

    async def try_all(host, credentials=None, timeout=None):
        return None

    async def owned(host, credentials=None, **kwargs):
        return StubDevice()

    monkeypatch.setattr(kb.Device, "connect", staticmethod(refuse))
    monkeypatch.setattr(kb.Discover, "try_connect_all", staticmethod(try_all))
    monkeypatch.setattr(kb.Discover, "discover_single", staticmethod(owned))
    with pytest.raises(AuthRequired, match="rejected the credentials"):
        await backend.probe("192.0.2.10", credentials=creds)


async def test_explain_probe_failure_is_quiet_for_an_empty_address(backend, creds, monkeypatch):
    async def unreachable(*, config=None, host=None):
        raise KasaException("no answer")

    async def try_all(host, credentials=None, timeout=None):
        return None

    async def owned(host, credentials=None, **kwargs):
        return StubDevice()

    monkeypatch.setattr(kb.Device, "connect", staticmethod(unreachable))
    monkeypatch.setattr(kb.Discover, "try_connect_all", staticmethod(try_all))
    monkeypatch.setattr(kb.Discover, "discover_single", staticmethod(owned))
    assert await backend.probe("192.0.2.10", credentials=creds) is None


async def test_explain_probe_failure_stops_when_a_protocol_connects(backend, creds, monkeypatch):
    device = StubDevice()

    async def connects(*, config=None, host=None):
        return device

    async def try_all(host, credentials=None, timeout=None):
        return None

    async def owned(host, credentials=None, **kwargs):
        return StubDevice()

    monkeypatch.setattr(kb.Device, "connect", staticmethod(connects))
    monkeypatch.setattr(kb.Discover, "try_connect_all", staticmethod(try_all))
    monkeypatch.setattr(kb.Discover, "discover_single", staticmethod(owned))
    assert await backend.probe("192.0.2.10", credentials=creds) is None
    assert device.disconnected is True


async def test_probe_reports_authentication_from_the_protocol_walk(backend, creds, monkeypatch):
    async def owned(host, credentials=None, **kwargs):
        return StubDevice()

    async def try_all(host, credentials=None, timeout=None):
        raise AuthenticationError("bad password")

    monkeypatch.setattr(kb.Discover, "discover_single", staticmethod(owned))
    monkeypatch.setattr(kb.Discover, "try_connect_all", staticmethod(try_all))
    with pytest.raises(AuthRequired):
        await backend.probe("192.0.2.10", credentials=creds)


async def test_probe_reports_authentication_from_the_update(backend, creds, monkeypatch):
    device = StubDevice(update_error=AuthenticationError("expired"))

    async def owned(host, credentials=None, **kwargs):
        return StubDevice()

    async def try_all(host, credentials=None, timeout=None):
        return device

    monkeypatch.setattr(kb.Discover, "discover_single", staticmethod(owned))
    monkeypatch.setattr(kb.Discover, "try_connect_all", staticmethod(try_all))
    with pytest.raises(AuthRequired):
        await backend.probe("192.0.2.10", credentials=creds)


async def test_owns_treats_authentication_failure_as_ownership(backend, creds, monkeypatch):
    async def needs_auth(host, credentials=None, **kwargs):
        raise AuthenticationError("login required")

    monkeypatch.setattr(kb.Discover, "discover_single", staticmethod(needs_auth))
    assert await backend._owns("192.0.2.10", creds) is True


async def test_owns_when_discovery_returns_nothing(backend, creds, monkeypatch):
    async def nothing(host, credentials=None, **kwargs):
        return None

    monkeypatch.setattr(kb.Discover, "discover_single", staticmethod(nothing))
    assert await backend._owns("192.0.2.10", creds) is True


async def test_connect_without_a_cached_hint_uses_discovery(backend, creds, monkeypatch):
    device = StubDevice()

    async def fake_single(host, credentials=None, **kwargs):
        return device

    monkeypatch.setattr(kb.Discover, "discover_single", staticmethod(fake_single))
    status = await backend.status(
        DeviceRecord(backend="kasa", host="192.0.2.10"), credentials=creds
    )
    assert status.alias == "Lab Plug"


async def test_connect_when_discovery_finds_nothing(backend, creds, monkeypatch):
    async def nothing(host, credentials=None, **kwargs):
        return None

    monkeypatch.setattr(kb.Discover, "discover_single", staticmethod(nothing))
    with pytest.raises(DeviceNotFound, match="cannot reach"):
        await backend.status(DeviceRecord(backend="kasa", host="192.0.2.10"), credentials=creds)


async def test_connect_reports_authentication_from_discovery(backend, creds, monkeypatch):
    async def refuse(host, credentials=None, **kwargs):
        raise AuthenticationError("bad password")

    monkeypatch.setattr(kb.Discover, "discover_single", staticmethod(refuse))
    with pytest.raises(AuthRequired):
        await backend.status(DeviceRecord(backend="kasa", host="192.0.2.10"), credentials=creds)


async def test_switch_a_child_socket_reports_that_socket(backend, creds, monkeypatch):
    child = StubDevice(alias="Left", device_type="stripsocket")
    device = StubDevice(children=[child], device_type="strip")
    _patch_connect(monkeypatch, device)
    status = await backend.switch(
        DeviceRecord(backend="kasa", host="192.0.2.10", connect_hint={"host": "192.0.2.10"}),
        on=False,
        credentials=creds,
        child="Left",
    )
    assert child.switched == [False]
    assert status.child == "Left"


def test_credentials_are_converted_for_the_library():
    assert kb._to_kasa_credentials(None) is None
    converted = kb._to_kasa_credentials(Credentials("user", "password123"))
    assert converted.username == "user"

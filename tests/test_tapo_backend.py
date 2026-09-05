"""Tapo adapter tests. plugp100 is never called: every device is a stub."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from powerctl.backends import tapo_backend as tb
from powerctl.backends.base import DeviceRecord
from powerctl.errors import AuthRequired, DeviceNotFound
from powerctl.secrets import Credentials


class StubInfo:
    def __init__(self, data: dict[str, Any]):
        self._data = data

    def get_unmapped_state(self) -> dict[str, Any]:
        return self._data


class StubEnergy:
    def __init__(self, power: dict | None = None, energy: dict | None = None):
        self.power_info = StubInfo(power) if power is not None else None
        self.energy_info = StubInfo(energy) if energy is not None else None


class StubType:
    def __init__(self, value: str):
        self.value = value


class StubClient:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class StubSocket:
    def __init__(self, nickname: str, is_on: bool = True):
        self.nickname = nickname
        self.is_on = is_on
        self.switched: list[bool] = []

    async def turn_on(self):
        self.is_on = True
        self.switched.append(True)

    async def turn_off(self):
        self.is_on = False
        self.switched.append(False)


class StubDevice:
    def __init__(
        self,
        *,
        nickname="Lab Plug",
        model="P110M",
        mac="AA-BB-CC-DD-EE-FF",
        device_id="ID1",
        is_on=True,
        energy: StubEnergy | None = None,
        sockets: list[StubSocket] | None = None,
        raw_state: dict | None = None,
    ):
        self.nickname = nickname
        self.model = model
        self.mac = mac
        self.device_id = device_id
        self.is_on = is_on
        self.device_type = StubType("plug")
        self._energy = energy
        self.sockets = sockets or []
        self.is_strip = bool(sockets)
        self.raw_state = raw_state or {"device_on": is_on, "nested": {"x": 1}}
        self.client = StubClient()
        self.updates = 0
        self.switched: list[bool] = []

    def get_component(self, kind):
        return self._energy

    async def update(self):
        self.updates += 1

    async def turn_on(self):
        self.is_on = True
        self.switched.append(True)

    async def turn_off(self):
        self.is_on = False
        self.switched.append(False)


@dataclass
class StubSchema:
    http_port: int | None = 80
    is_support_https: bool = False
    encrypt_type: str = "TPAP"
    lv: int = 2


@dataclass
class StubDiscovered:
    ip: str = "192.0.2.10"
    device_type: str = "SMART.TAPOPLUG"
    device_model: str = "P110M(UK)"
    mac: str = "AA-BB-CC-DD-EE-FF"
    device_id: str = "ID1"
    mgt_encrypt_schm: Any = None


@pytest.fixture
def backend():
    return tb.TapoBackend()


@pytest.fixture
def creds():
    return Credentials("user@example.com", "password123")


def test_energy_converts_watts_and_watt_hours():
    reading = tb.energy_from_component(
        StubEnergy(
            power={"current_power": 162},
            energy={"today_energy": 1946, "month_energy": 12173, "current_power": 162681},
        )
    )
    assert reading is not None
    assert reading.power_w == 162.0
    assert reading.today_kwh == pytest.approx(1.946)
    assert reading.this_month_kwh == pytest.approx(12.173)


def test_energy_falls_back_to_milliwatts_when_watts_are_missing():
    reading = tb.energy_from_component(StubEnergy(energy={"current_power": 162681}))
    assert reading is not None
    assert reading.power_w == pytest.approx(162.681)


def test_energy_is_none_without_a_component():
    assert tb.energy_from_component(None) is None


def test_energy_is_none_when_the_device_reports_nothing():
    assert tb.energy_from_component(StubEnergy(power={}, energy={})) is None


def test_connect_hint_keeps_only_the_port():
    hint = tb.connect_hint_from_discovery(StubDiscovered(mgt_encrypt_schm=StubSchema()))
    assert hint == {"port": 80}


def test_connect_hint_is_empty_without_a_schema():
    assert tb.connect_hint_from_discovery(StubDiscovered()) == {}


def test_sanitise_hint_drops_the_encryption_scheme():
    # Passing the TPAP scheme to plugp100 makes every connection fail, so a hint
    # stored by an older version must not be forwarded.
    assert tb.sanitise_hint(
        {"port": 80, "encryption_type": "TPAP", "device_type": "SMART.TAPOPLUG"}
    ) == {"port": 80}


def test_sanitise_hint_handles_none():
    assert tb.sanitise_hint(None) == {}


def test_mac_is_normalised():
    assert tb._normalise_mac("aa-bb-cc-dd-ee-ff") == "AA:BB:CC:DD:EE:FF"
    assert tb._normalise_mac(None) is None


def test_record_mapping():
    record = tb.record_from_device(
        StubDevice(energy=StubEnergy(power={"current_power": 5})), host="192.0.2.10"
    )
    assert record.backend == "tapo"
    assert record.alias == "Lab Plug"
    assert record.mac == "AA:BB:CC:DD:EE:FF"
    assert record.supports_energy is True
    assert record.supports_switching is True


def test_status_mapping_includes_children():
    device = StubDevice(sockets=[StubSocket("Left"), StubSocket("Right", is_on=False)])
    status = tb.status_from_device(device, host="192.0.2.10")
    assert [child.alias for child in status.children] == ["Left", "Right"]
    assert [child.is_on for child in status.children] == [True, False]


def test_select_socket_by_name_and_index():
    device = StubDevice(sockets=[StubSocket("Left"), StubSocket("Right")])
    assert tb.select_socket(device, "right").nickname == "Right"
    assert tb.select_socket(device, "0").nickname == "Left"


def test_select_socket_rejects_unknown_names():
    device = StubDevice(sockets=[StubSocket("Left")])
    with pytest.raises(DeviceNotFound, match="no socket"):
        tb.select_socket(device, "Middle")
    with pytest.raises(DeviceNotFound, match="no index"):
        tb.select_socket(device, "7")


def test_select_socket_rejects_a_device_without_sockets():
    with pytest.raises(DeviceNotFound, match="no child sockets"):
        tb.select_socket(StubDevice(), "0")


def test_busy_and_auth_classification():
    assert tb._is_busy(Exception("Returned error_code: TapoError.ERR_STAT_ACCESS")) is True
    assert tb._is_busy(Exception("unknown error_code: -2101 TPAP discover failed")) is True
    assert tb._is_busy(Exception("some other failure")) is False
    assert tb._is_auth(Exception("Unable to authenticate for host")) is True
    assert tb._is_auth(Exception("host unreachable")) is False


def _patch_connect(monkeypatch, results):
    """Make plugp100's connect() return or raise the given sequence."""
    calls = []

    async def fake_connect(config, session):
        calls.append(config)
        result = results[min(len(calls) - 1, len(results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(tb, "connect", fake_connect)
    monkeypatch.setattr(tb, "_RETRY_BACKOFF_SECONDS", 0)
    return calls


async def test_status_reads_state(backend, creds, monkeypatch):
    device = StubDevice(energy=StubEnergy(power={"current_power": 12}))
    _patch_connect(monkeypatch, [device])
    status = await backend.status(
        DeviceRecord(backend="tapo", host="192.0.2.10"), credentials=creds
    )
    assert status.is_on is True
    assert status.energy.power_w == 12.0
    assert device.client.closed is True


async def test_status_of_one_socket(backend, creds, monkeypatch):
    device = StubDevice(sockets=[StubSocket("Left"), StubSocket("Right", is_on=False)])
    _patch_connect(monkeypatch, [device])
    status = await backend.status(
        DeviceRecord(backend="tapo", host="192.0.2.10"), credentials=creds, child="Right"
    )
    assert status.is_on is False
    assert status.alias == "Right"


async def test_status_with_features_keeps_scalars_only(backend, creds, monkeypatch):
    _patch_connect(monkeypatch, [StubDevice()])
    status = await backend.status(
        DeviceRecord(backend="tapo", host="192.0.2.10"),
        credentials=creds,
        with_features=True,
    )
    assert status.features == {"device_on": True}


async def test_switch_turns_the_device_off(backend, creds, monkeypatch):
    device = StubDevice()
    _patch_connect(monkeypatch, [device])
    status = await backend.switch(
        DeviceRecord(backend="tapo", host="192.0.2.10"), on=False, credentials=creds
    )
    assert device.switched == [False]
    assert status.is_on is False


async def test_switch_targets_one_socket(backend, creds, monkeypatch):
    socket = StubSocket("Left")
    device = StubDevice(sockets=[socket])
    _patch_connect(monkeypatch, [device])
    await backend.switch(
        DeviceRecord(backend="tapo", host="192.0.2.10"),
        on=False,
        credentials=creds,
        child="Left",
    )
    assert socket.switched == [False]


async def test_connect_retries_while_the_device_holds_a_stale_session(backend, creds, monkeypatch):
    device = StubDevice()
    calls = _patch_connect(
        monkeypatch,
        [Exception("Returned error_code: TapoError.ERR_STAT_ACCESS"), device],
    )
    status = await backend.status(
        DeviceRecord(backend="tapo", host="192.0.2.10"), credentials=creds
    )
    assert status.is_on is True
    assert len(calls) == 2


async def test_connect_reports_authentication_after_the_retries(backend, creds, monkeypatch):
    _patch_connect(monkeypatch, [tb.InvalidAuthentication("192.0.2.10", "SMART.TAPOPLUG")])
    with pytest.raises(AuthRequired, match="rejected the credentials"):
        await backend.status(DeviceRecord(backend="tapo", host="192.0.2.10"), credentials=creds)


async def test_connect_reports_an_unreachable_host_without_retrying(backend, creds, monkeypatch):
    calls = _patch_connect(monkeypatch, [tb.HostUnreachableError("192.0.2.99", None)])
    with pytest.raises(DeviceNotFound, match="cannot reach"):
        await backend.status(DeviceRecord(backend="tapo", host="192.0.2.10"), credentials=creds)
    assert len(calls) == 1


async def test_probe_returns_none_for_a_foreign_address(backend, creds, monkeypatch):
    _patch_connect(monkeypatch, [tb.HostUnreachableError("192.0.2.99", None)])
    assert await backend.probe("192.0.2.99", credentials=creds) is None


async def test_probe_returns_a_record(backend, creds, monkeypatch):
    _patch_connect(monkeypatch, [StubDevice()])
    record = await backend.probe("192.0.2.10", credentials=creds)
    assert record is not None and record.alias == "Lab Plug"


async def test_discover_maps_and_deduplicates_replies(backend, creds, monkeypatch):
    entry = StubDiscovered(mgt_encrypt_schm=StubSchema())
    # A mesh node answers the same discovery. It is not a SMART device, so the
    # kasa adapter reports it and this one leaves it alone rather than listing
    # every mesh node twice.
    mesh = StubDiscovered(ip="192.0.2.20", device_type="HOMEWIFISYSTEM", device_model="X20")
    camera = StubDiscovered(ip="192.0.2.30", device_type="SMART.IPCAMERA", device_model="C100")

    async def fake_scan(timeout=5):
        return [entry, entry, mesh, camera]

    monkeypatch.setattr(tb.TapoDiscovery, "scan", staticmethod(fake_scan))
    calls = _patch_connect(monkeypatch, [StubDevice()])
    records = await backend.discover(target="192.0.2.255", timeout=1, credentials=creds)
    assert [rec.host for rec in records] == ["192.0.2.10", "192.0.2.30"]
    # The duplicate reply must not cause a second connection, and a camera is
    # listed without being connected to.
    assert len(calls) == 1
    assert records[1].supports_switching is False


async def test_discover_records_an_authentication_failure(backend, creds, monkeypatch):
    async def fake_scan(timeout=5):
        return [StubDiscovered(mgt_encrypt_schm=StubSchema())]

    monkeypatch.setattr(tb.TapoDiscovery, "scan", staticmethod(fake_scan))
    _patch_connect(monkeypatch, [tb.InvalidAuthentication("192.0.2.10", "SMART.TAPOPLUG")])
    records = await backend.discover(target="192.0.2.255", timeout=1, credentials=creds)
    assert records[0].needs_credentials is True
    assert "rejected the credentials" in records[0].error


async def test_discover_records_other_failures(backend, creds, monkeypatch):
    async def fake_scan(timeout=5):
        return [StubDiscovered(mgt_encrypt_schm=StubSchema())]

    monkeypatch.setattr(tb.TapoDiscovery, "scan", staticmethod(fake_scan))
    _patch_connect(monkeypatch, [Exception("device is busy elsewhere")])
    records = await backend.discover(target="192.0.2.255", timeout=1, credentials=creds)
    assert records[0].error is not None


async def test_one_lock_per_address_serialises_access(backend):
    first = backend._lock_for("192.0.2.10")
    assert backend._lock_for("192.0.2.10") is first
    assert backend._lock_for("192.0.2.11") is not first
    assert isinstance(first, asyncio.Lock)


def test_empty_credentials_are_used_for_unbound_devices():
    auth = tb._to_auth(None)
    assert auth.username == "" and auth.password == ""


def test_get_backend_returns_the_adapter():
    assert tb.get_backend().name == "tapo"

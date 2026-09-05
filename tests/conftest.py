"""Shared fixtures. No test touches the network or the real config directory."""

from __future__ import annotations

import pytest

from powerctl import backends
from powerctl.backends.base import Backend, DeviceRecord, DeviceStatus, EnergyReading


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point the registry and credential file at a temporary directory."""
    monkeypatch.setenv("POWERCTL_HOME", str(tmp_path / "config"))
    for var in (
        "POWERCTL_FAKE_USERNAME",
        "POWERCTL_FAKE_PASSWORD",
        "POWERCTL_KASA_USERNAME",
        "POWERCTL_KASA_PASSWORD",
        "KASA_USERNAME",
        "KASA_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "config"


class FakeBackend(Backend):
    """In-memory backend used by the tests instead of real hardware."""

    name = "fake"
    description = "fake devices for tests"

    def __init__(self) -> None:
        self.state: dict[str, bool] = {"192.0.2.10": True}
        self.calls: list[tuple[str, ...]] = []

    async def discover(self, *, target, timeout, credentials):
        self.calls.append(("discover", target, str(timeout)))
        return [
            DeviceRecord(
                backend=self.name,
                host="192.0.2.10",
                alias="Lab Plug",
                model="FAKE100",
                device_type="plug",
                mac="AA:BB:CC:DD:EE:FF",
                supports_switching=True,
                supports_energy=True,
                connect_hint={"host": "192.0.2.10", "timeout": 5},
            )
        ]

    async def probe(self, host, *, credentials):
        self.calls.append(("probe", host))
        if host != "192.0.2.10":
            return None
        return DeviceRecord(
            backend=self.name,
            host=host,
            alias="Lab Plug",
            model="FAKE100",
            device_type="plug",
            supports_switching=True,
        )

    async def status(self, record, *, credentials, child=None, with_features=False):
        self.calls.append(("status", record.host, str(child)))
        return DeviceStatus(
            backend=self.name,
            host=record.host,
            alias=record.alias or "Lab Plug",
            model="FAKE100",
            device_type="plug",
            is_on=self.state.get(record.host, False),
            energy=EnergyReading(power_w=12.5, voltage_v=230.1, current_a=0.06),
            child=child,
        )

    async def switch(self, record, *, on, credentials, child=None):
        self.calls.append(("switch", record.host, "on" if on else "off"))
        self.state[record.host] = on
        status = await self.status(record, credentials=credentials, child=child)
        status.is_on = on
        return status


@pytest.fixture
def fake_backend(monkeypatch):
    """Register the fake backend under the name 'fake'."""
    backend = FakeBackend()
    monkeypatch.setitem(backends._LOADERS, "fake", "tests.conftest")
    monkeypatch.setitem(backends._CACHE, "fake", backend)
    return backend

"""Discovery merging, sweeping, credentials and the cycle sequence."""

from __future__ import annotations

import pytest

from powerctl import core
from powerctl.backends.base import DeviceRecord
from powerctl.errors import AuthRequired, DeviceNotFound, PowerctlError, RefusedError
from powerctl.registry import Registry
from powerctl.secrets import store_credentials


@pytest.fixture
def session(fake_backend):
    return core.Session(registry=Registry())


async def test_discover_stores_devices_and_stamps_them(session, fake_backend):
    records = await core.discover(session, backends=["fake"], target="192.0.2.255")
    assert records[0].last_seen is not None
    assert session.registry.find("Lab Plug") is not None


async def test_discover_keeps_devices_that_did_not_answer(session, fake_backend):
    session.registry.upsert(
        DeviceRecord(backend="fake", host="192.0.2.77", alias="Bench", mac="AA:BB:CC:DD:EE:00")
    )
    await core.discover(session, backends=["fake"], target="192.0.2.255")
    # A device that missed one scan must not vanish, or its protection would be
    # matched against nothing the next time it is addressed.
    assert session.registry.find("Bench") is not None
    assert session.registry.find("Lab Plug") is not None


async def test_discover_can_skip_saving(session, fake_backend):
    await core.discover(session, backends=["fake"], target="192.0.2.255", save=False)
    assert session.registry.devices == []


async def test_sweep_probes_only_addresses_the_broadcast_missed(session, fake_backend, monkeypatch):
    async def fake_open_ports(hosts, ports, **kwargs):
        # 192.0.2.10 already answered the broadcast; 192.0.2.30 did not.
        return ["192.0.2.10", "192.0.2.30"]

    monkeypatch.setattr(core, "open_ports", fake_open_ports)
    records = await core.discover(
        session, backends=["fake"], target="192.0.2.255", sweep="192.0.2.0/27"
    )
    probed = [call[1] for call in fake_backend.calls if call[0] == "probe"]
    assert probed == ["192.0.2.30"]
    assert [rec.host for rec in records].count("192.0.2.10") == 1


async def test_sweep_reports_a_device_that_rejects_credentials(session, fake_backend, monkeypatch):
    async def fake_open_ports(hosts, ports, **kwargs):
        return ["192.0.2.30"]

    async def refusing_probe(host, *, credentials):
        raise AuthRequired(f"{host} rejected the credentials")

    monkeypatch.setattr(core, "open_ports", fake_open_ports)
    monkeypatch.setattr(fake_backend, "probe", refusing_probe)
    records = await core.discover(
        session, backends=["fake"], target="192.0.2.255", sweep="192.0.2.0/29"
    )
    swept = [rec for rec in records if rec.host == "192.0.2.30"]
    assert swept and "rejected the credentials" in swept[0].error


async def test_sweep_without_a_detectable_subnet_is_an_error(session, monkeypatch):
    monkeypatch.setattr(core, "local_subnet", lambda: None)
    with pytest.raises(PowerctlError, match="cannot determine the local subnet"):
        await core.discover(session, backends=["fake"], sweep=True)


async def test_probe_tries_the_next_adapter(session, fake_backend, monkeypatch):
    records = await core.probe(session, ["192.0.2.10"], backends=["fake"], save=True)
    assert records and session.registry.find("Lab Plug") is not None


async def test_probe_reports_authentication_when_no_adapter_succeeds(
    session, fake_backend, monkeypatch
):
    async def refusing_probe(host, *, credentials):
        raise AuthRequired("device rejected the credentials")

    monkeypatch.setattr(fake_backend, "probe", refusing_probe)
    with pytest.raises(AuthRequired):
        await core.probe(session, ["192.0.2.10"], backends=["fake"])


async def test_probe_returns_nothing_for_an_address_with_no_device(session, fake_backend):
    assert await core.probe(session, ["192.0.2.99"], backends=["fake"]) == []


async def test_status_many_returns_errors_without_raising(session, fake_backend):
    session.registry.upsert(DeviceRecord(backend="fake", host="192.0.2.10", alias="Lab Plug"))
    results = await core.status_many(session, ["Lab Plug", "missing"], backend="fake")
    assert results[0].alias == "Lab Plug"
    assert isinstance(results[1], DeviceNotFound)


async def test_identification_failure_refuses_the_power_cut(session, fake_backend, monkeypatch):
    async def failing_status(record, *, credentials, child=None, with_features=False):
        raise PowerctlError("device did not answer")

    monkeypatch.setattr(fake_backend, "status", failing_status)
    with pytest.raises(RefusedError, match="could not be identified"):
        await core.switch(session, "192.0.2.10", on=False, backend="fake", confirmed=True)


async def test_credentials_are_shared_between_adapters_of_one_vendor(session):
    store_credentials("tplink", "user@example.com", "password123")
    assert core.credential_scope("kasa") == "tplink"
    assert core.credential_scope("tapo") == "tplink"
    assert core.credential_scope("unregistered") == "unregistered"
    creds = session.credentials("tapo")
    assert creds is not None and creds.username == "user@example.com"


async def test_credentials_are_loaded_once(session, monkeypatch):
    calls = []

    def counting_load(scope, **kwargs):
        calls.append(scope)
        return None

    monkeypatch.setattr(core, "load_credentials", counting_load)
    session.credentials("kasa")
    session.credentials("tapo")
    assert calls == ["tplink"]


async def test_cycle_waits_for_the_machine_to_come_back(session, fake_backend, monkeypatch):
    session.registry.upsert(DeviceRecord(backend="fake", host="192.0.2.10", alias="Lab Plug"))

    async def back_quickly(host, **kwargs):
        return 3.0

    monkeypatch.setattr(core, "wait_for_host", back_quickly)
    result = await core.cycle(
        session,
        "Lab Plug",
        backend="fake",
        confirmed=True,
        off_seconds=0,
        wait_host="192.0.2.60",
        wait_port=80,
    )
    assert result.wait_seconds == 3.0
    assert result.to_dict()["final_state"] == "on"


async def test_cycle_can_report_a_timeout_without_failing(session, fake_backend, monkeypatch):
    session.registry.upsert(DeviceRecord(backend="fake", host="192.0.2.10", alias="Lab Plug"))

    async def never(host, **kwargs):
        return None

    monkeypatch.setattr(core, "wait_for_host", never)
    result = await core.cycle(
        session,
        "Lab Plug",
        backend="fake",
        confirmed=True,
        off_seconds=0,
        wait_host="192.0.2.60",
        require_wait=False,
    )
    assert result.events[-1]["result"] == "timeout"


async def test_resolve_rejects_a_name_that_is_not_a_device(session):
    with pytest.raises(DeviceNotFound, match="no device called"):
        session.resolve("not a device")

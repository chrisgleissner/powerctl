from __future__ import annotations

import pytest

from powerctl import core
from powerctl.backends.base import DeviceRecord
from powerctl.errors import DeviceNotFound, RefusedError, UsageError, WaitTimeout
from powerctl.registry import Registry


@pytest.fixture
def session(fake_backend):
    reg = Registry(
        devices=[
            DeviceRecord(
                backend="fake",
                host="192.0.2.10",
                alias="Lab Plug",
                mac="AA:BB:CC:DD:EE:FF",
                supports_switching=True,
            )
        ]
    )
    return core.Session(registry=reg)


async def test_off_without_confirmation_is_refused(session, fake_backend):
    with pytest.raises(RefusedError, match="pass --yes"):
        await core.switch(session, "Lab Plug", on=False, backend="fake")
    assert not any(call[0] == "switch" for call in fake_backend.calls)


async def test_off_with_confirmation_switches(session, fake_backend):
    status = await core.switch(session, "Lab Plug", on=False, backend="fake", confirmed=True)
    assert status.is_on is False
    assert ("switch", "192.0.2.10", "off") in fake_backend.calls


async def test_on_needs_no_confirmation(session, fake_backend):
    status = await core.switch(session, "Lab Plug", on=True, backend="fake")
    assert status.is_on is True


async def test_protected_device_is_refused_even_with_yes(session):
    session.registry.protect("Lab Plug")
    with pytest.raises(RefusedError, match="protected list"):
        await core.switch(session, "Lab Plug", on=False, backend="fake", confirmed=True)


async def test_protected_device_can_be_forced(session, fake_backend):
    session.registry.protect("Lab Plug")
    status = await core.switch(
        session, "Lab Plug", on=False, backend="fake", confirmed=True, force_protected=True
    )
    assert status.is_on is False


async def test_status_does_not_switch(session, fake_backend):
    await core.status(session, "Lab Plug", backend="fake")
    assert not any(call[0] == "switch" for call in fake_backend.calls)


async def test_unknown_name_raises(session):
    with pytest.raises(DeviceNotFound):
        await core.status(session, "no such plug", backend="fake")


async def test_bare_ip_is_accepted_without_registry_entry(session):
    record = session.resolve("192.0.2.55", "fake")
    assert record.host == "192.0.2.55"


async def test_cycle_turns_off_then_on(session, fake_backend):
    result = await core.cycle(
        session, "Lab Plug", backend="fake", confirmed=True, off_seconds=0
    )
    switches = [call for call in fake_backend.calls if call[0] == "switch"]
    assert switches == [("switch", "192.0.2.10", "off"), ("switch", "192.0.2.10", "on")]
    assert result.final_state == "on"
    assert result.was_on is True
    assert [event["step"] for event in result.events] == [
        "initial_state",
        "power_off",
        "waited",
        "power_on",
    ]


async def test_cycle_without_confirmation_is_refused(session, fake_backend):
    with pytest.raises(RefusedError):
        await core.cycle(session, "Lab Plug", backend="fake", off_seconds=0)
    assert not any(call[0] == "switch" for call in fake_backend.calls)


async def test_cycle_rejects_negative_off_seconds(session):
    with pytest.raises(UsageError):
        await core.cycle(session, "Lab Plug", backend="fake", confirmed=True, off_seconds=-1)


async def test_cycle_reports_timeout_when_host_stays_down(session, monkeypatch):
    async def never_up(host, **kwargs):
        return None

    monkeypatch.setattr(core, "wait_for_host", never_up)
    with pytest.raises(WaitTimeout, match="did not answer"):
        await core.cycle(
            session,
            "Lab Plug",
            backend="fake",
            confirmed=True,
            off_seconds=0,
            wait_host="192.0.2.20",
            wait_timeout=1,
        )


async def test_cycle_records_wait_time_when_host_returns(session, monkeypatch):
    async def comes_back(host, **kwargs):
        return 4.2

    monkeypatch.setattr(core, "wait_for_host", comes_back)
    result = await core.cycle(
        session,
        "Lab Plug",
        backend="fake",
        confirmed=True,
        off_seconds=0,
        wait_host="192.0.2.20",
    )
    assert result.wait_seconds == 4.2
    assert result.events[-1]["step"] == "wait_host"

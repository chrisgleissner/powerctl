"""Actions shared by the CLI and by anything else driving powerctl.

Everything that decides *what* happens lives here; ``cli.py`` only parses
arguments and formats output. The safety rules are enforced in this module so
they cannot be bypassed by calling the library directly:

* :func:`switch` refuses to cut power unless the caller passes ``confirmed=True``.
* A device on the registry's protected list is refused even then, unless
  ``force_protected=True`` is also passed.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .backends import get_backend
from .backends.base import Backend, DeviceRecord, DeviceStatus
from .errors import DeviceNotFound, RefusedError, UsageError, WaitTimeout
from .netutil import default_broadcast, wait_for_host
from .registry import Registry, looks_like_host
from .secrets import REDACTOR, Credentials, load_credentials

DEFAULT_BACKEND = "kasa"
DEFAULT_OFF_SECONDS = 5.0


@dataclass
class Session:
    """Holds the registry and caches credentials for the duration of one run."""

    registry: Registry = field(default_factory=Registry.load)
    _credentials: dict[str, Credentials | None] = field(default_factory=dict)

    def credentials(self, backend: str) -> Credentials | None:
        """Return the credentials for ``backend``, loading them at most once."""
        if backend not in self._credentials:
            creds = load_credentials(backend)
            REDACTOR.add_credentials(creds)
            self._credentials[backend] = creds
        return self._credentials[backend]

    def backend(self, name: str) -> Backend:
        return get_backend(name)

    def resolve(self, name: str, backend: str | None = None) -> DeviceRecord:
        """Turn a user supplied name into a device record.

        Accepts an alias, host name, IP address, MAC address or device id from
        the registry. An address that is not in the registry is used directly,
        so a device can be driven before the first discovery.
        """
        record = self.registry.find(name, backend=backend)
        if record is not None:
            return record
        if looks_like_host(name):
            return DeviceRecord(backend=backend or DEFAULT_BACKEND, host=name)
        raise DeviceNotFound(
            f"no device called '{name}'; run 'powerctl discover' or pass an IP address"
        )


async def discover(
    session: Session,
    *,
    backends: list[str] | None = None,
    target: str | None = None,
    timeout: int = 5,
    save: bool = True,
) -> list[DeviceRecord]:
    """Scan the network and return every device the backends recognise."""
    names = backends or [DEFAULT_BACKEND]
    target = target or default_broadcast()
    found: list[DeviceRecord] = []
    for name in names:
        backend = session.backend(name)
        records = await backend.discover(
            target=target, timeout=timeout, credentials=session.credentials(name)
        )
        found.extend(records)
        if save:
            session.registry.replace_backend(name, records)
    if save:
        session.registry.save()
    return found


async def status(
    session: Session,
    name: str,
    *,
    backend: str | None = None,
    child: str | None = None,
    with_features: bool = False,
) -> DeviceStatus:
    """Query one device without changing its state."""
    record = session.resolve(name, backend)
    return await session.backend(record.backend).status(
        record,
        credentials=session.credentials(record.backend),
        child=child,
        with_features=with_features,
    )


async def status_many(
    session: Session, names: list[str], *, backend: str | None = None
) -> list[DeviceStatus | Exception]:
    """Query several devices concurrently. Failures are returned, not raised."""
    tasks = [status(session, name, backend=backend) for name in names]
    return await asyncio.gather(*tasks, return_exceptions=True)


def check_switch_allowed(
    session: Session,
    record: DeviceRecord,
    *,
    on: bool,
    confirmed: bool,
    force_protected: bool = False,
) -> None:
    """Raise :class:`RefusedError` if this power cut is not allowed.

    Turning a device on is always allowed. Turning one off needs an explicit
    confirmation from the caller, and a protected device needs a second,
    separate override on top of that.
    """
    if on:
        return
    label = record.alias or record.host
    if not confirmed:
        raise RefusedError(
            f"refusing to cut power to '{label}': pass --yes to confirm"
        )
    if session.registry.is_protected(record) and not force_protected:
        raise RefusedError(
            f"'{label}' is on the protected list: "
            f"pass --force-protected, or run 'powerctl unprotect {label}' first"
        )


async def switch(
    session: Session,
    name: str,
    *,
    on: bool,
    backend: str | None = None,
    child: str | None = None,
    confirmed: bool = False,
    force_protected: bool = False,
) -> DeviceStatus:
    """Switch a device on or off, after the safety checks have passed."""
    record = session.resolve(name, backend)
    check_switch_allowed(
        session, record, on=on, confirmed=confirmed, force_protected=force_protected
    )
    return await session.backend(record.backend).switch(
        record,
        on=on,
        credentials=session.credentials(record.backend),
        child=child,
    )


@dataclass
class CycleResult:
    """What a power cycle did, step by step, for machine readable output."""

    device: str
    host: str
    child: str | None
    was_on: bool | None
    off_seconds: float
    events: list[dict[str, Any]] = field(default_factory=list)
    final_state: str | None = None
    wait_host: str | None = None
    wait_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "host": self.host,
            "child": self.child,
            "was_on": self.was_on,
            "off_seconds": self.off_seconds,
            "events": self.events,
            "final_state": self.final_state,
            "wait_host": self.wait_host,
            "wait_seconds": self.wait_seconds,
        }


async def cycle(
    session: Session,
    name: str,
    *,
    backend: str | None = None,
    child: str | None = None,
    off_seconds: float = DEFAULT_OFF_SECONDS,
    confirmed: bool = False,
    force_protected: bool = False,
    wait_host: str | None = None,
    wait_port: int | None = None,
    wait_timeout: float = 120.0,
    require_wait: bool = True,
) -> CycleResult:
    """Cut power, wait, restore power, and optionally wait for a host to return.

    This is the sequence used to reboot a machine that has no reset line: the
    outlet is switched off for ``off_seconds`` and then back on. If ``wait_host``
    is given, the call does not return until that host answers again, so a
    caller knows whether the machine actually came back.
    """
    if off_seconds < 0:
        raise UsageError("--off-seconds cannot be negative")
    record = session.resolve(name, backend)
    check_switch_allowed(
        session, record, on=False, confirmed=confirmed, force_protected=force_protected
    )
    device_backend = session.backend(record.backend)
    creds = session.credentials(record.backend)
    label = record.alias or record.host

    before = await device_backend.status(record, credentials=creds, child=child)
    result = CycleResult(
        device=label,
        host=record.host,
        child=child,
        was_on=before.is_on,
        off_seconds=off_seconds,
        wait_host=wait_host,
    )
    started = time.monotonic()

    def note(step: str, **fields: Any) -> None:
        result.events.append(
            {"step": step, "at": round(time.monotonic() - started, 3), **fields}
        )

    note("initial_state", state=None if before.is_on is None else ("on" if before.is_on else "off"))

    off_status = await device_backend.switch(record, on=False, credentials=creds, child=child)
    note("power_off", state="on" if off_status.is_on else "off")

    await asyncio.sleep(off_seconds)
    note("waited", seconds=off_seconds)

    on_status = await device_backend.switch(record, on=True, credentials=creds, child=child)
    note("power_on", state="on" if on_status.is_on else "off")
    result.final_state = "on" if on_status.is_on else "off"

    if wait_host:
        elapsed = await wait_for_host(
            wait_host, port=wait_port, timeout=wait_timeout, up=True
        )
        if elapsed is None:
            note("wait_host", host=wait_host, port=wait_port, result="timeout")
            if require_wait:
                raise WaitTimeout(
                    f"power restored to '{label}', but {wait_host} did not answer "
                    f"within {wait_timeout:.0f}s"
                )
        else:
            result.wait_seconds = round(elapsed, 2)
            note("wait_host", host=wait_host, port=wait_port, seconds=result.wait_seconds)

    return result

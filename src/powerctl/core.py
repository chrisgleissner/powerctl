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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from .backends import get_backend
from .backends.base import Backend, DeviceRecord, DeviceStatus
from .errors import (
    AuthRequired,
    DeviceNotFound,
    PowerctlError,
    RefusedError,
    UsageError,
    WaitTimeout,
)
from .netutil import default_broadcast, local_subnet, open_ports, subnet_hosts, wait_for_host
from .registry import Registry, looks_like_host, protection_path
from .secrets import REDACTOR, Credentials, load_credentials

DEFAULT_BACKEND = "kasa"


def credential_scope(name: str) -> str:
    """Return the credential scope of the named backend, or the name itself.

    An unknown name is returned unchanged so that callers, and the tests, can ask
    about a backend that is not registered in this environment.
    """
    from .backends import get_backend

    try:
        return get_backend(name).scope
    except (UsageError, ImportError):
        return name


DEFAULT_OFF_SECONDS = 5.0


@dataclass
class Session:
    """Holds the registry and caches credentials for the duration of one run."""

    registry: Registry = field(default_factory=Registry.load)
    _credentials: dict[str, Credentials | None] = field(default_factory=dict)

    def credentials(self, backend: str) -> Credentials | None:
        """Return the credentials for ``backend``, loading them at most once.

        Adapters that share a vendor account share a credential scope, so the
        Tapo adapter finds the TP-Link ID the user stored while setting up their
        Kasa plugs.
        """
        scope = credential_scope(backend)
        if scope not in self._credentials:
            creds = load_credentials(scope)
            REDACTOR.add_credentials(creds)
            self._credentials[scope] = creds
        return self._credentials[scope]

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


#: TCP ports a TP-Link outlet may listen on: 9999 is the legacy IOT protocol,
#: 80 carries KLAP/AES/TPAP, 4433 is the https variant used by some models.
SWEEP_PORTS = (80, 9999, 4433)


async def discover(
    session: Session,
    *,
    backends: list[str] | None = None,
    target: str | None = None,
    timeout: int = 5,
    save: bool = True,
    sweep: str | bool = False,
    sweep_concurrency: int = 32,
) -> list[DeviceRecord]:
    """Scan the network and return every device the backends recognise.

    The broadcast pass is the fast path. ``sweep`` adds a second pass that probes
    each address of the local subnet directly, which finds a device that ignored
    or missed the broadcast; pass True for the subnet of the default route, or a
    CIDR such as "192.0.2.0/24".

    Results are merged into the registry rather than replacing it, so a device
    that did not answer this time keeps its entry and its protection.
    """
    names = backends or [DEFAULT_BACKEND]
    target = target or default_broadcast()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    found: list[DeviceRecord] = []
    for name in names:
        backend = session.backend(name)
        records = await backend.discover(
            target=target, timeout=timeout, credentials=session.credentials(name)
        )
        found.extend(replace(rec, last_seen=now) for rec in records)

    if sweep:
        cidr = sweep if isinstance(sweep, str) else (local_subnet() or "")
        if not cidr:
            raise PowerctlError("cannot determine the local subnet; pass --sweep <CIDR>")
        found.extend(
            await _sweep_subnet(
                session,
                cidr,
                names,
                already=[rec.host for rec in found],
                concurrency=sweep_concurrency,
                stamp=now,
            )
        )

    if save:
        for record in found:
            session.registry.upsert(record)
        session.registry.save()
    return found


async def _sweep_subnet(
    session: Session,
    cidr: str,
    backends: list[str],
    *,
    already: list[str],
    concurrency: int,
    stamp: str,
) -> list[DeviceRecord]:
    """Probe every address of ``cidr`` that has a TP-Link port open."""
    seen = set(already)
    hosts = [host for host in subnet_hosts(cidr) if host not in seen]
    candidates = [
        host
        for host in await open_ports(hosts, SWEEP_PORTS, concurrency=concurrency)
        # Filter again on the way out: a device already found by the broadcast
        # must not be probed and returned a second time.
        if host not in seen
    ]
    semaphore = asyncio.Semaphore(min(concurrency, 16))
    records: list[DeviceRecord] = []

    async def probe_one(host: str) -> None:
        async with semaphore:
            for name in backends:
                backend = session.backend(name)
                try:
                    record = await backend.probe(host, credentials=session.credentials(name))
                except NotImplementedError:
                    continue
                except PowerctlError as exc:
                    # A device that answers but rejects the credentials is still a
                    # device the user needs to see.
                    records.append(
                        DeviceRecord(backend=name, host=host, error=str(exc), last_seen=stamp)
                    )
                    return
                if record is not None:
                    records.append(replace(record, last_seen=stamp))
                    return

    await asyncio.gather(*(probe_one(host) for host in candidates))
    return sorted(records, key=lambda rec: rec.host)


async def probe(
    session: Session,
    hosts: list[str],
    *,
    backends: list[str] | None = None,
    save: bool = True,
) -> list[DeviceRecord]:
    """Identify devices by address when broadcast discovery does not reach them."""
    names = backends or [DEFAULT_BACKEND]
    found: list[DeviceRecord] = []
    for host in hosts:
        errors: list[PowerctlError] = []
        for name in names:
            backend = session.backend(name)
            try:
                record = await backend.probe(host, credentials=session.credentials(name))
            except NotImplementedError:
                continue
            except PowerctlError as exc:
                # One adapter failing is not the answer for the address: another
                # adapter may own the device. Keep the error in case none does.
                errors.append(exc)
                continue
            if record is not None:
                found.append(record)
                if save:
                    session.registry.upsert(record)
                break
        else:
            if errors:
                # Prefer an authentication error: it means a device was found and
                # only the account was wrong, which is what the user must fix.
                auth = [exc for exc in errors if isinstance(exc, AuthRequired)]
                raise (auth or errors)[0]
    if save and found:
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

    Turning a device on is always allowed. Turning one off passes three gates:

    1. The caller must confirm explicitly.
    2. A protected device is refused; ``force_protected`` overrides that.
    3. A device protected as critical is refused even then. No flag lifts it,
       because the point of the tier is that a wrong command cannot reach a
       fridge, a router or a server. Removing the entry from the protection file
       is a deliberate, separate act.
    """
    if on:
        return
    label = record.alias or record.host
    if not confirmed:
        raise RefusedError(f"refusing to cut power to '{label}': pass --yes to confirm")
    if session.registry.is_critical(record):
        entry = session.registry.protection_for(record)
        raise RefusedError(
            f"'{label}' is protected as critical and will not be switched off by "
            f"powerctl under any flag (matched protection '{entry.name if entry else label}'). "
            f"Remove it from {protection_path()} first if that is really intended."
        )
    if session.registry.is_protected(record) and not force_protected:
        raise RefusedError(
            f"'{label}' is on the protected list: "
            f"pass --force-protected, or run 'powerctl unprotect {label}' first"
        )


async def identify(
    session: Session, record: DeviceRecord, *, child: str | None = None
) -> DeviceRecord:
    """Fill in the identity of a device by asking the device itself.

    A name that is not in the registry resolves to a record holding nothing but
    an address. Protection is matched on identity, so cutting power to such a
    record without asking the device who it is would ignore a protection stored
    under the device's alias or MAC. This queries the device, which changes
    nothing on it, and returns a record carrying the identity it reported.
    """
    if record.mac and record.alias:
        return record
    try:
        status = await session.backend(record.backend).status(
            record, credentials=session.credentials(record.backend), child=child
        )
    except PowerctlError as exc:
        # Fail closed: an unidentified device may be a protected one under an
        # address the registry cannot currently match.
        raise RefusedError(
            f"refusing to cut power to '{record.host}': the device could not be "
            f"identified first, so its protection status is unknown ({exc})"
        ) from exc
    return replace(
        record,
        alias=record.alias or status.alias,
        mac=record.mac or status.mac,
        model=record.model or status.model,
        device_type=record.device_type or status.device_type,
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
    """Switch a device on or off, after the safety checks have passed.

    Cutting power runs the guard twice: once on the record as resolved, and once
    on the identity the device reports. The second check is what stops a
    protected device from being switched off by an address that the registry
    cannot currently match to its alias.
    """
    record = session.resolve(name, backend)
    check_switch_allowed(
        session, record, on=on, confirmed=confirmed, force_protected=force_protected
    )
    if not on:
        record = await identify(session, record, child=child)
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

    before = await device_backend.status(record, credentials=creds, child=child)
    # Re-check against the identity the device reported, not just the name the
    # caller used, before any power is cut.
    record = replace(
        record,
        alias=record.alias or before.alias,
        mac=record.mac or before.mac,
        model=record.model or before.model,
    )
    check_switch_allowed(
        session, record, on=False, confirmed=confirmed, force_protected=force_protected
    )
    label = record.alias or record.host
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
        result.events.append({"step": step, "at": round(time.monotonic() - started, 3), **fields})

    note("initial_state", state=None if before.is_on is None else ("on" if before.is_on else "off"))

    off_status = await device_backend.switch(record, on=False, credentials=creds, child=child)
    note("power_off", state="on" if off_status.is_on else "off")

    await asyncio.sleep(off_seconds)
    note("waited", seconds=off_seconds)

    on_status = await device_backend.switch(record, on=True, credentials=creds, child=child)
    note("power_on", state="on" if on_status.is_on else "off")
    result.final_state = "on" if on_status.is_on else "off"

    if wait_host:
        elapsed = await wait_for_host(wait_host, port=wait_port, timeout=wait_timeout, up=True)
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

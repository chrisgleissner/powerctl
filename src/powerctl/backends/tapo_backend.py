"""Tapo backend, built on plugp100.

Tapo devices are handled by their own adapter rather than by the kasa one for a
practical reason: the Tapo protocol keeps changing. Recent firmware (for example
the P110M on 1.4.3) speaks TPAP, which the current python-kasa release rejects
outright. plugp100 ships TPAP support in a released version, so the side of the
estate that moves fastest depends on a library that releases.

The mapping from plugp100's objects to powerctl's :class:`DeviceRecord` and
:class:`DeviceStatus` lives in module level functions so it can be unit tested
without hardware.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import aiohttp
from plugp100.common.credentials import AuthCredential
from plugp100.components.energy import EnergyComponent
from plugp100.devices.base import TapoDevice
from plugp100.devices.factory import DeviceConnectConfiguration, connect
from plugp100.discovery import TapoDiscovery
from plugp100.errors.invalid_authentication import InvalidAuthentication
from plugp100.errors.protocol_guess import (
    HostUnreachableError,
    ProtocolDetectionTimeoutError,
    UnsupportedProtocolError,
)

from ..errors import AuthRequired, DeviceNotFound, PowerctlError
from ..secrets import Credentials
from .base import Backend, DeviceRecord, DeviceStatus, EnergyReading

#: Discovery reports the family as a SMART.* string; these can be switched.
_SWITCHABLE_FAMILIES = ("SMART.TAPOPLUG", "SMART.TAPOSWITCH", "SMART.TAPOBULB")

#: plugp100 raises these when the address does not hold a usable Tapo device.
#: How many times a connection is attempted, and the pause between attempts.
#: TPAP devices keep a small number of sessions and reject new ones with
#: ERR_STAT_ACCESS or error -2101 while an old session is still held; the
#: condition clears on its own, so one retry recovers it.
#: Measured against a P110M on firmware 1.4.3: after the device has been idle,
#: the first TPAP handshake fails with InvalidAuthentication and the next one,
#: about ten seconds later, succeeds with the same credentials. Three attempts
#: with a ten second pause covers that without hiding a genuinely wrong password
#: for long.
_CONNECT_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 10.0

#: InvalidAuthentication also derives from ProtocolGuessError, so it must be
#: caught before these.
_NOT_A_DEVICE = (
    HostUnreachableError,
    ProtocolDetectionTimeoutError,
    UnsupportedProtocolError,
)


def _is_busy(exc: Exception) -> bool:
    """True if the device refused the session because it is holding another one."""
    text = str(exc).lower()
    return (
        "err_stat_access" in text
        or "-2101" in text
        or "session state access" in text
        or "tpap discover failed" in text
    )


def _is_auth(exc: Exception) -> bool:
    """True if the device rejected the account rather than the connection."""
    if isinstance(exc, InvalidAuthentication):
        return True
    text = str(exc).lower()
    return "authenticat" in text or "credential" in text


def _to_auth(credentials: Credentials | None) -> AuthCredential:
    """plugp100 always wants an AuthCredential; unbound devices accept empty ones."""
    if credentials is None:
        return AuthCredential("", "")
    return AuthCredential(credentials.username, credentials.password)


def _normalise_mac(mac: str | None) -> str | None:
    """Return a colon separated upper case MAC; plugp100 reports AA-BB-CC-DD-EE-FF style."""
    if not mac:
        return None
    return mac.replace("-", ":").upper()


def energy_from_component(component: EnergyComponent | None) -> EnergyReading | None:
    """Convert plugp100's energy payload into powerctl units.

    plugp100 reports ``current_power`` in watts in ``power_info`` and in
    milliwatts inside ``energy_info``; daily and monthly figures are watt hours.
    Voltage and current are not exposed by this library, so they stay None.
    """
    if component is None:
        return None
    reading = EnergyReading()
    power_info = getattr(component, "power_info", None)
    if power_info is not None:
        raw = power_info.get_unmapped_state()
        if raw.get("current_power") is not None:
            reading.power_w = float(raw["current_power"])
    energy_info = getattr(component, "energy_info", None)
    if energy_info is not None:
        raw = energy_info.get_unmapped_state()
        if reading.power_w is None and raw.get("current_power") is not None:
            reading.power_w = float(raw["current_power"]) / 1000.0
        if raw.get("today_energy") is not None:
            reading.today_kwh = float(raw["today_energy"]) / 1000.0
        if raw.get("month_energy") is not None:
            reading.this_month_kwh = float(raw["month_energy"]) / 1000.0
    if all(value is None for value in (reading.power_w, reading.today_kwh, reading.this_month_kwh)):
        return None
    return reading


def connect_hint_from_discovery(discovered: Any) -> dict[str, Any]:
    """Build secret-free reconnect parameters from a discovery reply.

    Only the port is kept. Passing plugp100 the encryption scheme that discovery
    reports for a TPAP device (``encryption_type="TPAP"``) makes every connection
    fail with ``error_code -2101, TPAP discover failed``, while letting plugp100
    detect the protocol itself works. The device type and model are left out for
    the same reason: they select a code path before detection has run.
    """
    schema = getattr(discovered, "mgt_encrypt_schm", None)
    if schema is None:
        return {}
    port = schema.http_port or (443 if schema.is_support_https else 80)
    return {"port": port} if port else {}


#: Connect parameters powerctl is willing to pass to plugp100. Anything else,
#: including a stored encryption scheme from an older registry, is dropped.
_ALLOWED_HINT_KEYS = frozenset({"port"})


def sanitise_hint(hint: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only the connect parameters that are safe to force."""
    if not hint:
        return {}
    return {key: value for key, value in hint.items() if key in _ALLOWED_HINT_KEYS}


def record_from_device(
    device: TapoDevice, *, host: str, connect_hint: dict[str, Any] | None = None
) -> DeviceRecord:
    """Map a connected plugp100 device to a registry record."""
    energy = device.get_component(EnergyComponent)
    sockets = list(device.sockets) if getattr(device, "is_strip", False) else []
    return DeviceRecord(
        backend=TapoBackend.name,
        host=host,
        alias=device.nickname,
        model=device.model,
        device_type=device.device_type.value,
        mac=_normalise_mac(device.mac),
        device_id=device.device_id,
        supports_switching=hasattr(device, "turn_on"),
        supports_energy=energy is not None,
        children=[socket.nickname or f"child_{index}" for index, socket in enumerate(sockets)],
        connect_hint=connect_hint or {},
    )


def status_from_device(
    device: TapoDevice,
    *,
    host: str,
    child: str | None = None,
    include_children: bool = True,
) -> DeviceStatus:
    """Map a connected plugp100 device to a status result."""
    status = DeviceStatus(
        backend=TapoBackend.name,
        host=host,
        alias=device.nickname,
        model=device.model,
        device_type=device.device_type.value,
        mac=_normalise_mac(device.mac),
        is_on=getattr(device, "is_on", None),
        energy=energy_from_component(device.get_component(EnergyComponent)),
        child=child,
    )
    if include_children and getattr(device, "is_strip", False):
        status.children = [
            DeviceStatus(
                backend=TapoBackend.name,
                host=host,
                alias=socket.nickname,
                device_type="stripsocket",
                is_on=socket.is_on,
            )
            for socket in device.sockets
        ]
    return status


def select_socket(device: TapoDevice, child: str) -> TapoDevice:
    """Return the socket of a power strip identified by name or index."""
    sockets = list(device.sockets) if getattr(device, "is_strip", False) else []
    if not sockets:
        raise DeviceNotFound(f"{device.nickname or 'device'} has no child sockets")
    if child.isdigit():
        index = int(child)
        if index >= len(sockets):
            raise DeviceNotFound(f"device has {len(sockets)} sockets, no index {index}")
        return sockets[index]
    wanted = child.casefold()
    for socket in sockets:
        if (socket.nickname or "").casefold() == wanted:
            return socket
    names = ", ".join(socket.nickname or "?" for socket in sockets)
    raise DeviceNotFound(f"no socket '{child}'; sockets: {names}")


class TapoBackend(Backend):
    """Tapo plugs, switches and bulbs, including TPAP firmware."""

    name = "tapo"
    credential_scope = "tplink"
    description = "TP-Link Tapo (plugp100): P100/P110/P110M plugs, switches, bulbs"

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, host: str) -> asyncio.Lock:
        """One lock per address.

        A TPAP device serves a single session at a time. Two overlapping
        connections to the same outlet make it reject one of them, so every
        operation on one address is serialised.
        """
        return self._locks.setdefault(host, asyncio.Lock())

    async def _connect(
        self,
        host: str,
        credentials: Credentials | None,
        hint: dict[str, Any] | None,
        session: aiohttp.ClientSession | None = None,
    ) -> TapoDevice:
        """Connect and update, retrying while the device holds a stale session."""
        config = DeviceConnectConfiguration(
            host=host, credentials=_to_auth(credentials), **sanitise_hint(hint)
        )
        last: Exception | None = None
        for attempt in range(_CONNECT_ATTEMPTS):
            try:
                device = await connect(config, session)
                await device.update()
            except _NOT_A_DEVICE as exc:
                if isinstance(exc, InvalidAuthentication) or _is_busy(exc):
                    last = exc
                else:
                    raise DeviceNotFound(f"cannot reach a Tapo device at {host}: {exc}") from exc
            except Exception as exc:  # plugp100 wraps transport errors in its own types
                if not _is_busy(exc) and not _is_auth(exc):
                    raise PowerctlError(f"{host}: {exc}") from exc
                last = exc
            else:
                return device
            if attempt < _CONNECT_ATTEMPTS - 1:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
        if last is not None and _is_auth(last) and not _is_busy(last):
            raise AuthRequired(f"{host} rejected the credentials for the Tapo account: {last}")
        raise PowerctlError(
            f"{host} did not accept a session after {_CONNECT_ATTEMPTS} attempts: {last}"
        )

    @staticmethod
    async def _close(device: TapoDevice) -> None:
        with contextlib.suppress(Exception):
            await device.client.close()

    @staticmethod
    @contextlib.asynccontextmanager
    async def _session() -> Any:
        """Own the HTTP session.

        plugp100 creates its own session when none is passed and does not close
        it if the protocol guess fails, which leaks an aiohttp connector for
        every address that turns out not to be a Tapo device.
        """
        session = aiohttp.ClientSession()
        try:
            yield session
        finally:
            with contextlib.suppress(Exception):
                await session.close()

    async def discover(
        self,
        *,
        target: str,
        timeout: int,
        credentials: Credentials | None,
    ) -> list[DeviceRecord]:
        # plugp100's scan takes no target: it broadcasts on the SMART discovery
        # port. The target is accepted for interface compatibility.
        discovered = await TapoDiscovery.scan(timeout=timeout)
        # The scan sends several packets, so one device can answer more than
        # once. Connecting to the same device twice makes it reject the second
        # session with ERR_STAT_ACCESS, so keep one reply per address.
        by_host: dict[str, Any] = {}
        for entry in discovered:
            by_host.setdefault(entry.ip, entry)
        records: list[DeviceRecord] = []
        for entry in by_host.values():
            family = getattr(entry, "device_type", "") or ""
            hint = connect_hint_from_discovery(entry)
            if not family.startswith("SMART."):
                continue
            base = DeviceRecord(
                backend=self.name,
                host=entry.ip,
                model=getattr(entry, "device_model", None),
                device_type=family,
                mac=_normalise_mac(getattr(entry, "mac", None)),
                device_id=getattr(entry, "device_id", None),
                supports_switching=family in _SWITCHABLE_FAMILIES,
                connect_hint=hint,
            )
            if not base.supports_switching:
                # Hubs, cameras and mesh nodes answer the same discovery; keep
                # them visible but do not pretend they can be switched.
                records.append(base)
                continue
            try:
                async with self._lock_for(entry.ip), self._session() as session:
                    device = await self._connect(entry.ip, credentials, hint, session)
                    try:
                        records.append(record_from_device(device, host=entry.ip, connect_hint=hint))
                    finally:
                        await self._close(device)
                continue
            except AuthRequired as exc:
                records.append(
                    DeviceRecord(**{**base.to_dict(), "needs_credentials": True, "error": str(exc)})
                )
                continue
            except PowerctlError as exc:
                records.append(DeviceRecord(**{**base.to_dict(), "error": str(exc)}))
                continue
        return sorted(records, key=lambda rec: rec.host)

    async def probe(self, host: str, *, credentials: Credentials | None) -> DeviceRecord | None:
        async with self._lock_for(host), self._session() as session:
            try:
                device = await self._connect(host, credentials, None, session)
            except DeviceNotFound:
                return None
            try:
                return record_from_device(device, host=host)
            finally:
                await self._close(device)

    async def status(
        self,
        record: DeviceRecord,
        *,
        credentials: Credentials | None,
        child: str | None = None,
        with_features: bool = False,
    ) -> DeviceStatus:
        async with self._lock_for(record.host), self._session() as session:
            device = await self._connect(
                record.host, credentials, record.connect_hint or None, session
            )
            try:
                return self._read_status(device, record, child, with_features)
            finally:
                await self._close(device)

    def _read_status(
        self,
        device: TapoDevice,
        record: DeviceRecord,
        child: str | None,
        with_features: bool,
    ) -> DeviceStatus:
        """Build a status result from an already connected device."""
        if child is not None:
            socket = select_socket(device, child)
            status = status_from_device(
                device, host=record.host, child=child, include_children=False
            )
            status.is_on = socket.is_on
            status.alias = socket.nickname
            return status
        status = status_from_device(device, host=record.host)
        if with_features:
            status.features = {
                key: value
                for key, value in (device.raw_state or {}).items()
                if isinstance(value, (str, int, float, bool, type(None)))
            }
        return status

    async def switch(
        self,
        record: DeviceRecord,
        *,
        on: bool,
        credentials: Credentials | None,
        child: str | None = None,
    ) -> DeviceStatus:
        async with self._lock_for(record.host), self._session() as session:
            device = await self._connect(
                record.host, credentials, record.connect_hint or None, session
            )
            try:
                target = select_socket(device, child) if child is not None else device
                if not hasattr(target, "turn_on"):
                    raise PowerctlError(f"{record.host} has no switch")
                await (target.turn_on() if on else target.turn_off())
                await device.update()
                return self._read_status(device, record, child, with_features=False)
            finally:
                await self._close(device)


def get_backend() -> Backend:
    """Entry point used by :mod:`powerctl.backends`."""
    return TapoBackend()

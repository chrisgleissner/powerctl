"""TP-Link backend, built on python-kasa.

Covers both device generations that python-kasa supports: the older IOT line
(HS100/HS110/KP115/KP303/HS300, plain XOR protocol on port 9999) and the newer
SMART/TAPO line (KLAP or AES on port 80/20002), which requires TP-Link cloud
credentials.

Connection data is cached in the registry so that later calls use
``Device.connect()`` and skip broadcast discovery. The cached data never
contains credentials: those are read from the environment or the credential
file on every call.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from kasa import (
    AuthenticationError,
    Device,
    DeviceConfig,
    DeviceConnectionParameters,
    Discover,
    KasaException,
    Module,
    UnsupportedDeviceError,
)
from kasa import (
    Credentials as KasaCredentials,
)

from ..errors import AuthRequired, DeviceNotFound, PowerctlError
from ..secrets import Credentials, scrub
from .base import Backend, DeviceRecord, DeviceStatus, EnergyReading

#: Families that answer TP-Link discovery but hold no switchable outlet. They are
#: reported so the user can see what is on the network, with an explanation
#: rather than a protocol error.
_NOT_SWITCHABLE_FAMILIES = {
    "HOMEWIFISYSTEM": "Deco mesh node, no switchable outlet",
    "IOT.IPCAMERA": "camera, no switchable outlet",
    "SMART.IPCAMERA": "camera, no switchable outlet",
    "SMART.TAPOROBOVAC": "robot vacuum, no switchable outlet",
}

#: Device types that expose a relay we can switch.
_SWITCHABLE = {
    "plug",
    "strip",
    "stripsocket",
    "wallswitch",
    "dimmer",
    "bulb",
    "lightstrip",
    "fan",
}


def _discovery_field(result: Any, name: str) -> Any:
    """Read a field from a discovery result, which may be a dict or an object.

    python-kasa passes the raw discovery payload for unsupported devices, and its
    shape differs between protocol versions, so both forms are handled.
    """
    if result is None:
        return None
    if isinstance(result, dict):
        if name in result:
            return result[name]
        inner = result.get("result")
        if isinstance(inner, dict):
            return inner.get(name)
        return None
    return getattr(result, name, None)


def _to_kasa_credentials(creds: Credentials | None) -> KasaCredentials | None:
    if creds is None:
        return None
    return KasaCredentials(creds.username, creds.password)


def _connect_hint(device: Device) -> dict[str, Any]:
    """Return reconnect parameters for ``device`` with every credential removed."""
    hint = device.config.to_dict_control_credentials(credentials_hash="")
    hint.pop("credentials", None)
    hint.pop("credentials_hash", None)
    hint.pop("aes_keys", None)
    return hint


def _energy_of(device: Device) -> EnergyReading | None:
    energy = device.modules.get(Module.Energy)
    if energy is None:
        return None
    reading = EnergyReading()
    for field, attr in (
        ("power_w", "current_consumption"),
        ("voltage_v", "voltage"),
        ("current_a", "current"),
        ("today_kwh", "consumption_today"),
        ("this_month_kwh", "consumption_this_month"),
        ("total_kwh", "consumption_total"),
    ):
        with contextlib.suppress(AttributeError, KasaException):
            setattr(reading, field, getattr(energy, attr))
    return reading


def _has_energy(device: Device) -> bool:
    if device.modules.get(Module.Energy) is not None:
        return True
    return any(child.modules.get(Module.Energy) is not None for child in device.children)


def _record_from_device(device: Device, *, needs_credentials: bool = False) -> DeviceRecord:
    device_type = device.device_type.value
    return DeviceRecord(
        backend=KasaBackend.name,
        host=device.host,
        alias=device.alias,
        model=device.model,
        device_type=device_type,
        mac=device.mac or None,
        device_id=device.device_id or None,
        supports_switching=device_type in _SWITCHABLE,
        supports_energy=_has_energy(device),
        needs_credentials=needs_credentials,
        children=[child.alias or f"child_{index}" for index, child in enumerate(device.children)],
        connect_hint=_connect_hint(device),
    )


def _status_from_device(
    device: Device,
    *,
    child: str | None = None,
    with_features: bool = False,
    include_children: bool = True,
) -> DeviceStatus:
    status = DeviceStatus(
        backend=KasaBackend.name,
        host=device.host,
        alias=device.alias,
        model=device.model,
        device_type=device.device_type.value,
        mac=device.mac or None,
        is_on=device.is_on if device.device_type.value in _SWITCHABLE else None,
        energy=_energy_of(device),
        child=child,
    )
    if with_features:
        status.features = {
            fid: scrub(feature.value)
            for fid, feature in device.features.items()
            if _feature_value_is_safe(feature)
        }
    if include_children:
        status.children = [
            _status_from_device(sub, with_features=with_features, include_children=False)
            for sub in device.children
        ]
    return status


def _feature_value_is_safe(feature: Any) -> bool:
    try:
        value = feature.value
    except Exception:  # pragma: no cover - some features raise when unsupported
        return False
    return isinstance(value, (str, int, float, bool, type(None)))


def _select_child(device: Device, child: str) -> Device:
    """Return the child socket identified by alias, id or index."""
    if not device.children:
        raise DeviceNotFound(f"{device.host} has no child sockets")
    if child.isdigit():
        index = int(child)
        if index >= len(device.children):
            raise DeviceNotFound(
                f"{device.host} has {len(device.children)} sockets, no index {index}"
            )
        return device.children[index]
    wanted = child.casefold()
    for sub in device.children:
        if (sub.alias or "").casefold() == wanted or (sub.device_id or "").casefold() == wanted:
            return sub
    names = ", ".join(sub.alias or "?" for sub in device.children)
    raise DeviceNotFound(f"{device.host} has no socket '{child}'; sockets: {names}")


class KasaBackend(Backend):
    """TP-Link Kasa and Tapo outlets, switches and bulbs."""

    name = "kasa"
    credential_scope = "tplink"
    description = "TP-Link Kasa IOT (python-kasa): KP115/HS110/HS300 plugs, strips, switches, bulbs"

    async def discover(
        self,
        *,
        target: str,
        timeout: int,
        credentials: Credentials | None,
    ) -> list[DeviceRecord]:
        unsupported: list[DeviceRecord] = []

        async def on_unsupported(info: Any) -> None:
            """Record a device that answered discovery but speaks no known protocol.

            This is how a TPAP-only device such as a recent Tapo P110M shows up on
            a build of python-kasa without TPAP support. Reporting it is better
            than dropping it, because the address is the thing the user needs.
            """
            host = str(getattr(info, "host", None) or "unknown host")
            result = getattr(info, "discovery_result", None)
            family = str(_discovery_field(result, "device_type") or "")
            if family.startswith("SMART."):
                # Tapo devices are the tapo adapter's job; reporting them here
                # would list every one of them twice.
                return
            model = _discovery_field(result, "device_model")
            explanation = _NOT_SWITCHABLE_FAMILIES.get(family.upper())
            reason = explanation or str(
                getattr(info, "error", None) or "protocol not supported by this build"
            )
            unsupported.append(
                DeviceRecord(
                    backend=self.name,
                    host=host,
                    model=model,
                    device_type=family or None,
                    supports_switching=False,
                    error=reason,
                )
            )

        found = await Discover.discover(
            target=target,
            discovery_timeout=timeout,
            credentials=_to_kasa_credentials(credentials),
            on_unsupported=on_unsupported,
        )
        results = await asyncio.gather(*(self._record_for(device) for device in found.values()))
        records = [record for record in results if record is not None]
        known = {rec.host for rec in records}
        records.extend(rec for rec in unsupported if rec.host not in known)
        return sorted(records, key=lambda rec: rec.host)

    @staticmethod
    def _is_iot(device: Device) -> bool:
        """True if the device belongs to the Kasa IOT family this adapter owns.

        python-kasa can also speak to older Tapo hardware, but powerctl gives
        every Tapo device to the tapo adapter. Ownership has to be exclusive:
        these devices accept one session at a time, so a second adapter opening
        its own connection makes the device answer ERR_STAT_ACCESS.
        """
        family = device.config.connection_type.device_family.value
        return family.startswith("IOT")

    async def _record_for(self, device: Device) -> DeviceRecord | None:
        """Update a freshly discovered device, tolerating missing credentials."""
        if not self._is_iot(device):
            with contextlib.suppress(Exception):
                await device.disconnect()
            return None
        try:
            await device.update()
        except AuthenticationError:
            record = DeviceRecord(
                backend=self.name,
                host=device.host,
                model=device.model,
                device_type=device.device_type.value,
                mac=device.mac or None,
                needs_credentials=True,
                connect_hint=_connect_hint(device),
                error="authentication required",
            )
            return record
        except KasaException as exc:
            return DeviceRecord(
                backend=self.name,
                host=device.host,
                model=device.model,
                device_type=device.device_type.value,
                connect_hint=_connect_hint(device),
                error=str(exc),
            )
        finally:
            with contextlib.suppress(Exception):
                await device.disconnect()
        return _record_from_device(device)

    async def _connect(self, record: DeviceRecord, credentials: Credentials | None) -> Device:
        """Connect using the cached hint, falling back to a direct connection."""
        kasa_creds = _to_kasa_credentials(credentials)
        hint = dict(record.connect_hint or {})
        errors: list[str] = []
        if hint:
            hint["host"] = record.host
            hint.pop("credentials", None)
            hint.pop("credentials_hash", None)
            if kasa_creds is not None:
                hint["credentials"] = {
                    "username": kasa_creds.username,
                    "password": kasa_creds.password,
                }
            try:
                config = DeviceConfig.from_dict(hint)
                return await Device.connect(config=config)
            except AuthenticationError as exc:
                raise AuthRequired(f"{record.host} requires TP-Link credentials: {exc}") from exc
            except (KasaException, ValueError, TypeError) as exc:
                errors.append(f"cached connection parameters: {exc}")
        try:
            device = await Discover.discover_single(record.host, credentials=kasa_creds)
        except AuthenticationError as exc:
            raise AuthRequired(f"{record.host} requires TP-Link credentials: {exc}") from exc
        except KasaException as exc:
            errors.append(f"discovery: {exc}")
            raise DeviceNotFound(f"cannot reach {record.host} ({'; '.join(errors)})") from exc
        if device is None:
            raise DeviceNotFound(f"cannot reach {record.host}")
        await device.update()
        return device

    async def probe(
        self,
        host: str,
        *,
        credentials: Credentials | None,
    ) -> DeviceRecord | None:
        """Try every Kasa protocol against one address.

        Returns None when the address holds no device this adapter owns, so the
        caller can offer it to the next adapter. A Tapo device is not this
        adapter's device even when python-kasa recognises the family, because
        powerctl gives all SMART.* families to the tapo adapter.
        """
        if not await self._owns(host, credentials):
            return None
        try:
            device = await Discover.try_connect_all(
                host, credentials=_to_kasa_credentials(credentials), timeout=5
            )
        except AuthenticationError as exc:
            raise AuthRequired(f"{host} requires TP-Link credentials: {exc}") from exc
        if device is None:
            await self._explain_probe_failure(host, credentials)
            return None
        try:
            await device.update()
            return _record_from_device(device)
        except AuthenticationError as exc:
            raise AuthRequired(f"{host} requires TP-Link credentials: {exc}") from exc
        finally:
            with contextlib.suppress(Exception):
                await device.disconnect()

    async def _owns(self, host: str, credentials: Credentials | None) -> bool:
        """True if the address answers as a Kasa IOT device.

        A unicast discovery is cheap and says which family the device belongs to.
        An UnsupportedDeviceError means python-kasa cannot speak to it at all,
        which is exactly the TPAP case that belongs to the tapo adapter. When the
        query fails for any other reason the answer is True, so a device that
        ignores discovery still gets the full protocol walk.
        """
        try:
            device = await Discover.discover_single(
                host,
                credentials=_to_kasa_credentials(credentials),
                discovery_timeout=4,
            )
        except UnsupportedDeviceError:
            return False
        except AuthenticationError:
            return True
        except KasaException:
            return True
        if device is None:
            return True
        try:
            return self._is_iot(device)
        finally:
            with contextlib.suppress(Exception):
                await device.disconnect()

    async def _explain_probe_failure(self, host: str, credentials: Credentials | None) -> None:
        """Tell "no device here" apart from "device here, credentials rejected".

        ``Discover.try_connect_all`` swallows the error of every attempt, so a
        Tapo device that refuses the supplied account looks exactly like an
        address with nothing on it. This retries the two SMART protocol variants
        on their own and lets the authentication error surface.
        """
        for encryption, login_version in (("KLAP", 2), ("AES", 2), ("KLAP", None)):
            config = DeviceConfig(
                host=host,
                credentials=_to_kasa_credentials(credentials),
                connection_type=DeviceConnectionParameters.from_values(
                    device_family="SMART.TAPOPLUG",
                    encryption_type=encryption,
                    login_version=login_version,
                    https=False,
                ),
            )
            try:
                device = await Device.connect(config=config)
            except AuthenticationError as exc:
                raise AuthRequired(
                    f"{host} is a TP-Link SMART/Tapo device, but it rejected the "
                    f"credentials for account "
                    f"'{credentials.username if credentials else 'none'}': {exc}. "
                    f"Check the password with 'powerctl login --backend kasa', and "
                    f"that this account owns the device in the Tapo app."
                ) from exc
            except (KasaException, OSError):
                continue
            else:
                with contextlib.suppress(Exception):
                    await device.disconnect()
                return

    async def status(
        self,
        record: DeviceRecord,
        *,
        credentials: Credentials | None,
        child: str | None = None,
        with_features: bool = False,
    ) -> DeviceStatus:
        device = await self._connect(record, credentials)
        try:
            if child is not None:
                sub = _select_child(device, child)
                return _status_from_device(
                    sub, child=child, with_features=with_features, include_children=False
                )
            return _status_from_device(device, with_features=with_features)
        finally:
            with contextlib.suppress(Exception):
                await device.disconnect()

    async def switch(
        self,
        record: DeviceRecord,
        *,
        on: bool,
        credentials: Credentials | None,
        child: str | None = None,
    ) -> DeviceStatus:
        device = await self._connect(record, credentials)
        try:
            target = _select_child(device, child) if child is not None else device
            if target.device_type.value not in _SWITCHABLE:
                raise PowerctlError(
                    f"{record.host} is a {target.device_type.value}, it has no switch"
                )
            if on:
                await target.turn_on()
            else:
                await target.turn_off()
            await device.update()
            if child is not None:
                target = _select_child(device, child)
                return _status_from_device(target, child=child, include_children=False)
            return _status_from_device(device)
        finally:
            with contextlib.suppress(Exception):
                await device.disconnect()


def get_backend() -> Backend:
    """Entry point used by :mod:`powerctl.backends`."""
    return KasaBackend()

"""On-disk cache of discovered devices.

The registry lets ``powerctl on kitchen`` work without a broadcast discovery on
every call, and gives devices stable names. It stores only addressing and
protocol data. Credentials are never written here; see
:func:`powerctl.secrets.scrub`, which every record passes through on the way in.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .backends.base import DeviceRecord
from .errors import DeviceNotFound, PowerctlError
from .secrets import config_home, scrub

REGISTRY_VERSION = 1

_MAC_RE = re.compile(r"^([0-9a-f]{2}[:-]){5}[0-9a-f]{2}$", re.IGNORECASE)


def registry_path() -> Path:
    """Return the path of the device registry file."""
    return config_home() / "devices.json"


def protection_path() -> Path:
    """Return the path of the protected device list.

    Protections live in their own file, separate from the device cache, because
    the cache is rewritten by every discovery and can be deleted to force a
    rescan. A safety rule must not disappear with a cache.
    """
    return config_home() / "protected.json"


def looks_like_host(value: str) -> bool:
    """True if ``value`` is an IP address or a dotted host name."""
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return "." in value and " " not in value


@dataclass(frozen=True)
class Protection:
    """One protected device, identified by every name it is known under.

    A protection that only stored a display name could be bypassed by naming the
    same device a different way: after the device cache was rebuilt, the address
    no longer resolved to the alias, so the name did not match and the guard let
    the power cut through. Every identifier of the device is therefore stored,
    and a match on any one of them protects it.
    """

    name: str
    host: str | None = None
    mac: str | None = None
    device_id: str | None = None
    #: A critical device is never switched off by this tool. Unlike an ordinary
    #: protection, no command line flag overrides it: the entry has to be removed
    #: from the protection file first. Use it for anything whose power loss is
    #: expensive or dangerous, such as a fridge, a router or a server.
    critical: bool = False

    def identifiers(self) -> set[str]:
        """Return every identifier that refers to this protected device."""
        values = (self.name, self.host, self.mac, self.device_id)
        return {_key(value) for value in values if value}

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "name": self.name,
                "host": self.host,
                "mac": self.mac,
                "device_id": self.device_id,
            }.items()
            if value
        } | ({"critical": True} if self.critical else {})

    @classmethod
    def from_any(cls, entry: Any) -> Protection:
        """Accept both the current object form and the old plain string form."""
        if isinstance(entry, str):
            return cls(name=entry, host=entry if looks_like_host(entry) else None)
        return cls(
            name=str(entry.get("name")),
            host=entry.get("host"),
            mac=entry.get("mac"),
            device_id=entry.get("device_id"),
            critical=bool(entry.get("critical", False)),
        )


def _key(value: str) -> str:
    """Normalise an identifier for comparison: case and MAC separators."""
    return value.replace("-", ":").casefold().strip()


@dataclass
class Registry:
    """The set of known devices plus the list of protected device keys."""

    devices: list[DeviceRecord] = field(default_factory=list)
    protected: list[Protection] = field(default_factory=list)
    path: Path | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> Registry:
        path = path or registry_path()
        protected = cls._load_protected()
        if not path.exists():
            return cls(protected=protected, path=path)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise PowerctlError(f"cannot read registry {path}: {exc}") from exc
        devices = [DeviceRecord.from_dict(item) for item in data.get("devices", [])]
        # Entries written by an older version that kept protections in the device
        # file are carried over rather than dropped.
        known = {key for entry in protected for key in entry.identifiers()}
        legacy = [
            Protection.from_any(entry)
            for entry in data.get("protected", [])
            if not (Protection.from_any(entry).keys() & known)
        ]
        return cls(devices=devices, protected=protected + legacy, path=path)

    @staticmethod
    def _load_protected() -> list[Protection]:
        path = protection_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise PowerctlError(f"cannot read {path}: {exc}") from exc
        entries = data.get("protected", data) if isinstance(data, dict) else data
        if not isinstance(entries, list):
            raise PowerctlError(f"{path}: expected a list of protected device names")
        return [Protection.from_any(entry) for entry in entries]

    def save_protected(self) -> Path:
        """Write the protected list to its own file."""
        path = protection_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": REGISTRY_VERSION,
            "updated": datetime.now(UTC).isoformat(timespec="seconds"),
            "protected": [entry.to_dict() for entry in self._unique_protections()],
        }
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
        return path

    def save(self, path: Path | None = None) -> Path:
        path = path or self.path or registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": REGISTRY_VERSION,
            "updated": datetime.now(UTC).isoformat(timespec="seconds"),
            "devices": [scrub(record.to_dict()) for record in self.devices],
        }
        self.save_protected()
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
        self.path = path
        return path

    def upsert(self, record: DeviceRecord) -> None:
        """Add ``record``, replacing any existing entry for the same device.

        Devices are matched on MAC address when both entries have one, because a
        DHCP lease can move a device to a different address.
        """
        for index, existing in enumerate(self.devices):
            same_mac = bool(record.mac) and existing.mac == record.mac
            # One address is one physical outlet, whichever adapter reached it:
            # both adapters see the same device on the network, and the later
            # record wins so the adapter that can actually drive it is kept.
            same_host = existing.host == record.host
            if same_mac or same_host:
                self.devices[index] = record
                return
        self.devices.append(record)

    def replace_backend(self, backend: str, records: list[DeviceRecord]) -> None:
        """Replace all entries of one backend with a fresh discovery result."""
        self.devices = [rec for rec in self.devices if rec.backend != backend]
        self.devices.extend(records)

    def keys_for(self, record: DeviceRecord) -> list[str]:
        """Return every name that resolves to ``record``."""
        keys = [record.host]
        if record.alias:
            keys.append(record.alias)
        if record.mac:
            keys.append(record.mac)
        if record.device_id:
            keys.append(record.device_id)
        return keys

    def find(self, name: str, backend: str | None = None) -> DeviceRecord | None:
        """Resolve ``name`` against alias, host, MAC or device id (case-insensitive)."""
        wanted = name.casefold()
        candidates = [rec for rec in self.devices if backend is None or rec.backend == backend]
        matches = [
            rec for rec in candidates if any(key.casefold() == wanted for key in self.keys_for(rec))
        ]
        if len(matches) > 1:
            hosts = ", ".join(rec.host for rec in matches)
            raise DeviceNotFound(f"'{name}' matches several devices: {hosts}")
        if matches:
            return matches[0]
        if _MAC_RE.match(name):
            normalised = name.replace("-", ":").casefold()
            for rec in candidates:
                if rec.mac and rec.mac.replace("-", ":").casefold() == normalised:
                    return rec
        prefix = [
            rec for rec in candidates if rec.alias and rec.alias.casefold().startswith(wanted)
        ]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            names = ", ".join(rec.alias or rec.host for rec in prefix)
            raise DeviceNotFound(f"'{name}' is ambiguous, it matches: {names}")
        return None

    def _unique_protections(self) -> list[Protection]:
        seen: dict[str, Protection] = {}
        for entry in self.protected:
            seen.setdefault(entry.name.casefold(), entry)
        return sorted(seen.values(), key=lambda entry: entry.name.casefold())

    def protected_keys(self) -> set[str]:
        """Every identifier that is protected, normalised for comparison."""
        return {key for entry in self.protected for key in entry.identifiers()}

    def protection_for(self, record: DeviceRecord) -> Protection | None:
        """Return the protection covering ``record``, or None."""
        keys = {_key(key) for key in self.keys_for(record) if key}
        for entry in self.protected:
            if entry.identifiers() & keys:
                return entry
        return None

    def is_critical(self, record: DeviceRecord) -> bool:
        """True if ``record`` is protected as critical."""
        entry = self.protection_for(record)
        return bool(entry and entry.critical)

    def critical_keys(self) -> set[str]:
        """Every identifier that is protected as critical."""
        return {key for entry in self.protected if entry.critical for key in entry.identifiers()}

    def is_protected(self, record: DeviceRecord) -> bool:
        """True if any identifier of ``record`` is protected.

        Alias, address, MAC and device id are all compared, so a protected
        device stays protected however it is addressed.
        """
        protected = self.protected_keys()
        return any(_key(key) in protected for key in self.keys_for(record) if key)

    def protect(
        self,
        record: DeviceRecord | None = None,
        name: str | None = None,
        *,
        critical: bool = False,
    ) -> Protection:
        """Protect a device, storing every identifier known for it."""
        if record is not None:
            entry = Protection(
                name=record.alias or record.host,
                host=record.host,
                mac=record.mac,
                device_id=record.device_id,
                critical=critical,
            )
        elif name is not None:
            entry = Protection(
                name=name,
                host=name if looks_like_host(name) else None,
                critical=critical,
            )
        else:
            raise ValueError("protect() needs a record or a name")
        existing = {key for item in self.protected for key in item.identifiers()}
        if not (entry.identifiers() & existing):
            self.protected.append(entry)
            return entry
        # Merge newly learnt identifiers into the entry that already matches.
        for index, item in enumerate(self.protected):
            if item.identifiers() & entry.identifiers():
                self.protected[index] = Protection(
                    name=item.name,
                    host=item.host or entry.host,
                    mac=item.mac or entry.mac,
                    device_id=item.device_id or entry.device_id,
                    # Criticality is sticky: re-protecting without --critical
                    # must not quietly downgrade an entry.
                    critical=item.critical or entry.critical,
                )
                return self.protected[index]
        return entry

    def unprotect(self, name: str, *, allow_critical: bool = False) -> bool:
        wanted = _key(name)
        matches = [entry for entry in self.protected if wanted in entry.identifiers()]
        critical = [entry for entry in matches if entry.critical]
        if critical and not allow_critical:
            names = ", ".join(entry.name for entry in critical)
            raise PowerctlError(
                f"'{names}' is protected as critical; edit {protection_path()} by hand to remove it"
            )
        for entry in matches:
            self.protected.remove(entry)
        return bool(matches)

    def to_dict(self) -> dict[str, Any]:
        return {
            "devices": [scrub(rec.to_dict()) for rec in self.devices],
            "protected": [entry.to_dict() for entry in self._unique_protections()],
        }

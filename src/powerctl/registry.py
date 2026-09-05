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
from datetime import datetime, timezone
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


def looks_like_host(value: str) -> bool:
    """True if ``value`` is an IP address or a dotted host name."""
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return "." in value and " " not in value


@dataclass
class Registry:
    """The set of known devices plus the list of protected device keys."""

    devices: list[DeviceRecord] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    path: Path | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> Registry:
        path = path or registry_path()
        if not path.exists():
            return cls(path=path)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise PowerctlError(f"cannot read registry {path}: {exc}") from exc
        devices = [DeviceRecord.from_dict(item) for item in data.get("devices", [])]
        return cls(devices=devices, protected=list(data.get("protected", [])), path=path)

    def save(self, path: Path | None = None) -> Path:
        path = path or self.path or registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": REGISTRY_VERSION,
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "devices": [scrub(record.to_dict()) for record in self.devices],
            "protected": sorted(set(self.protected)),
        }
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
            same_host = existing.host == record.host and existing.backend == record.backend
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
        candidates = [
            rec for rec in self.devices if backend is None or rec.backend == backend
        ]
        matches = [
            rec
            for rec in candidates
            if any(key.casefold() == wanted for key in self.keys_for(rec))
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
            rec
            for rec in candidates
            if rec.alias and rec.alias.casefold().startswith(wanted)
        ]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            names = ", ".join(rec.alias or rec.host for rec in prefix)
            raise DeviceNotFound(f"'{name}' is ambiguous, it matches: {names}")
        return None

    def is_protected(self, record: DeviceRecord) -> bool:
        """True if any name of ``record`` is on the protected list."""
        protected = {entry.casefold() for entry in self.protected}
        return any(key.casefold() in protected for key in self.keys_for(record))

    def protect(self, name: str) -> None:
        if name not in self.protected:
            self.protected.append(name)

    def unprotect(self, name: str) -> bool:
        matches = [entry for entry in self.protected if entry.casefold() == name.casefold()]
        for entry in matches:
            self.protected.remove(entry)
        return bool(matches)

    def to_dict(self) -> dict[str, Any]:
        return {
            "devices": [scrub(rec.to_dict()) for rec in self.devices],
            "protected": sorted(set(self.protected)),
        }

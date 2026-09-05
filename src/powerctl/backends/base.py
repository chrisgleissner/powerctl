"""Backend interface and the data types every backend returns.

A backend talks to one family of switchable outlets. The CLI, the registry and
the power-cycle logic only use the types in this module, so adding support for
another vendor means adding a module next to ``kasa.py`` and registering it in
``powerctl.backends.__init__``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

from ..secrets import Credentials


@dataclass(frozen=True)
class DeviceRecord:
    """Everything needed to find and reconnect to a device, without secrets.

    ``connect_hint`` is backend specific connection data (protocol, encryption,
    port). It must never contain credentials; :class:`powerctl.registry.Registry`
    writes it to disk verbatim.
    """

    backend: str
    host: str
    alias: str | None = None
    model: str | None = None
    device_type: str | None = None
    mac: str | None = None
    device_id: str | None = None
    supports_switching: bool = False
    supports_energy: bool = False
    needs_credentials: bool = False
    children: list[str] = field(default_factory=list)
    connect_hint: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeviceRecord:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class EnergyReading:
    """Instantaneous and cumulative energy figures. Fields are None if unsupported."""

    power_w: float | None = None
    voltage_v: float | None = None
    current_a: float | None = None
    today_kwh: float | None = None
    this_month_kwh: float | None = None
    total_kwh: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DeviceStatus:
    """The result of querying one device (or one socket of a power strip)."""

    backend: str
    host: str
    alias: str | None = None
    model: str | None = None
    device_type: str | None = None
    mac: str | None = None
    #: True if powered on, False if off, None if the device has no switch.
    is_on: bool | None = None
    energy: EnergyReading | None = None
    children: list[DeviceStatus] = field(default_factory=list)
    features: dict[str, Any] | None = None
    #: Name of the selected child socket, if a child was addressed.
    child: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "backend": self.backend,
            "host": self.host,
            "alias": self.alias,
            "model": self.model,
            "device_type": self.device_type,
            "mac": self.mac,
            "state": None if self.is_on is None else ("on" if self.is_on else "off"),
            "energy": self.energy.to_dict() if self.energy else None,
            "children": [child.to_dict() for child in self.children],
        }
        if self.child is not None:
            data["child"] = self.child
        if self.features is not None:
            data["features"] = self.features
        return data


class Backend(ABC):
    """Protocol implemented by every device family powerctl can drive."""

    #: Short identifier used in the registry, on the command line and for the
    #: credential environment variables (``POWERCTL_<NAME>_USERNAME``).
    name: str = "base"
    #: Human readable description shown by ``powerctl backends``.
    description: str = ""

    @abstractmethod
    async def discover(
        self,
        *,
        target: str,
        timeout: int,
        credentials: Credentials | None,
    ) -> list[DeviceRecord]:
        """Find devices of this family on the local network."""

    @abstractmethod
    async def status(
        self,
        record: DeviceRecord,
        *,
        credentials: Credentials | None,
        child: str | None = None,
        with_features: bool = False,
    ) -> DeviceStatus:
        """Query power state and, where available, energy readings."""

    @abstractmethod
    async def switch(
        self,
        record: DeviceRecord,
        *,
        on: bool,
        credentials: Credentials | None,
        child: str | None = None,
    ) -> DeviceStatus:
        """Switch the device (or one child socket) on or off and return its new state."""

"""Backend registry.

Backends are imported lazily so that a missing optional dependency only breaks
that one backend rather than the whole CLI.
"""

from __future__ import annotations

from ..errors import UsageError
from .base import Backend, DeviceRecord, DeviceStatus, EnergyReading

_LOADERS: dict[str, str] = {
    # backend name -> module path providing get_backend()
    # Two adapters on purpose: the Kasa IOT protocol is frozen and served well by
    # python-kasa, while Tapo firmware keeps changing and needs a library that
    # releases often, currently plugp100.
    "kasa": "powerctl.backends.kasa_backend",
    "tapo": "powerctl.backends.tapo_backend",
}

_CACHE: dict[str, Backend] = {}


def backend_names() -> list[str]:
    """Return the names of all registered backends."""
    return sorted(_LOADERS)


def get_backend(name: str) -> Backend:
    """Return the backend instance called ``name``."""
    if name in _CACHE:
        return _CACHE[name]
    if name not in _LOADERS:
        raise UsageError(f"unknown backend '{name}'; known backends: {', '.join(backend_names())}")
    import importlib

    module = importlib.import_module(_LOADERS[name])
    backend = module.get_backend()
    _CACHE[name] = backend
    return backend


def all_backends() -> list[Backend]:
    """Return every backend that can be imported in this environment."""
    backends = []
    for name in backend_names():
        try:
            backends.append(get_backend(name))
        except ImportError:  # pragma: no cover - depends on the environment
            continue
    return backends


__all__ = [
    "Backend",
    "DeviceRecord",
    "DeviceStatus",
    "EnergyReading",
    "all_backends",
    "backend_names",
    "get_backend",
]

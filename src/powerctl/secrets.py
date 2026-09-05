"""Credential loading and redaction.

Rules that the rest of the code base relies on:

* Passwords are never accepted as command line arguments, because process
  arguments are readable by other local users through ``/proc`` and end up in
  shell history.
* Passwords are never written to the device registry or to any log or JSON
  output. :func:`redact` is applied to every string that leaves the process.
* The credential file must not be readable by group or other. A file with wider
  permissions is rejected instead of being used.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .errors import PowerctlError

#: Environment variables checked for each backend, in order. ``{B}`` is
#: replaced by the upper case backend name.
_ENV_USER = ("POWERCTL_{B}_USERNAME", "{B}_USERNAME")
_ENV_PASS = ("POWERCTL_{B}_PASSWORD", "{B}_PASSWORD")

_MIN_REDACT_LEN = 4


@dataclass(frozen=True)
class Credentials:
    """A username/password pair for one backend."""

    username: str
    password: str
    source: str = "unknown"

    def __repr__(self) -> str:  # pragma: no cover - defensive only
        return f"Credentials(username={self.username!r}, password='***', source={self.source!r})"

    __str__ = __repr__


def config_home() -> Path:
    """Return the directory holding the registry and the credential file."""
    override = os.environ.get("POWERCTL_HOME")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base).expanduser() / "powerctl"


def credentials_path() -> Path:
    """Return the path of the credential file."""
    return config_home() / "credentials.json"


def _check_file_mode(path: Path) -> None:
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PowerctlError(
            f"{path} is readable by group or other; run "
            f"'chmod 600 {path}' before using it"
        )


def load_credentials(backend: str) -> Credentials | None:
    """Return credentials for ``backend`` from the environment or the file.

    Environment variables take precedence over the credential file so that a
    one-off run can override stored values without editing the file.
    """
    upper = backend.upper()
    for user_var, pass_var in zip(_ENV_USER, _ENV_PASS, strict=True):
        username = os.environ.get(user_var.format(B=upper))
        password = os.environ.get(pass_var.format(B=upper))
        if username and password:
            return Credentials(username, password, source=f"env:{user_var.format(B=upper)}")

    path = credentials_path()
    if not path.exists():
        return None
    _check_file_mode(path)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise PowerctlError(f"cannot read {path}: {exc}") from exc
    entry = data.get(backend)
    if not entry:
        return None
    username, password = entry.get("username"), entry.get("password")
    if not username or not password:
        raise PowerctlError(f"{path}: entry for '{backend}' lacks username or password")
    return Credentials(username, password, source=f"file:{path}")


def store_credentials(backend: str, username: str, password: str) -> Path:
    """Write credentials for ``backend`` to the credential file with mode 0600."""
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, dict[str, str]] = {}
    if path.exists():
        _check_file_mode(path)
        try:
            data = json.loads(path.read_text())
        except ValueError:
            data = {}
    data[backend] = {"username": username, "password": password}
    # Create with restrictive permissions before any content is written.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    os.chmod(path, 0o600)
    return path


def forget_credentials(backend: str) -> bool:
    """Remove the stored credentials for ``backend``. Returns True if removed."""
    path = credentials_path()
    if not path.exists():
        return False
    _check_file_mode(path)
    data = json.loads(path.read_text())
    if backend not in data:
        return False
    del data[backend]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    return True


class Redactor:
    """Replaces known secret values with ``***`` in any text leaving the process."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def add(self, value: str | None) -> None:
        """Register a secret value. Very short values are ignored."""
        if value and len(value) >= _MIN_REDACT_LEN:
            self._secrets.add(value)

    def add_credentials(self, creds: Credentials | None) -> None:
        if creds is not None:
            self.add(creds.password)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text


#: Process wide redactor. Every credential that is loaded registers itself here.
REDACTOR = Redactor()


def scrub(obj: object) -> object:
    """Recursively drop credential-like keys and redact known secret values."""
    secret_keys = {"password", "credentials", "credentials_hash", "aes_keys", "token"}
    if isinstance(obj, dict):
        return {
            key: ("***" if key in secret_keys else scrub(value))
            for key, value in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [scrub(item) for item in obj]
    if isinstance(obj, str):
        return REDACTOR(obj)
    return obj

"""Exception types and process exit codes used by powerctl."""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_TIMEOUT = 4


class PowerctlError(Exception):
    """Base class for all errors raised by powerctl."""

    exit_code = EXIT_ERROR


class UsageError(PowerctlError):
    """The command line arguments were not usable."""

    exit_code = EXIT_USAGE


class DeviceNotFound(PowerctlError):
    """The requested device could not be resolved or reached."""


class AuthRequired(PowerctlError):
    """The device needs credentials that are not available."""


class RefusedError(PowerctlError):
    """A safety guard refused the requested action."""

    exit_code = EXIT_REFUSED


class WaitTimeout(PowerctlError):
    """A host did not come back within the configured timeout."""

    exit_code = EXIT_TIMEOUT

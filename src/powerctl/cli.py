"""Command line interface for powerctl."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import stat
import sys
from typing import Any

from . import __version__, core
from .backends import all_backends, backend_names
from .backends.base import DeviceRecord, DeviceStatus
from .core import DEFAULT_BACKEND, DEFAULT_OFF_SECONDS, Session
from .errors import EXIT_OK, DeviceNotFound, PowerctlError, UsageError
from .netutil import default_broadcast
from .registry import registry_path
from .secrets import (
    REDACTOR,
    credentials_path,
    forget_credentials,
    load_credentials,
    store_credentials,
)


def out(text: str = "") -> None:
    """Print to stdout with every known secret redacted."""
    print(REDACTOR(text))


def emit_json(payload: Any) -> None:
    out(json.dumps(payload, indent=2, sort_keys=False))


def _fmt(value: float | None, unit: str, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f} {unit}"


def _state(is_on: bool | None) -> str:
    if is_on is None:
        return "-"
    return "on" if is_on else "off"


def print_records(
    records: list[DeviceRecord],
    registry_protected: set[str],
    critical: set[str] | None = None,
) -> None:
    if not records:
        out("No devices found.")
        return
    rows = [("HOST", "ALIAS", "MODEL", "TYPE", "ENERGY", "FLAGS")]
    for rec in records:
        flags = []
        if rec.needs_credentials:
            flags.append("auth")
        if rec.children:
            flags.append(f"{len(rec.children)} sockets")
        keys = {
            key.replace("-", ":").casefold()
            for key in (rec.host, rec.alias or "", rec.mac or "")
            if key
        }
        if keys & (critical or set()):
            flags.append("critical")
        elif keys & registry_protected:
            flags.append("protected")
        if rec.error:
            flags.append(rec.error)
        rows.append(
            (
                rec.host,
                rec.alias or "-",
                rec.model or "-",
                rec.device_type or "-",
                "yes" if rec.supports_energy else "no",
                ", ".join(flags) or "-",
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for index, row in enumerate(rows):
        out("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if index == 0:
            out("  ".join("-" * width for width in widths))


def print_status(status: DeviceStatus, indent: str = "") -> None:
    name = status.alias or status.host
    head = f"{indent}{name}  [{_state(status.is_on)}]"
    if status.child:
        head += f"  socket={status.child}"
    out(head)
    details = f"{indent}  host={status.host}"
    if status.model:
        details += f"  model={status.model}"
    if status.device_type:
        details += f"  type={status.device_type}"
    if status.mac:
        details += f"  mac={status.mac}"
    out(details)
    energy = status.energy
    if energy and any(
        value is not None
        for value in (
            energy.power_w,
            energy.voltage_v,
            energy.current_a,
            energy.today_kwh,
            energy.this_month_kwh,
            energy.total_kwh,
        )
    ):
        out(
            f"{indent}  power={_fmt(energy.power_w, 'W')}"
            f"  voltage={_fmt(energy.voltage_v, 'V')}"
            f"  current={_fmt(energy.current_a, 'A', 3)}"
        )
        out(
            f"{indent}  today={_fmt(energy.today_kwh, 'kWh', 3)}"
            f"  month={_fmt(energy.this_month_kwh, 'kWh', 3)}"
            f"  total={_fmt(energy.total_kwh, 'kWh', 3)}"
        )
    elif status.is_on is not None:
        out(f"{indent}  energy: not reported by this device")
    for child in status.children:
        print_status(child, indent=indent + "    ")
    if status.features:
        out(f"{indent}  features:")
        for key, value in sorted(status.features.items()):
            out(f"{indent}    {key} = {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="powerctl",
        description=(
            "Discover, query and switch networked smart outlets. "
            "Turning a device off always needs --yes."
        ),
    )
    parser.add_argument("--version", action="version", version=f"powerctl {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print machine readable JSON")
    common.add_argument(
        "--backend",
        default=None,
        help=f"restrict to one backend ({', '.join(backend_names())}); default: all known",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", parents=[common], help="scan the network for devices")
    p.add_argument("--target", default=None, help="broadcast address (default: auto-detected)")
    p.add_argument("--timeout", type=int, default=5, help="discovery timeout in seconds")
    p.add_argument("--no-save", action="store_true", help="do not update the registry")
    p.add_argument(
        "--all-devices",
        action="store_true",
        help=(
            "also list devices that answer TP-Link discovery but have no switchable "
            "outlet, such as mesh nodes and cameras; hidden by default"
        ),
    )
    p.add_argument(
        "--sweep",
        nargs="?",
        const=True,
        default=False,
        metavar="CIDR",
        help=(
            "after the broadcast, probe every address of the local subnet "
            "(or of the given CIDR) so a device that ignores broadcast is still found"
        ),
    )

    p = sub.add_parser(
        "probe",
        parents=[common],
        help="identify devices by address when discovery does not reach them",
    )
    p.add_argument("host", nargs="+", help="IP address or host name to try")
    p.add_argument("--no-save", action="store_true", help="do not update the registry")

    p = sub.add_parser("list", parents=[common], help="list devices from the registry")
    p.add_argument(
        "--all-devices",
        action="store_true",
        help="also list devices that have no switchable outlet",
    )

    p = sub.add_parser("status", parents=[common], help="show state and power use")
    p.add_argument("device", nargs="*", help="alias, host, IP, MAC or device id")
    p.add_argument("--all", action="store_true", help="query every device in the registry")
    p.add_argument("--child", default=None, help="socket of a power strip (alias or index)")
    p.add_argument("--features", action="store_true", help="also dump all device features")

    p = sub.add_parser("on", parents=[common], help="switch a device on")
    p.add_argument("device")
    p.add_argument("--child", default=None, help="socket of a power strip (alias or index)")

    p = sub.add_parser("off", parents=[common], help="switch a device off (needs --yes)")
    p.add_argument("device")
    p.add_argument("--child", default=None, help="socket of a power strip (alias or index)")
    p.add_argument("--yes", action="store_true", help="confirm that power may be cut")
    p.add_argument(
        "--force-protected",
        action="store_true",
        help="also override the protected list (never overrides a critical device)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="run every safety check and report what would happen, changing nothing",
    )

    p = sub.add_parser(
        "cycle",
        parents=[common],
        help="power cycle a device: off, wait, on (needs --yes)",
    )
    p.add_argument("device")
    p.add_argument("--child", default=None, help="socket of a power strip (alias or index)")
    p.add_argument("--yes", action="store_true", help="confirm that power may be cut")
    p.add_argument(
        "--force-protected",
        action="store_true",
        help="override the protected list (never overrides a critical device)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="run every safety check and report what would happen, changing nothing",
    )
    p.add_argument(
        "--off-seconds",
        type=float,
        default=DEFAULT_OFF_SECONDS,
        help=f"seconds to stay off (default: {DEFAULT_OFF_SECONDS:.0f})",
    )
    p.add_argument("--wait-host", default=None, help="host to wait for after power returns")
    p.add_argument(
        "--wait-port",
        type=int,
        default=None,
        help="TCP port to probe on --wait-host (default: ICMP ping)",
    )
    p.add_argument(
        "--wait-timeout",
        type=float,
        default=120.0,
        help="seconds to wait for --wait-host (default: 120)",
    )
    p.add_argument(
        "--no-require-wait",
        action="store_true",
        help="report a wait timeout instead of exiting non-zero",
    )

    p = sub.add_parser("protect", parents=[common], help="protect a device from being switched off")
    p.add_argument("device")
    p.add_argument(
        "--critical",
        action="store_true",
        help=(
            "never switch this device off, whatever flags are passed; "
            "removing the protection means editing the protection file by hand"
        ),
    )

    p = sub.add_parser(
        "unprotect", parents=[common], help="remove a device from the protected list"
    )
    p.add_argument("device")

    p = sub.add_parser("protected", parents=[common], help="list protected devices")

    p = sub.add_parser("backends", parents=[common], help="list available backends")

    p = sub.add_parser("login", parents=[common], help="store credentials for a backend")
    p.add_argument("--username", default=None, help="account name; prompted for if omitted")

    p = sub.add_parser("logout", parents=[common], help="delete stored credentials for a backend")

    p = sub.add_parser("doctor", parents=[common], help="check configuration and file permissions")
    return parser


async def run(args: argparse.Namespace) -> int:
    session = Session()
    command = args.command

    if command == "backends":
        backends = all_backends()
        if args.json:
            emit_json([{"name": b.name, "description": b.description} for b in backends])
        else:
            for backend in backends:
                out(f"{backend.name}: {backend.description}")
        return EXIT_OK

    if command == "discover":
        names = [args.backend] if args.backend else backend_names()
        target = args.target or default_broadcast()
        if not args.json:
            out(f"Scanning {target} for {', '.join(names)} devices ({args.timeout}s) ...")
        if not args.json and args.sweep:
            out("Sweeping the local subnet as well; this takes longer.")
        records = await core.discover(
            session,
            backends=names,
            target=target,
            timeout=args.timeout,
            save=not args.no_save,
            sweep=args.sweep,
        )
        # This tool is about power. A device with no relay cannot be switched,
        # so it is not shown unless it is explicitly asked for.
        if not args.all_devices:
            records = [rec for rec in records if rec.supports_switching]
        if args.json:
            emit_json([rec.to_dict() for rec in records])
        else:
            print_records(
                records, session.registry.protected_keys(), session.registry.critical_keys()
            )
            seen = {rec.host for rec in records}
            missing = [
                rec
                for rec in session.registry.devices
                if rec.host not in seen
                and rec.supports_switching
                and (args.backend is None or rec.backend == args.backend)
            ]
            if missing:
                out("\nKnown but did not answer this scan:")
                for rec in missing:
                    label = rec.alias or rec.host
                    when = f", last seen {rec.last_seen}" if rec.last_seen else ""
                    out(f"  {label} ({rec.host}){when}")
            if not args.no_save:
                out(f"\nSaved {len(records)} device(s) to {registry_path()}")
            if any(rec.needs_credentials for rec in records):
                out(
                    "\nSome devices need TP-Link cloud credentials. "
                    "Run 'powerctl login --backend kasa'."
                )
        return EXIT_OK

    if command == "probe":
        names = [args.backend] if args.backend else backend_names()
        records = await core.probe(session, list(args.host), backends=names, save=not args.no_save)
        if args.json:
            emit_json([rec.to_dict() for rec in records])
        else:
            if records:
                print_records(
                    records,
                    session.registry.protected_keys(),
                    session.registry.critical_keys(),
                )
            missed = set(args.host) - {rec.host for rec in records}
            for host in sorted(missed):
                out(f"{host}: no supported device answered")
        return EXIT_OK if records else 1

    if command == "list":
        records = [
            rec
            for rec in session.registry.devices
            if (args.backend is None or rec.backend == args.backend)
            and (args.all_devices or rec.supports_switching)
        ]
        if args.json:
            emit_json([rec.to_dict() for rec in records])
        else:
            print_records(
                records, session.registry.protected_keys(), session.registry.critical_keys()
            )
        return EXIT_OK

    if command == "status":
        names = list(args.device)
        if args.all or not names:
            names = [
                rec.alias or rec.host
                for rec in session.registry.devices
                # A device with no relay has no state to report, and querying it
                # would only produce an error for every mesh node on the network.
                if rec.supports_switching and (args.backend is None or rec.backend == args.backend)
            ]
            if not names:
                raise UsageError("registry is empty; run 'powerctl discover' first")
        if len(names) == 1:
            result = await core.status(
                session,
                names[0],
                backend=args.backend,
                child=args.child,
                with_features=args.features,
            )
            if args.json:
                emit_json(result.to_dict())
            else:
                print_status(result)
            return EXIT_OK
        results = await core.status_many(session, names, backend=args.backend)
        payload = []
        failed = False
        for name, result in zip(names, results, strict=True):
            if isinstance(result, Exception):
                failed = True
                payload.append({"device": name, "error": str(result)})
                if not args.json:
                    out(f"{name}: error: {result}")
                continue
            payload.append(result.to_dict())
            if not args.json:
                print_status(result)
                out()
        if args.json:
            emit_json(payload)
        return 1 if failed else EXIT_OK

    if command in {"on", "off"}:
        turn_on = command == "on"
        if not turn_on and args.dry_run:
            return await dry_run(session, args, cycle=False)
        result = await core.switch(
            session,
            args.device,
            on=turn_on,
            backend=args.backend,
            child=args.child,
            confirmed=turn_on or args.yes,
            force_protected=getattr(args, "force_protected", False),
        )
        if args.json:
            emit_json(result.to_dict())
        else:
            print_status(result)
        return EXIT_OK

    if command == "cycle":
        if args.dry_run:
            return await dry_run(session, args, cycle=True)
        result = await core.cycle(
            session,
            args.device,
            backend=args.backend,
            child=args.child,
            off_seconds=args.off_seconds,
            confirmed=args.yes,
            force_protected=args.force_protected,
            wait_host=args.wait_host,
            wait_port=args.wait_port,
            wait_timeout=args.wait_timeout,
            require_wait=not args.no_require_wait,
        )
        if args.json:
            emit_json(result.to_dict())
        else:
            out(f"Power cycled {result.device} ({result.host}).")
            for event in result.events:
                fields = " ".join(f"{k}={v}" for k, v in event.items() if k not in {"step", "at"})
                out(f"  +{event['at']:>6.1f}s  {event['step']:<14} {fields}")
            out(f"Final state: {result.final_state}")
        return EXIT_OK

    if command == "protect":
        # A device can be protected before it is in the registry: a name that
        # does not resolve yet is stored verbatim, so protection is in place as
        # soon as the device appears under that alias or address.
        try:
            record = session.resolve(args.device, args.backend)
            entry = session.registry.protect(record=record, critical=args.critical)
        except DeviceNotFound:
            entry = session.registry.protect(name=args.device, critical=args.critical)
            out(f"'{args.device}' is not in the registry yet; protecting the name anyway.")
        session.registry.save()
        identifiers = ", ".join(sorted(entry.identifiers()))
        tier = (
            "critical, no flag overrides it"
            if entry.critical
            else "override with --force-protected"
        )
        out(
            f"Protected '{entry.name}' ({tier}): 'powerctl off' and 'powerctl cycle' "
            f"will refuse it when addressed by any of: {identifiers}."
        )
        return EXIT_OK

    if command == "unprotect":
        removed = session.registry.unprotect(args.device)
        session.registry.save()
        out(
            f"Removed '{args.device}' from the protected list."
            if removed
            else f"'{args.device}' was not on the protected list."
        )
        return EXIT_OK

    if command == "protected":
        entries = session.registry._unique_protections()
        if args.json:
            emit_json([entry.to_dict() for entry in entries])
        elif not entries:
            out("No protected devices.")
        else:
            for entry in entries:
                identifiers = ", ".join(value for value in (entry.host, entry.mac) if value)
                mark = "  [critical]" if entry.critical else ""
                out(f"{entry.name}{mark}" + (f"  ({identifiers})" if identifiers else ""))
        return EXIT_OK

    if command == "login":
        backend = args.backend or DEFAULT_BACKEND
        scope = core.credential_scope(backend)
        if scope != backend:
            out(
                f"'{backend}' authenticates against the shared '{scope}' account, "
                f"which every adapter using that account will pick up."
            )
        username = args.username or input(f"{scope} account (email): ").strip()
        if not username:
            raise UsageError("no account name given")
        password = getpass.getpass(f"{scope} password (not echoed): ")
        if not password:
            raise UsageError("no password given")
        path = store_credentials(scope, username, password)
        REDACTOR.add(password)
        out(f"Stored credentials for '{scope}' in {path} (mode 0600).")
        return EXIT_OK

    if command == "logout":
        scope = core.credential_scope(args.backend or DEFAULT_BACKEND)
        removed = forget_credentials(scope)
        out(
            f"Removed stored credentials for '{scope}'."
            if removed
            else f"No stored credentials for '{scope}'."
        )
        return EXIT_OK

    if command == "doctor":
        report = doctor_report(session)
        if args.json:
            emit_json(report)
        else:
            for line in report["lines"]:
                out(line)
        return EXIT_OK if report["ok"] else 1

    raise UsageError(f"unknown command '{command}'")


async def dry_run(session: Session, args: argparse.Namespace, *, cycle: bool) -> int:
    """Run the safety checks for a power cut and report, without switching.

    This exists so that the guards can be exercised against real hardware
    without ever cutting power, which is the only safe way to verify them.
    """
    record = session.resolve(args.device, args.backend)
    action = "cycle" if cycle else "off"
    try:
        record = await core.identify(session, record, child=args.child)
        core.check_switch_allowed(
            session,
            record,
            on=False,
            confirmed=args.yes,
            force_protected=args.force_protected,
        )
    except PowerctlError as exc:
        payload = {
            "device": record.alias or record.host,
            "host": record.host,
            "action": action,
            "would_run": False,
            "reason": str(exc),
        }
        if args.json:
            emit_json(payload)
        else:
            out(f"Dry run: '{action}' would be REFUSED for {payload['device']}.")
            out(f"  reason: {exc}")
        return exc.exit_code
    payload = {
        "device": record.alias or record.host,
        "host": record.host,
        "mac": record.mac,
        "action": action,
        "would_run": True,
        "protected": session.registry.is_protected(record),
        "critical": session.registry.is_critical(record),
    }
    if args.json:
        emit_json(payload)
    else:
        out(f"Dry run: '{action}' WOULD run against {payload['device']} ({record.host}).")
        out("  nothing was changed on the device")
    return EXIT_OK


def doctor_report(session: Session) -> dict[str, Any]:
    """Check the registry, the credential file and the credential environment."""
    lines: list[str] = []
    ok = True
    reg = registry_path()
    lines.append(f"registry: {reg} ({'present' if reg.exists() else 'not created yet'})")
    lines.append(f"devices in registry: {len(session.registry.devices)}")
    lines.append(f"protected devices: {len(set(session.registry.protected))}")

    cred = credentials_path()
    if cred.exists():
        mode = stat.S_IMODE(cred.stat().st_mode)
        wide = bool(mode & (stat.S_IRWXG | stat.S_IRWXO))
        lines.append(f"credential file: {cred} (mode {mode:04o})")
        if wide:
            ok = False
            lines.append(f"  PROBLEM: readable by group or other; run 'chmod 600 {cred}'")
    else:
        lines.append(f"credential file: {cred} (none)")

    for name in backend_names():
        scope = core.credential_scope(name)
        env_user = f"POWERCTL_{scope.upper()}_USERNAME"
        env_set = bool(os.environ.get(env_user))
        try:
            creds = load_credentials(scope)
        except PowerctlError as exc:
            ok = False
            lines.append(f"backend {name}: credential error: {exc}")
            continue
        if creds is None:
            lines.append(
                f"backend {name} (account '{scope}'): no credentials "
                f"(fine for older Kasa devices; Tapo needs 'powerctl login --backend {name}')"
            )
        else:
            lines.append(
                f"backend {name} (account '{scope}'): credentials for "
                f"{creds.username} from {creds.source}"
            )
        if env_set and creds and not creds.source.startswith("env"):
            lines.append(f"  note: {env_user} is set but was not used")
    lines.append(
        "Never pass a password on the command line; use 'powerctl login' or the environment."
    )
    return {"ok": ok, "lines": lines}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except PowerctlError as exc:
        print(REDACTOR(f"powerctl: {exc}"), file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("powerctl: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

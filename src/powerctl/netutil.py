"""Network helpers: broadcast address detection and host reachability waits."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import shutil
import socket
import subprocess
import time

DEFAULT_BROADCAST = "255.255.255.255"


def default_broadcast() -> str:
    """Return the broadcast address of the interface holding the default route.

    A machine with docker, libvirt or LXC bridges has several interfaces, and a
    discovery sent to 255.255.255.255 can leave through the wrong one. Falling
    back to the global broadcast address is still correct on single-homed hosts.
    """
    if not shutil.which("ip"):
        return DEFAULT_BROADCAST
    try:
        routes = json.loads(
            subprocess.run(
                ["ip", "-j", "route", "show", "default"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout
        )
        interface = next((route["dev"] for route in routes if route.get("dev")), None)
        if not interface:
            return DEFAULT_BROADCAST
        addresses = json.loads(
            subprocess.run(
                ["ip", "-j", "-4", "addr", "show", "dev", interface],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout
        )
        for entry in addresses:
            for info in entry.get("addr_info", []):
                if info.get("family") != "inet":
                    continue
                if info.get("broadcast"):
                    return info["broadcast"]
                network = ipaddress.ip_network(
                    f"{info['local']}/{info['prefixlen']}", strict=False
                )
                return str(network.broadcast_address)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return DEFAULT_BROADCAST
    return DEFAULT_BROADCAST


async def tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """True if a TCP connection to ``host:port`` completes within ``timeout``."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def icmp_up(host: str, timeout: float = 2.0) -> bool:
    """True if the host answers a single ping. Requires the system ping binary."""
    ping = shutil.which("ping")
    if not ping:
        return False
    process = await asyncio.create_subprocess_exec(
        ping,
        "-c",
        "1",
        "-W",
        str(max(1, int(timeout))),
        host,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return await asyncio.wait_for(process.wait(), timeout=timeout + 1) == 0
    except asyncio.TimeoutError:
        process.kill()
        return False


async def wait_for_host(
    host: str,
    *,
    port: int | None = None,
    timeout: float = 120.0,
    interval: float = 2.0,
    up: bool = True,
) -> float | None:
    """Wait until ``host`` is reachable (``up``) or unreachable (``up=False``).

    Uses a TCP connect when ``port`` is given, otherwise ICMP. Returns the
    elapsed seconds, or None if the timeout expired first.
    """
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        if port is not None:
            reachable = await tcp_open(host, port, timeout=min(interval, 2.0))
        else:
            reachable = await icmp_up(host, timeout=min(interval, 2.0))
        if reachable is up:
            return time.monotonic() - started
        await asyncio.sleep(interval)
    return None


def resolve_host(host: str) -> str | None:
    """Return the IP address for ``host``, or None if it does not resolve."""
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None

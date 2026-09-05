"""Network helpers. No test touches a real network: sockets are stubbed."""

from __future__ import annotations

import subprocess

import pytest

from powerctl import netutil


def test_subnet_hosts_lists_usable_addresses():
    hosts = netutil.subnet_hosts("192.0.2.0/30")
    assert hosts == ["192.0.2.1", "192.0.2.2"]


def test_subnet_hosts_refuses_a_network_that_is_too_large():
    with pytest.raises(ValueError, match="more than the limit"):
        netutil.subnet_hosts("198.51.0.0/16")


async def test_open_ports_returns_only_hosts_that_answer(monkeypatch):
    async def fake_tcp_open(host, port, timeout=1.0):
        return host == "192.0.2.5" and port == 9999

    monkeypatch.setattr(netutil, "tcp_open", fake_tcp_open)
    found = await netutil.open_ports(["192.0.2.4", "192.0.2.5"], (80, 9999))
    assert found == ["192.0.2.5"]


async def test_wait_for_host_returns_elapsed_once_reachable(monkeypatch):
    answers = iter([False, True])

    async def fake_tcp_open(host, port, timeout=1.0):
        return next(answers)

    monkeypatch.setattr(netutil, "tcp_open", fake_tcp_open)
    elapsed = await netutil.wait_for_host("192.0.2.9", port=80, timeout=5, interval=0.01)
    assert elapsed is not None and elapsed >= 0


async def test_wait_for_host_times_out(monkeypatch):
    async def never(host, port, timeout=1.0):
        return False

    monkeypatch.setattr(netutil, "tcp_open", never)
    assert await netutil.wait_for_host("192.0.2.9", port=80, timeout=0.05, interval=0.01) is None


async def test_wait_for_host_can_wait_for_a_host_to_go_away(monkeypatch):
    async def down(host, timeout=1.0):
        return False

    monkeypatch.setattr(netutil, "icmp_up", down)
    assert await netutil.wait_for_host("192.0.2.9", timeout=5, interval=0.01, up=False) is not None


async def test_tcp_open_reports_a_refused_connection(monkeypatch):
    async def refuse(host, port):
        raise OSError("connection refused")

    monkeypatch.setattr(netutil.asyncio, "open_connection", refuse)
    assert await netutil.tcp_open("192.0.2.9", 80, timeout=0.1) is False


async def test_icmp_up_without_a_ping_binary(monkeypatch):
    monkeypatch.setattr(netutil.shutil, "which", lambda name: None)
    assert await netutil.icmp_up("192.0.2.9") is False


def _fake_ip_command(routes: str, addresses: str):
    def run(cmd, **kwargs):
        payload = routes if "route" in cmd else addresses
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")

    return run


def test_default_broadcast_uses_the_default_route_interface(monkeypatch):
    monkeypatch.setattr(netutil.shutil, "which", lambda name: "/usr/bin/ip")
    monkeypatch.setattr(
        netutil.subprocess,
        "run",
        _fake_ip_command(
            '[{"dev": "eth0"}]',
            '[{"addr_info": [{"family": "inet", "local": "192.0.2.5",'
            ' "prefixlen": 24, "broadcast": "192.0.2.255"}]}]',
        ),
    )
    assert netutil.default_broadcast() == "192.0.2.255"


def test_default_broadcast_computes_one_when_not_reported(monkeypatch):
    monkeypatch.setattr(netutil.shutil, "which", lambda name: "/usr/bin/ip")
    monkeypatch.setattr(
        netutil.subprocess,
        "run",
        _fake_ip_command(
            '[{"dev": "eth0"}]',
            '[{"addr_info": [{"family": "inet", "local": "192.0.2.5", "prefixlen": 24}]}]',
        ),
    )
    assert netutil.default_broadcast() == "192.0.2.255"


def test_default_broadcast_falls_back_without_the_ip_command(monkeypatch):
    monkeypatch.setattr(netutil.shutil, "which", lambda name: None)
    assert netutil.default_broadcast() == netutil.DEFAULT_BROADCAST


def test_local_subnet_returns_the_cidr(monkeypatch):
    monkeypatch.setattr(netutil.shutil, "which", lambda name: "/usr/bin/ip")
    monkeypatch.setattr(
        netutil.subprocess,
        "run",
        _fake_ip_command(
            '[{"dev": "eth0"}]',
            '[{"addr_info": [{"family": "inet", "local": "192.0.2.5", "prefixlen": 24}]}]',
        ),
    )
    assert netutil.local_subnet() == "192.0.2.0/24"


def test_local_subnet_is_none_when_the_command_fails(monkeypatch):
    monkeypatch.setattr(netutil.shutil, "which", lambda name: "/usr/bin/ip")

    def boom(cmd, **kwargs):
        raise subprocess.SubprocessError("no route")

    monkeypatch.setattr(netutil.subprocess, "run", boom)
    assert netutil.local_subnet() is None


def test_resolve_host_returns_none_for_an_unresolvable_name(monkeypatch):
    def fail(name):
        raise OSError("no such host")

    monkeypatch.setattr(netutil.socket, "gethostbyname", fail)
    assert netutil.resolve_host("nope.invalid") is None


class StubProcess:
    def __init__(self, returncode=0, hangs=False):
        self.returncode = returncode
        self._hangs = hangs
        self.killed = False

    async def wait(self):
        if self._hangs:
            import asyncio

            await asyncio.sleep(10)
        return self.returncode

    def kill(self):
        self.killed = True


async def test_icmp_up_when_the_host_answers(monkeypatch):
    monkeypatch.setattr(netutil.shutil, "which", lambda name: "/bin/ping")

    async def fake_exec(*args, **kwargs):
        return StubProcess(returncode=0)

    monkeypatch.setattr(netutil.asyncio, "create_subprocess_exec", fake_exec)
    assert await netutil.icmp_up("192.0.2.9") is True


async def test_icmp_up_when_the_host_is_silent(monkeypatch):
    monkeypatch.setattr(netutil.shutil, "which", lambda name: "/bin/ping")

    async def fake_exec(*args, **kwargs):
        return StubProcess(returncode=1)

    monkeypatch.setattr(netutil.asyncio, "create_subprocess_exec", fake_exec)
    assert await netutil.icmp_up("192.0.2.9") is False


async def test_icmp_up_kills_a_hanging_ping(monkeypatch):
    monkeypatch.setattr(netutil.shutil, "which", lambda name: "/bin/ping")
    process = StubProcess(hangs=True)

    async def fake_exec(*args, **kwargs):
        return process

    monkeypatch.setattr(netutil.asyncio, "create_subprocess_exec", fake_exec)
    assert await netutil.icmp_up("192.0.2.9", timeout=0.05) is False
    assert process.killed is True


async def test_tcp_open_closes_the_connection_it_made(monkeypatch):
    class Writer:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

    writer = Writer()

    async def fake_open(host, port):
        return object(), writer

    monkeypatch.setattr(netutil.asyncio, "open_connection", fake_open)
    assert await netutil.tcp_open("192.0.2.9", 80) is True
    assert writer.closed is True


async def test_tcp_open_tolerates_a_failing_close(monkeypatch):
    class Writer:
        def close(self):
            return None

        async def wait_closed(self):
            raise OSError("already gone")

    async def fake_open(host, port):
        return object(), Writer()

    monkeypatch.setattr(netutil.asyncio, "open_connection", fake_open)
    assert await netutil.tcp_open("192.0.2.9", 80) is True


def test_default_broadcast_without_a_default_route(monkeypatch):
    monkeypatch.setattr(netutil.shutil, "which", lambda name: "/usr/bin/ip")
    monkeypatch.setattr(netutil.subprocess, "run", _fake_ip_command("[]", "[]"))
    assert netutil.default_broadcast() == netutil.DEFAULT_BROADCAST


def test_default_broadcast_when_the_interface_has_no_ipv4(monkeypatch):
    monkeypatch.setattr(netutil.shutil, "which", lambda name: "/usr/bin/ip")
    monkeypatch.setattr(
        netutil.subprocess,
        "run",
        _fake_ip_command('[{"dev": "eth0"}]', '[{"addr_info": [{"family": "inet6"}]}]'),
    )
    assert netutil.default_broadcast() == netutil.DEFAULT_BROADCAST


def test_local_subnet_without_the_ip_command(monkeypatch):
    monkeypatch.setattr(netutil.shutil, "which", lambda name: None)
    assert netutil.local_subnet() is None


def test_local_subnet_without_a_default_route(monkeypatch):
    monkeypatch.setattr(netutil.shutil, "which", lambda name: "/usr/bin/ip")
    monkeypatch.setattr(netutil.subprocess, "run", _fake_ip_command("[]", "[]"))
    assert netutil.local_subnet() is None


def test_resolve_host_returns_an_address(monkeypatch):
    monkeypatch.setattr(netutil.socket, "gethostbyname", lambda name: "192.0.2.9")
    assert netutil.resolve_host("plug.example") == "192.0.2.9"

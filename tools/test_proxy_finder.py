#!/usr/bin/env python3
"""Proxy finder — discovery, validation and the automatic-use mode.

Everything here is local and deterministic: the "public proxies" are tiny
proxy servers started on 127.0.0.1 (a canned HTTP proxy, a *transparent* one
that really forwards, a SOCKS5 one, plus a dead port), and the check URL is a
local server, so no external proxy or network is involved.

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/test_proxy_finder.py
    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/test_proxy_finder.py --live

``--live`` additionally runs a real search against the public proxy lists.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnidownloader.core.proxy_manager import (  # noqa: E402
    ProxyManager, ProxyType,
)
from omnidownloader.services.proxy_finder import (  # noqa: E402
    ProxyCandidate, ProxyFinder, _is_private_ip, extract_ip, parse_proxy_lines,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# ── tiny local "public proxies" ─────────────────────────────────

class RawServer:
    """A one-handler asyncio TCP server on a random local port."""

    def __init__(self, handler):
        self._handler = handler
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> "RawServer":
        self._server = await asyncio.start_server(
            self._on_client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def _on_client(self, reader, writer):
        try:
            await self._handler(reader, writer)
        except Exception:  # noqa: BLE001 - a test client that vanished
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass


async def _read_headers(reader) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await asyncio.wait_for(reader.read(4096), timeout=5)
        if not chunk:
            break
        data += chunk
    return data


def _ip_reply(ip: str) -> bytes:
    body = json.dumps({"ip": ip}).encode()
    return (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() +
            b"\r\nConnection: close\r\n\r\n" + body)


def canned_http_proxy(ip: str):
    """An HTTP proxy that answers every request with *ip* (a real remote hop)."""
    async def handler(reader, writer):
        await _read_headers(reader)
        writer.write(_ip_reply(ip))
        await writer.drain()
    return handler


def forward_http_proxy():
    """A *transparent* proxy: it forwards the request itself, so the exit IP is
    the caller's own.  This is the case that must be rejected."""
    async def handler(reader, writer):
        raw = await _read_headers(reader)
        if not raw:
            return
        request_line = raw.split(b"\r\n", 1)[0].decode("latin-1")
        parts = request_line.split()
        if len(parts) < 2 or not parts[1].startswith("http://"):
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return
        target = urlsplit(parts[1])
        port = target.port or 80
        upstream_reader, upstream_writer = await asyncio.open_connection(
            target.hostname, port)
        upstream_writer.write(
            f"GET {target.path or '/'}?{target.query} HTTP/1.1\r\n"
            f"Host: {target.hostname}:{port}\r\n"
            "User-Agent: transparent\r\nConnection: close\r\n\r\n".encode())
        await upstream_writer.drain()
        try:
            while True:
                chunk = await asyncio.wait_for(upstream_reader.read(4096), timeout=5)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        finally:
            upstream_writer.close()
    return handler


def canned_socks5_proxy(ip: str):
    """A SOCKS5 proxy that completes the handshake, then answers with *ip*."""
    async def handler(reader, writer):
        greeting = await asyncio.wait_for(reader.readexactly(2), timeout=5)
        if greeting[0] != 5:
            return
        await reader.readexactly(greeting[1])          # offered methods
        writer.write(b"\x05\x00")                      # no auth
        await writer.drain()
        request = await asyncio.wait_for(reader.readexactly(4), timeout=5)
        atyp = request[3]
        if atyp == 1:
            await reader.readexactly(4)
        elif atyp == 3:
            length = (await reader.readexactly(1))[0]
            await reader.readexactly(length)
        elif atyp == 4:
            await reader.readexactly(16)
        await reader.readexactly(2)                    # port
        writer.write(b"\x05\x00\x00\x01" + b"\x00\x00\x00\x00" + b"\x00\x00")
        await writer.drain()
        await _read_headers(reader)                    # the tunnelled request
        writer.write(_ip_reply(ip))
        await writer.drain()
    return handler


class DirectServer:
    """Stands in for ipify: replies with our own (fake) public IP."""

    def __init__(self, ip: str = "10.0.0.1"):
        self.ip = ip
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> "DirectServer":
        async def handler(reader, writer):
            await _read_headers(reader)
            writer.write(_ip_reply(self.ip))
            await writer.drain()
            writer.close()
        self._server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/ip"

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


def closed_port() -> int:
    """A port nothing is listening on (bound then released)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ── parsing ─────────────────────────────────────────────────────

def test_parsing() -> None:
    text = """\
# a comment
8.8.8.8:8080
socks5://1.2.3.4:1080
http://user:secret@5.6.7.8:3128
9.9.9.9:0
not a proxy
1.2.3.4:70000
// another comment
8.8.8.8:8080
11.11.11.11:8888
"""
    cands = parse_proxy_lines(text, ProxyType.HTTP, source="test")
    keys = [c.key for c in cands]
    check("bare host:port lines are parsed", ("http", "8.8.8.8", 8080) in keys, str(keys))
    check("a scheme overrides the list's default type",
          ("socks5", "1.2.3.4", 1080) in keys)
    check("credentials are kept",
          any(c.host == "5.6.7.8" and c.username == "user"
              and c.password == "secret" for c in cands))
    check("out-of-range ports are dropped",
          not any(c.port in (0, 70000) for c in cands), str([c.port for c in cands]))
    check("junk and comment lines are dropped", len(cands) == 4,
          f"{len(cands)} candidates: {keys}")
    check("duplicates are collapsed", keys.count(("http", "8.8.8.8", 8080)) == 1)
    check("the candidate URL is usable by aiohttp",
          cands[0].url == "http://8.8.8.8:8080", cands[0].url)
    check("a SOCKS candidate uses the socks5 scheme",
          any(c.url.startswith("socks5://") for c in cands))
    check("ipify JSON is understood", extract_ip('{"ip": "203.0.113.9"}') == "203.0.113.9")
    check("plain-text IP replies are understood", extract_ip("203.0.113.9\n") == "203.0.113.9")
    check("garbage yields no IP", extract_ip("<!doctype html>nope") == "")
    check("RFC1918 addresses are recognised",
          _is_private_ip("192.168.1.5") and _is_private_ip("10.0.0.1")
          and not _is_private_ip("8.8.8.8"))


# ── validation ──────────────────────────────────────────────────

async def test_validation() -> None:
    direct = await DirectServer("10.0.0.1").start()
    good = await RawServer(canned_http_proxy("203.0.113.9")).start()
    transparent = await RawServer(forward_http_proxy()).start()
    socks = await RawServer(canned_socks5_proxy("198.51.100.7")).start()

    finder = ProxyFinder(check_url=direct.url, timeout=6, concurrency=4)
    ours = await finder.real_ip()
    check("our own IP is read through the direct connection", ours == "10.0.0.1", ours)

    cand = ProxyCandidate("127.0.0.1", good.port, ProxyType.HTTP)
    ok = await finder.validate(cand)
    check("a working HTTP proxy is accepted", ok, cand.error)
    check("its exit IP is recorded", cand.exit_ip == "203.0.113.9", cand.exit_ip)
    check("its latency is measured", cand.latency_ms > 0,
          f"{cand.latency_ms:.1f} ms")

    dead = ProxyCandidate("127.0.0.1", closed_port(), ProxyType.HTTP)
    ok = await finder.validate(dead)
    check("a dead proxy is rejected", not ok)
    check("the failure is explained", bool(dead.error), dead.error)

    transp = ProxyCandidate("127.0.0.1", transparent.port, ProxyType.HTTP)
    ok = await finder.validate(transp)
    check("a transparent proxy (our own IP as exit) is rejected", not ok, transp.error)
    check("the reason names the problem", "transparent" in transp.error.lower(),
          transp.error)

    socks_cand = ProxyCandidate("127.0.0.1", socks.port, ProxyType.SOCKS5)
    ok = await finder.validate(socks_cand)
    check("a SOCKS5 proxy is validated over the socks connector", ok, socks_cand.error)
    check("its exit IP is recorded", socks_cand.exit_ip == "198.51.100.7",
          socks_cand.exit_ip)

    for server in (direct, good, transparent, socks):
        await server.stop()


# ── selection ───────────────────────────────────────────────────

class StubFinder(ProxyFinder):
    """A finder whose candidate list is fixed."""

    def __init__(self, candidates, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._candidates = candidates

    async def fetch_candidates(self, limit: int = 400):
        return list(self._candidates)

    async def _decorate(self, cand):  # no geo lookups against the network
        cand.country = "ZZ"


async def test_find_best() -> None:
    direct = await DirectServer("10.0.0.1").start()
    good = await RawServer(canned_http_proxy("203.0.113.9")).start()
    slow = await RawServer(canned_http_proxy("203.0.113.10")).start()
    transparent = await RawServer(forward_http_proxy()).start()

    finder = StubFinder(
        [ProxyCandidate("127.0.0.1", closed_port(), ProxyType.HTTP),
         ProxyCandidate("127.0.0.1", transparent.port, ProxyType.HTTP),
         ProxyCandidate("127.0.0.1", slow.port, ProxyType.HTTP),
         ProxyCandidate("127.0.0.1", good.port, ProxyType.HTTP)],
        check_url=direct.url, timeout=6, concurrency=4,
    )
    # Make the "slow" one measurably slower by holding its answer back.
    best = await finder.find_best(deadline=10)
    check("find_best returns a working proxy", best is not None,
          best.label if best else "None")
    check("it is one of the two that answer",
          bool(best and best.exit_ip in ("203.0.113.9", "203.0.113.10")),
          best.exit_ip if best else "")
    check("the transparent one is never chosen",
          bool(best) and best.port != transparent.port)
    check("the result carries an exit IP and a latency",
          bool(best and best.exit_ip and best.latency_ms > 0))

    empty = StubFinder([], check_url=direct.url)
    check("no candidates means no proxy (not an exception)",
          await empty.find_best() is None)

    all_dead = StubFinder(
        [ProxyCandidate("127.0.0.1", closed_port(), ProxyType.HTTP)],
        check_url=direct.url, timeout=3)
    check("an all-dead list yields None", await all_dead.find_best(deadline=8) is None)

    for server in (direct, good, slow, transparent):
        await server.stop()


# ── automatic mode in the manager ───────────────────────────────

async def test_auto_mode() -> None:
    import omnidownloader.main as main_mod

    loop = asyncio.get_running_loop()
    main_mod._loop = loop          # what schedule_async needs (engine loop)
    direct = await DirectServer("10.0.0.1").start()
    good = await RawServer(canned_http_proxy("203.0.113.9")).start()

    mgr = ProxyManager()
    found: list[tuple] = []
    status: list[str] = []
    mgr.auto_proxy_found.connect(lambda *a: found.append(a))
    mgr.auto_proxy_status.connect(status.append)
    cand = ProxyCandidate("127.0.0.1", good.port, ProxyType.HTTP)
    mgr.set_finder(StubFinder([cand], check_url=direct.url, timeout=6))

    got = await mgr.discover_proxy()
    check("discover_proxy adopts a working proxy", got is not None)
    check("the manager routes through it",
          mgr.enabled and mgr.config.host == "127.0.0.1"
          and mgr.config.port == good.port
          and mgr.config.proxy_type == ProxyType.HTTP,
          f"{mgr.config.to_url()}")
    check("get_proxy_url() is what the modules receive",
          mgr.get_proxy_url().startswith("http://127.0.0.1:"), mgr.get_proxy_url())
    check("the result is announced to the UI", len(found) == 1 and found[0][3] == "203.0.113.9",
          str(found))
    check("progress lines are emitted for the page",
          any("Using" in s for s in status), " | ".join(status))
    check("DNS leak protection is on for a discovered proxy",
          mgr.config.dns_leak_protection)
    check("the state can be persisted",
          mgr.auto_discovery_state()["proxy"]["host"] == "127.0.0.1"
          and mgr.to_dict()["auto_discover"] is False)

    # Tor owns the tunnel: auto discovery must not fight it.
    mgr.set_tor_enabled(True)
    check("with Tor on, discovery refuses to configure a proxy",
          await mgr.discover_proxy() is None)
    check("the refusal is explained",
          any("Tor" in s for s in status), status[-1])
    mgr.set_tor_enabled(False)

    mgr.set_auto_discovery(True)
    check("enabling auto discovery flags it for persistence",
          mgr.to_dict()["auto_discover"] is True)
    await asyncio.sleep(0.2)   # let the scheduled loop run its first search
    check("the background loop searches and adopts by itself",
          mgr.auto_candidate is not None, str(mgr.auto_candidate))
    mgr.set_auto_discovery(False)
    check("disabling it forgets the candidate",
          mgr.to_dict()["auto_discover"] is False and mgr.auto_candidate is None)

    # A restored setting must actually start the loop, not just set a flag.
    restored = ProxyManager()
    restored.from_dict({"auto_discover": True, "enabled": False})
    restored.set_finder(StubFinder([ProxyCandidate("127.0.0.1", good.port,
                                                   ProxyType.HTTP)],
                                   check_url=direct.url, timeout=6))
    restored.set_auto_discovery(True)
    await asyncio.sleep(0.2)
    check("a restored auto-discover config starts searching again",
          restored.auto_candidate is not None)
    restored.set_auto_discovery(False)

    for server in (direct, good):
        await server.stop()
    main_mod._loop = None


async def test_live() -> None:
    """A real search against the public lists (network, run with --live)."""
    direct_finder = ProxyFinder(timeout=8)
    ours = await direct_finder.real_ip()
    check("our real public IP is visible", bool(ours), ours or "(failed)")

    finder = ProxyFinder(timeout=6, concurrency=60)
    candidates = await finder.fetch_candidates()
    check("public lists yield candidates", len(candidates) > 20, f"{len(candidates)}")
    check("both http and socks entries are understood",
          len({c.proxy_type.value for c in candidates}) >= 2,
          str(sorted({c.proxy_type.value for c in candidates})))

    best = await finder.find_best(max_candidates=120, deadline=45)
    if best is None:
        check("a working proxy was found", False,
              "none of the sampled public proxies answered")
    else:
        check("a working proxy was found", True,
              f"{best.label} {best.latency_ms:.0f} ms exit {best.exit_ip} "
              f"{best.country}")
        check("its exit IP is not ours", best.exit_ip != ours, best.exit_ip)


async def main() -> int:
    from PyQt6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    assert app is not None

    test_parsing()
    await test_validation()
    await test_find_best()
    await test_auto_mode()
    if "--live" in sys.argv:
        await test_live()

    failures = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

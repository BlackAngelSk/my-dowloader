"""Tor Manager — embedded Tor daemon lifecycle management."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class TorManager:
    """Manages the local Tor daemon for anonymous connections."""

    def __init__(self, socks_port=9050, control_port=9051, data_dir=""):
        self._socks_port = socks_port
        self._control_port = control_port
        self._data_dir = data_dir or str(Path.home() / ".omnidownloader" / "tor")
        self._process: Optional[asyncio.subprocess.Process] = None
        self._torrc_path = os.path.join(self._data_dir, "torrc")
        self._control_password = "omnidownloader_tor_ctrl"
        self._is_bootstrapped = False

    @property
    def is_running(self):
        return self._process is not None and self._process.returncode is None

    @property
    def is_bootstrapped(self):
        return self._is_bootstrapped

    async def start(self):
        if self.is_running:
            return True
        tor_bin = self.find_tor_binary()
        if not tor_bin:
            logger.error("Tor binary not found")
            return False
        Path(self._data_dir).mkdir(parents=True, exist_ok=True)
        hashed_pw = await self._generate_hashed_password(tor_bin)
        self._write_torrc(hashed_pw)
        self._process = await asyncio.create_subprocess_exec(
            tor_bin, "-f", self._torrc_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=self._data_dir)
        self._is_bootstrapped = await self._wait_for_bootstrap(60)
        return self._is_bootstrapped

    async def stop(self):
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=10)
            except asyncio.TimeoutError:
                self._process.kill()
            self._process = None
            self._is_bootstrapped = False

    async def rotate_identity(self):
        if not self.is_running:
            return False
        try:
            from stem import Signal
            from stem.control import Controller
            with Controller.from_port(port=self._control_port) as ctrl:  # type: ignore[arg-type]
                ctrl.authenticate(password=self._control_password)
                ctrl.signal(Signal.NEWNYM)  # type: ignore[attr-defined]
                await asyncio.sleep(2)
                return True
        except Exception as exc:
            logger.error("Failed to rotate Tor identity: %s", exc)
            return False

    async def _generate_hashed_password(self, tor_bin):
        proc = await asyncio.create_subprocess_exec(
            tor_bin, "--hash-password", self._control_password,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        lines = stdout.decode().strip().split("\n")
        return lines[-1] if lines else ""

    def _write_torrc(self, hashed_password):
        content = (
            f"SocksPort {self._socks_port}\n"
            f"ControlPort {self._control_port}\n"
            f"HashedControlPassword {hashed_password}\n"
            f"DataDirectory {self._data_dir}\n"
            f"Log notice file {self._data_dir}/tor.log\n"
            f"RunAsDaemon 1\n"
        )
        with open(self._torrc_path, "w") as f:
            f.write(content)

    async def _wait_for_bootstrap(self, timeout):
        deadline = asyncio.get_event_loop().time() + timeout
        log_path = os.path.join(self._data_dir, "tor.log")
        while asyncio.get_event_loop().time() < deadline:
            if not self.is_running:
                return False
            if os.path.exists(log_path):
                with open(log_path) as f:
                    if "Bootstrapped 100%" in f.read():
                        return True
            await asyncio.sleep(1)
        return False

    async def is_circuit_established(self):
        if not self.is_running:
            return False
        try:
            import aiohttp
            from aiohttp_socks import ProxyConnector
            connector = ProxyConnector.from_url(self.socks_proxy_url)
            async with aiohttp.ClientSession(connector=connector,
                                             timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.get("https://check.torproject.org/api/ip") as resp:
                    data = await resp.json()
                    return data.get("IsTor", False)
        except Exception:
            return False

    @property
    def socks_proxy_url(self):
        return f"socks5://127.0.0.1:{self._socks_port}"

    def find_tor_binary(self):
        tor_path = shutil.which("tor")
        if tor_path:
            return tor_path
        for p in ["/usr/bin/tor", "/usr/local/bin/tor", "/opt/homebrew/bin/tor"]:
            if os.path.isfile(p):
                return p
        return None

"""Tor Manager — embedded Tor daemon lifecycle management."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from omnidownloader.core import platform_utils

logger = logging.getLogger(__name__)


class TorManager:
    """Manages the local Tor daemon for anonymous connections."""

    def __init__(self, socks_port=9050, control_port=9051, data_dir=""):
        self._socks_port = socks_port
        self._control_port = control_port
        self._data_dir = data_dir or str(platform_utils.tor_dir())
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
            # tor is chatty; an undrained pipe would fill and block it, and
            # everything we care about already goes to tor.log.
            **platform_utils.subprocess_kwargs(),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            cwd=self._data_dir)
        self._is_bootstrapped = await self._wait_for_bootstrap(60)
        return self._is_bootstrapped

    async def stop(self):
        if self._process and self._process.returncode is None:
            # Kill the tree, not just the parent: tor spawns children on
            # Windows and a bare terminate() used to leave them behind.
            platform_utils.kill_process_tree(self._process)
            try:
                await asyncio.wait_for(self._process.wait(), timeout=10)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
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
            **platform_utils.subprocess_kwargs(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        lines = stdout.decode().strip().split("\n")
        return lines[-1] if lines else ""

    def _write_torrc(self, hashed_password):
        # No RunAsDaemon: tor forked and the parent exited immediately, so
        # self._process.returncode became non-None, is_running was always
        # False, start() reported failure and stop() could never kill the
        # real daemon it had left behind.
        #
        # Paths are quoted: on Windows (and any home dir with a space, e.g.
        # "C:\Users\John Doe") an unquoted DataDirectory makes tor fail with
        # "Could not open configuration file" before it ever bootstraps.
        data_dir = str(self._data_dir).replace("\\", "/")
        content = (
            f"SocksPort {self._socks_port}\n"
            f"ControlPort {self._control_port}\n"
            f"HashedControlPassword {hashed_password}\n"
            f'DataDirectory "{data_dir}"\n'
            f'Log notice file "{data_dir}/tor.log"\n'
        )
        with open(self._torrc_path, "w", encoding="utf-8") as f:
            f.write(content)

    async def _wait_for_bootstrap(self, timeout):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        log_path = os.path.join(self._data_dir, "tor.log")
        while loop.time() < deadline:
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
        """Locate tor for this platform (PATH, then the usual install spots)."""
        return platform_utils.find_tor_binary() or None

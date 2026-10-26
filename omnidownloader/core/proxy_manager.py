"""Proxy Manager — central proxy configuration, validation, and kill switch.

Handles HTTP/HTTPS/SOCKS4/SOCKS5 proxy routing, Tor integration,
DNS leak protection, and real-time health monitoring with kill switch.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)


class ProxyType(str, Enum):
    HTTP = "http"
    HTTPS = "https"
    SOCKS4 = "socks4"
    SOCKS5 = "socks5"
    NONE = "none"


@dataclass
class ProxyConfig:
    """Configuration for a single proxy endpoint."""
    proxy_type: ProxyType = ProxyType.NONE
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    enabled: bool = False
    dns_leak_protection: bool = True

    @property
    def is_configured(self) -> bool:
        return bool(self.enabled and self.host and self.port > 0)

    def to_url(self) -> str:
        """Format as proxy URL: socks5://user:pass@host:port"""
        if not self.is_configured:
            return ""
        scheme = self.proxy_type.value
        auth = ""
        if self.username:
            auth = f"{self.username}:{self.password}@" if self.password else f"{self.username}@"
        return f"{scheme}://{auth}{self.host}:{self.port}"


@dataclass
class ProxyCheckResult:
    """Result of a proxy verification check."""
    success: bool = False
    ip: str = ""
    country: str = ""
    isp: str = ""
    latency_ms: float = 0.0
    error: str = ""
    checked_at: float = field(default_factory=time.time)

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.checked_at) > 120


class ProxyManager(QObject):
    """Central proxy orchestrator with kill switch and health monitoring."""

    proxy_status_changed = pyqtSignal(str, bool)    # proxy_url, is_alive
    kill_switch_triggered = pyqtSignal(str)          # reason
    kill_switch_cleared = pyqtSignal()
    ip_changed = pyqtSignal(str, str, str)           # ip, country, isp
    #: Automatic proxy discovery (find one and use it without the user typing
    #: anything).  ``url, label, latency_ms, exit_ip``.
    auto_proxy_found = pyqtSignal(str, str, float, str)
    #: Human-readable progress line for the anonymity page.
    auto_proxy_status = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._config = ProxyConfig()
        self._tor_enabled = False
        self._kill_switch_enabled = False
        self._kill_switch_interval = 15.0
        self._health_task: Optional[asyncio.Task] = None
        self._last_check = ProxyCheckResult()
        self._paused_by_kill_switch: bool = False
        self._tor_manager: Any = None  # set by main.py
        self._auto_discover = False
        self._auto_task: Optional[asyncio.Task] = None
        self._finder: Any = None  # injectable for tests
        self._auto_candidate: Any = None
        self._auto_recheck_interval = 180.0

    @property
    def auto_discovery_enabled(self) -> bool:
        return self._auto_discover

    @property
    def auto_candidate(self):
        """The proxy the automatic search settled on (or ``None``)."""
        return self._auto_candidate

    @property
    def config(self) -> ProxyConfig:
        return self._config

    @property
    def enabled(self) -> bool:
        return self._config.is_configured or self._tor_enabled

    @property
    def kill_switch_active(self) -> bool:
        return self._kill_switch_enabled

    @property
    def last_check(self) -> ProxyCheckResult:
        return self._last_check

    def configure(self, config: ProxyConfig) -> None:
        self._config = config
        logger.info("Proxy configured: %s", config.to_url() if config.is_configured else "disabled")

    def set_tor_enabled(self, enabled: bool) -> None:
        self._tor_enabled = enabled
        if enabled:
            self._config.proxy_type = ProxyType.SOCKS5
            self._config.host = "127.0.0.1"
            self._config.port = self._tor_socks_port()
            self._config.enabled = True
            self._config.dns_leak_protection = True
        elif (self._config.host == "127.0.0.1"
              and self._config.port == self._tor_socks_port()):
            # Disabling Tor used to leave the proxy configured, so every module
            # kept routing through a stopped Tor instance.
            self._config.enabled = False
        logger.info("Tor %s", "enabled" if enabled else "disabled")

    def set_kill_switch_interval(self, seconds: int) -> None:
        """Set the proxy health-check interval in seconds."""
        try:
            interval = max(5, int(seconds))
        except (TypeError, ValueError):
            return
        self._kill_switch_interval = float(interval)
        logger.info("Kill-switch check interval: %ss", interval)

    def _tor_socks_port(self) -> int:
        """Use the port the Tor manager was actually configured with."""
        tor = getattr(self, "_tor_manager", None)
        return int(getattr(tor, "_socks_port", 9050) or 9050)

    def set_kill_switch(self, enabled: bool) -> None:
        """Enable/disable the kill switch (called from the GUI thread)."""
        self._kill_switch_enabled = enabled
        if enabled and self._health_task is None:
            # Must not build a future here: this slot runs on the Qt thread,
            # which has no running event loop — it raised RuntimeError, so the
            # monitor never started and nothing was ever paused.
            import omnidownloader.main as main_mod
            main_mod.schedule_async(self._start_health_monitor())
        elif not enabled and self._health_task:
            self._health_task.cancel()
            self._health_task = None

    def get_proxy_url(self) -> str:
        """Return the active proxy URL string for subprocess args."""
        if self._tor_enabled:
            return f"socks5://127.0.0.1:{self._tor_socks_port()}"
        return self._config.to_url()

    # ── Automatic proxy discovery ───────────────────────────────
    #
    # Free proxies are found, proven to work (a request is actually pushed
    # through them) and then kept alive by re-checking; when one dies, the
    # next search replaces it.  Everything runs on the engine loop, and the
    # results leave the manager as signals so the GUI thread never waits.

    def set_auto_discovery(self, enabled: bool) -> None:
        """Turn automatic proxy discovery on/off (GUI thread safe).

        Re-enabling is honoured even when the flag was already restored from
        the config file at startup: what matters is whether the search loop is
        actually running, not the flag alone.
        """
        if enabled:
            if self._auto_discover and self._auto_task is not None \
                    and not self._auto_task.done():
                return
            self._auto_discover = True
            import omnidownloader.main as main_mod
            main_mod.schedule_async(self._auto_discovery_loop())
        else:
            if not self._auto_discover and self._auto_task is None:
                return
            self._auto_discover = False
            task, self._auto_task = self._auto_task, None
            if task is not None and not task.done():
                task.cancel()
            self._auto_candidate = None
            self.auto_proxy_status.emit("Automatic proxy discovery off")
            logger.info("Automatic proxy discovery disabled")

    def set_finder(self, finder) -> None:
        """Inject a :class:`ProxyFinder` (tests use a stub)."""
        self._finder = finder

    def _get_finder(self):
        if self._finder is None:
            from omnidownloader.services.proxy_finder import ProxyFinder
            self._finder = ProxyFinder()
        return self._finder

    async def discover_proxy(self):
        """Run one search and adopt the result.  Returns the candidate or None.

        Used both by the automatic loop and by the page's "Find now" button.
        """
        if self._tor_enabled:
            # Tor already owns the tunnel; replacing its config would break it.
            self.auto_proxy_status.emit("Tor is on — automatic proxy not used")
            return None
        finder = self._get_finder()
        self.auto_proxy_status.emit("Searching for a working proxy…")
        candidate = await finder.find_best()
        if candidate is None:
            self.auto_proxy_status.emit(
                "No working proxy found — every candidate was dead or transparent"
            )
            return None
        self.adopt_proxy(candidate)
        return candidate

    def adopt_proxy(self, candidate) -> None:
        """Route all traffic through *candidate* and announce it."""
        self._config = ProxyConfig(
            proxy_type=candidate.proxy_type,
            host=candidate.host,
            port=candidate.port,
            username=getattr(candidate, "username", "") or "",
            password=getattr(candidate, "password", "") or "",
            enabled=True,
            dns_leak_protection=True,
        )
        self._auto_candidate = candidate
        country = getattr(candidate, "country", "") or ""
        label = getattr(candidate, "label", "") or candidate.url
        exit_ip = getattr(candidate, "exit_ip", "") or ""
        latency = float(getattr(candidate, "latency_ms", 0.0) or 0.0)
        where = f"{exit_ip} {country}".strip()
        logger.info("Automatic proxy in use: %s (%.0f ms, exit %s)",
                    label, latency, where or "?")
        self.auto_proxy_found.emit(label, label, latency, exit_ip)
        self.auto_proxy_status.emit(
            f"Using {label} — {latency:.0f} ms, exit {where or 'unknown'}"
        )
        self.proxy_status_changed.emit(self.get_proxy_url(), True)

    async def _auto_discovery_loop(self) -> None:
        """Find a proxy, keep it alive, replace it when it stops answering."""
        self._auto_task = asyncio.current_task()
        while self._auto_discover:
            candidate = await self.discover_proxy()
            if not self._auto_discover:
                break
            if candidate is None:
                await asyncio.sleep(60.0)
                continue
            # Re-check the proxy while it is in use: free ones die within
            # minutes, and silently downloading through a dead proxy is worse
            # than finding a new one.
            while self._auto_discover:
                await asyncio.sleep(self._auto_recheck_interval)
                if not self._auto_discover:
                    return
                result = await self.verify_connection()
                if result.success:
                    continue
                logger.info("Automatic proxy died (%s) — searching again",
                            result.error)
                self.auto_proxy_status.emit(
                    f"Proxy stopped answering ({result.error}) — searching again…"
                )
                break
        logger.info("Automatic proxy loop stopped")

    def auto_discovery_state(self) -> dict:
        """Snapshot for persistence/UI: enabled + what was found."""
        cand = self._auto_candidate
        return {
            "enabled": self._auto_discover,
            "proxy": cand.to_dict() if cand is not None else None,
        }

    def get_aiohttp_connector(self):
        """Create an aiohttp connector routed through the proxy."""
        if not self.enabled:
            return None
        try:
            from aiohttp_socks import ProxyConnector
            url = self.get_proxy_url()
            if not url:
                return None
            # from_url derives the scheme (socks4/socks5/http) from the URL;
            # the manually computed type here was dead code.
            return ProxyConnector.from_url(url)
        except ImportError:
            logger.warning("aiohttp-socks not installed; proxy routing disabled for aiohttp")
            return None

    def get_ytdlp_args(self) -> list[str]:
        """Return yt-dlp CLI arguments for proxy routing."""
        url = self.get_proxy_url()
        if url:
            return ["--proxy", url]
        return []

    def get_libtorrent_proxy_settings(self) -> dict[str, Any]:
        """Return libtorrent session settings for proxy routing."""
        if not self.enabled:
            return {}
        try:
            import libtorrent as lt
        except ImportError:
            logger.warning("libtorrent not installed — proxy settings unavailable")
            return {}
        proxy_type = (lt.proxy_type_t.socks5
                      if self._config.proxy_type == ProxyType.SOCKS5
                      else lt.proxy_type_t.http)
        return {
            "force_proxy": True,
            "anonymous_mode": True,
            "proxy_hostname": self._config.host or "127.0.0.1",
            "proxy_type": proxy_type,
            "proxy_port": self._config.port or 9050,
            "proxy_username": self._config.username or "",
            "proxy_password": self._config.password or "",
            "proxy_misc": 0,
        }

    async def verify_connection(self) -> ProxyCheckResult:
        """Test the proxy by querying an IP endpoint through it."""
        import aiohttp
        start = time.monotonic()
        try:
            connector = self.get_aiohttp_connector()
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get("https://api.ipify.org?format=json") as resp:
                    if resp.status != 200:
                        return ProxyCheckResult(success=False, error=f"HTTP {resp.status}")
                    data = await resp.json()
                    ip = data.get("ip", "")
                    latency = (time.monotonic() - start) * 1000
                    # Try to get geo info
                    country = ""
                    isp = ""
                    try:
                        async with session.get(f"https://ipinfo.io/{ip}/json") as r2:
                            if r2.status == 200:
                                info = await r2.json()
                                country = info.get("country", "")
                                isp = info.get("org", "")
                    except Exception:
                        pass
                    result = ProxyCheckResult(
                        success=True, ip=ip, country=country, isp=isp,
                        latency_ms=latency, checked_at=time.time(),
                    )
                    self._last_check = result
                    self.ip_changed.emit(ip, country, isp)
                    self.proxy_status_changed.emit(self.get_proxy_url(), True)
                    return result
        except Exception as exc:
            result = ProxyCheckResult(success=False, error=str(exc), checked_at=time.time())
            self._last_check = result
            self.proxy_status_changed.emit(self.get_proxy_url(), False)
            return result

    async def check_kill_switch(self) -> bool:
        """Returns True if safe (proxy alive), False if kill switch should fire."""
        if not self._kill_switch_enabled:
            return True
        result = await self.verify_connection()
        if not result.success:
            reason = f"Kill switch: proxy unreachable ({result.error})"
            logger.critical(reason)
            self.kill_switch_triggered.emit(reason)
            return False
        return True

    async def _start_health_monitor(self) -> None:
        """Background loop that periodically checks proxy health."""
        while self._kill_switch_enabled:
            await asyncio.sleep(self._kill_switch_interval)
            if not self._kill_switch_enabled:
                break
            safe = await self.check_kill_switch()
            if not safe:
                self._paused_by_kill_switch = True
                # The DownloadManager should connect to kill_switch_triggered
                # and call pause_all_active_jobs()

    def start_health_monitor(self) -> None:
        if self._health_task is None or self._health_task.done():
            self._health_task = asyncio.ensure_future(self._start_health_monitor())

    def stop_health_monitor(self) -> None:
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
            self._health_task = None

    def was_paused_by_kill_switch(self) -> bool:
        return self._paused_by_kill_switch

    def clear_kill_switch_pause(self) -> None:
        self._paused_by_kill_switch = False
        self.kill_switch_cleared.emit()

    def to_dict(self) -> dict:
        return {
            "enabled": self._config.enabled,
            "proxy_type": self._config.proxy_type.value,
            "host": self._config.host,
            "port": self._config.port,
            "username": self._config.username,
            "password": self._config.password,
            "dns_leak_protection": self._config.dns_leak_protection,
            "tor_enabled": self._tor_enabled,
            "kill_switch_enabled": self._kill_switch_enabled,
            "kill_switch_interval": self._kill_switch_interval,
            # Remembered so an automatic search is not silently forgotten on
            # restart (the endpoint itself is re-discovered, not restored).
            "auto_discover": self._auto_discover,
        }

    def from_dict(self, data: dict) -> None:
        self._config = ProxyConfig(
            enabled=data.get("enabled", False),
            proxy_type=ProxyType(data.get("proxy_type", "none")),
            host=data.get("host", ""),
            port=data.get("port", 0),
            username=data.get("username", ""),
            password=data.get("password", ""),
            dns_leak_protection=data.get("dns_leak_protection", True),
        )
        self._tor_enabled = data.get("tor_enabled", False)
        self._kill_switch_enabled = data.get("kill_switch_enabled", False)
        self._kill_switch_interval = data.get("kill_switch_interval", 15.0)
        # Flag only: turning it on here would touch the asyncio loop from
        # whatever thread restored the settings.
        self._auto_discover = bool(data.get("auto_discover", False))


"""IP Checker — verifies masked IP through the proxy pipeline."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class IPCheckResult:
    ip: str = ""
    country: str = ""
    isp: str = ""
    is_tor: bool = False
    latency_ms: float = 0.0
    error: str = ""
    checked_at: float = field(default_factory=time.time)

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.checked_at) > 120

    @property
    def status_emoji(self) -> str:
        if self.error:
            return "🔴"
        if self.ip:
            return "🟢"
        return "⚪"


class IPChecker:
    """Query external IP services through the active proxy pipeline."""

    def __init__(self, cache_seconds: int = 60):
        self._cache_seconds = cache_seconds
        self._cached: Optional[IPCheckResult] = None

    async def check(self, proxy_url: str = "") -> IPCheckResult:
        if self._cached and not self._cached.is_stale and not proxy_url:
            return self._cached

        connector = None
        if proxy_url:
            try:
                from aiohttp_socks import ProxyConnector
                connector = ProxyConnector.from_url(proxy_url)
            except ImportError:
                logger.warning("aiohttp-socks not available for IP check")

        start = time.monotonic()
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                # Primary: ipify
                async with session.get("https://api.ipify.org?format=json") as resp:
                    if resp.status != 200:
                        return IPCheckResult(error=f"HTTP {resp.status}")
                    data = await resp.json()
                    ip = data.get("ip", "")

                # Geo info: ipinfo.io
                country, isp = "", ""
                try:
                    async with session.get(f"https://ipinfo.io/{ip}/json") as r2:
                        if r2.status == 200:
                            info = await r2.json()
                            country = info.get("country", "")
                            isp = info.get("org", "").lstrip("AS")
                except Exception:
                    pass

                # Tor check
                is_tor = False
                try:
                    async with session.get("https://check.torproject.org/api/ip") as r3:
                        if r3.status == 200:
                            tor_data = await r3.json()
                            is_tor = tor_data.get("IsTor", False)
                except Exception:
                    pass

                latency = (time.monotonic() - start) * 1000
                result = IPCheckResult(
                    ip=ip, country=country, isp=isp, is_tor=is_tor,
                    latency_ms=latency, checked_at=time.time(),
                )
                self._cached = result
                return result

        except Exception as exc:
            return IPCheckResult(error=str(exc), checked_at=time.time())

    def clear_cache(self) -> None:
        self._cached = None

"""Proxy Finder — finds public proxies that actually work, and proves it.

Nothing here trusts a list.  Every candidate has a request pushed through it
and is dropped unless a reply came back from a *different* IP than our own:
a "proxy" that swallows the CONNECT and fetches the URL itself is transparent
(our real IP is still exposed) and is useless for anonymity.

Free proxies are also slow and short-lived, so results are ranked by measured
latency and re-checked by :class:`ProxyManager` while in use.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from typing import Optional, Sequence

from omnidownloader.core.proxy_manager import ProxyType

logger = logging.getLogger(__name__)

#: An HTTPS endpoint that echoes the caller's IP.  HTTPS on purpose: it also
#: proves the proxy can tunnel TLS (CONNECT) and cannot silently rewrite the
#: page we asked for.
DEFAULT_CHECK_URL = "https://api.ipify.org/?format=json"
#: Best-effort geo lookup for the winner only (never per candidate).
GEO_URL = "https://ipinfo.io/{ip}/json"

_USER_AGENT = "OmniDownloader/1.0 (+proxy-check)"

_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_SCHEME_TYPES = {
    "http": ProxyType.HTTP,
    "https": ProxyType.HTTPS,
    "socks": ProxyType.SOCKS5,
    "socks4": ProxyType.SOCKS4,
    "socks4a": ProxyType.SOCKS4,
    "socks5": ProxyType.SOCKS5,
    "socks5h": ProxyType.SOCKS5,
}

#: (url, type used for entries without a scheme)
DEFAULT_SOURCES: tuple[tuple[str, ProxyType], ...] = (
    ("https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
     ProxyType.HTTP),
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
     ProxyType.HTTP),
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
     ProxyType.SOCKS5),
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt",
     ProxyType.SOCKS4),
    ("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
     ProxyType.HTTP),
    ("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
     ProxyType.SOCKS5),
    ("https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
     ProxyType.HTTP),
)


def _is_private_ip(ip: str) -> bool:
    """True for addresses no remote proxy can legitimately exit from."""
    try:
        parts = [int(p) for p in ip.split(".")]
    except ValueError:
        return False
    if len(parts) != 4:
        return False
    a, b = parts[0], parts[1]
    return (a in (10, 127, 0) or (a == 172 and 16 <= b <= 31)
            or (a == 192 and b == 168) or a >= 224)


def extract_ip(text: str) -> str:
    """Pull the IP out of an ipify reply (JSON ``{"ip": "..."}`` or plain)."""
    text = (text or "").strip()
    if not text:
        return ""
    if text.startswith("{"):
        try:
            data = json.loads(text)
            ip = str(data.get("ip", "")).strip()
            if ip:
                return ip
        except (ValueError, AttributeError):
            pass
    match = _IP_RE.search(text)
    return match.group(1) if match else ""


@dataclass
class ProxyCandidate:
    """One proxy endpoint, validated or not."""

    host: str
    port: int
    proxy_type: ProxyType = ProxyType.HTTP
    username: str = ""
    password: str = ""
    #: Filled in by :meth:`ProxyFinder.validate`.
    latency_ms: float = 0.0
    exit_ip: str = ""
    country: str = ""
    source: str = ""
    error: str = ""
    checked_at: float = 0.0

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.proxy_type.value, self.host, self.port)

    @property
    def url(self) -> str:
        auth = ""
        if self.username:
            auth = (f"{self.username}:{self.password}@"
                    if self.password else f"{self.username}@")
        return f"{self.proxy_type.value}://{auth}{self.host}:{self.port}"

    @property
    def label(self) -> str:
        return f"{self.proxy_type.value}://{self.host}:{self.port}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["proxy_type"] = self.proxy_type.value
        return data


def parse_proxy_lines(text: str, default_type: ProxyType = ProxyType.HTTP,
                      source: str = "") -> list[ProxyCandidate]:
    """Parse a proxy list (``host:port``, ``socks5://host:port``, …).

    Handles the shapes the public lists actually emit: bare ``ip:port``,
    ``scheme://ip:port``, ``scheme://user:pass@ip:port``, comments, blank
    lines and CRLF.  Duplicates within one list are collapsed.
    """
    out: list[ProxyCandidate] = []
    seen: set[tuple[str, str, int]] = set()
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        proxy_type = default_type
        if "://" in line:
            scheme, _, line = line.partition("://")
            proxy_type = _SCHEME_TYPES.get(scheme.strip().lower(), default_type)
        username = password = ""
        if "@" in line:
            creds, _, line = line.rpartition("@")
            username, _, password = creds.partition(":")
        line = line.strip().strip("/").split()[0] if line.strip() else ""
        if not line or ":" not in line:
            continue
        host, _, port_text = line.rpartition(":")
        host = host.strip().strip("[]")
        try:
            port = int(port_text.strip())
        except ValueError:
            continue
        if not host or not (0 < port < 65536):
            continue
        cand = ProxyCandidate(host=host, port=port, proxy_type=proxy_type,
                              username=username, password=password, source=source)
        if cand.key in seen:
            continue
        seen.add(cand.key)
        out.append(cand)
    return out


class ProxyFinder:
    """Fetches proxy lists and validates candidates by using them."""

    def __init__(self, check_url: str = DEFAULT_CHECK_URL,
                 sources: Optional[Sequence[tuple[str, ProxyType]]] = None,
                 timeout: float = 8.0, concurrency: int = 50):
        self.check_url = check_url
        self.sources = tuple(sources) if sources is not None else DEFAULT_SOURCES
        self.timeout = timeout
        self.concurrency = concurrency
        self._real_ip: Optional[str] = None
        self._real_ip_error = ""
        #: Serialised so 50 concurrent validations don't all hit ipify at once.
        self._real_ip_lock = asyncio.Lock()

    # ── discovery ───────────────────────────────────────────────

    async def fetch_text(self, url: str) -> str:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=self.timeout + 6)
        headers = {"User-Agent": _USER_AGENT}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                return await resp.text(errors="ignore")

    async def fetch_candidates(self, limit: int = 400) -> list[ProxyCandidate]:
        """Download every list and merge the parsed candidates (deduplicated).

        A source that fails is skipped: these lists go down regularly, and one
        dead URL must not cost the user the whole feature.  Sources are
        interleaved rather than concatenated so a *limit* leaves a mix of
        protocols and providers instead of whatever the first list happened to
        contain.
        """
        results = await asyncio.gather(
            *(self._fetch_one(url, ptype) for url, ptype in self.sources),
            return_exceptions=True,
        )
        lists: list[list[ProxyCandidate]] = [
            r for r in results if not isinstance(r, BaseException) and r
        ]
        merged: list[ProxyCandidate] = []
        seen: set[tuple[str, str, int]] = set()
        depth = 0
        while lists:
            for candidates in list(lists):
                if depth >= len(candidates):
                    lists.remove(candidates)
                    continue
                cand = candidates[depth]
                if cand.key in seen:
                    continue
                seen.add(cand.key)
                merged.append(cand)
            depth += 1
        logger.info("Proxy finder: %d unique candidates from %d sources",
                    len(merged), len(self.sources))
        return merged[:limit]

    async def _fetch_one(self, url: str,
                         ptype: ProxyType) -> list[ProxyCandidate]:
        try:
            text = await self.fetch_text(url)
        except Exception as exc:  # noqa: BLE001 - one dead list is not fatal
            logger.info("Proxy source failed (%s): %s", url, exc)
            return []
        return parse_proxy_lines(text, ptype, source=url)

    # ── validation ──────────────────────────────────────────────

    async def real_ip(self) -> str:
        """Our own public IP (cached); used to reject transparent proxies."""
        if self._real_ip is not None:
            return self._real_ip
        async with self._real_ip_lock:
            if self._real_ip is not None:       # filled while we waited
                return self._real_ip
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(
                            self.check_url,
                            headers={"User-Agent": _USER_AGENT}) as resp:
                        ip = extract_ip(await resp.text())
                if not ip:
                    self._real_ip_error = "no IP in reply"
                self._real_ip = ip
            except Exception as exc:  # noqa: BLE001
                self._real_ip_error = str(exc)
                self._real_ip = ""
            return self._real_ip

    def _session_for(self, cand: ProxyCandidate):
        """ClientSession routed through *cand* (SOCKS needs its own connector)."""
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=self.timeout,
                                       connect=min(5.0, self.timeout))
        if cand.proxy_type in (ProxyType.SOCKS4, ProxyType.SOCKS5):
            try:
                from aiohttp_socks import ProxyConnector
            except ImportError as exc:
                raise RuntimeError("aiohttp-socks is not installed") from exc
            return aiohttp.ClientSession(
                connector=ProxyConnector.from_url(cand.url), timeout=timeout)
        # An https:// target through an http:// proxy is a CONNECT tunnel,
        # which aiohttp handles from the proxy= argument.
        return aiohttp.ClientSession(proxy=cand.url, timeout=timeout)

    async def validate(self, cand: ProxyCandidate) -> bool:
        """Push a request through *cand*; fill in latency/exit IP on success.

        Fails (and records why in ``cand.error``) when the proxy is dead,
        cannot tunnel TLS, or is transparent — i.e. exits with an IP that is
        either our own or a private address, which means no remote hop was
        ever made.
        """
        start = time.monotonic()
        try:
            session = self._session_for(cand)
            async with session:
                async with session.get(self.check_url,
                                       headers={"User-Agent": _USER_AGENT}) as resp:
                    if resp.status != 200:
                        cand.error = f"HTTP {resp.status}"
                        return False
                    ip = extract_ip(await resp.text())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - dead proxies are noise
            cand.error = f"{type(exc).__name__}: {str(exc)[:120]}"
            return False
        if not ip:
            cand.error = "no IP in reply"
            return False
        ours = await self.real_ip()
        if ours and ip == ours:
            cand.error = "transparent: exits with our own IP"
            return False
        if not ours and _is_private_ip(ip):
            cand.error = f"implausible exit IP {ip}"
            return False
        cand.exit_ip = ip
        cand.latency_ms = (time.monotonic() - start) * 1000.0
        cand.checked_at = time.time()
        return True

    async def find_best(self, max_candidates: int = 250,
                        deadline: float = 30.0) -> Optional[ProxyCandidate]:
        """Return the fastest working candidate, or ``None``.

        *deadline* bounds the whole search: a wall of dead free proxies can
        otherwise keep the UI waiting for minutes.
        """
        candidates = await self.fetch_candidates()
        if not candidates:
            return None
        candidates = candidates[:max_candidates]
        # Resolve our own IP up front: without it the transparency check is
        # skipped, and 50 concurrent probes would each fetch it separately.
        await self.real_ip()
        working: list[ProxyCandidate] = []
        sem = asyncio.Semaphore(self.concurrency)

        async def probe(cand: ProxyCandidate) -> None:
            async with sem:
                if await self.validate(cand):
                    working.append(cand)
                    logger.info("Proxy works: %s (%.0f ms, exit %s)",
                                cand.label, cand.latency_ms, cand.exit_ip)

        tasks = [asyncio.ensure_future(probe(c)) for c in candidates]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True),
                                   timeout=deadline)
        except asyncio.TimeoutError:
            logger.info("Proxy search hit the %.0fs deadline (checked %d/%d)",
                        deadline, len(working), len(candidates))
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
        if not working:
            return None
        best = min(working, key=lambda c: c.latency_ms)
        await self._decorate(best)
        return best

    async def _decorate(self, cand: ProxyCandidate) -> None:
        """Best-effort geo info for the chosen proxy (never for all of them)."""
        if not cand.exit_ip:
            return
        try:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=6)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(GEO_URL.format(ip=cand.exit_ip)) as resp:
                    if resp.status == 200:
                        info = await resp.json(content_type=None)
                        cand.country = str(info.get("country", "") or "")
        except Exception as exc:  # noqa: BLE001 - geo is cosmetic
            logger.debug("Geo lookup failed for %s: %s", cand.exit_ip, exc)

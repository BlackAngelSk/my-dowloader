"""Dependency Manager — auto-download and update ffmpeg and yt-dlp binaries."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import stat
from pathlib import Path
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# Retry settings for transient DNS / network errors
_MAX_RETRIES = 3
_RETRY_BACKOFF = [2, 5, 10]  # seconds between attempts

# Binary download URLs
_YTDLP_URLS = {
    "Linux": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux",
    "Darwin": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos",
    "Windows": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
}

FFMPEG_URLS = {
    "Linux": "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
    "Darwin": "https://evermeet.cx/ffmpeg/ffmpeg-7.0.2.zip",
    "Windows": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
}


class DependencyManager:
    """Ensures yt-dlp and ffmpeg are available locally."""

    def __init__(self, deps_dir: str = "") -> None:
        self._deps_dir = Path(deps_dir or Path.home() / ".omnidownloader" / "deps")
        self._deps_dir.mkdir(parents=True, exist_ok=True)
        self._system = platform.system()

    @property
    def ytdlp_path(self) -> str:
        """Return path to yt-dlp binary."""
        system = self._system
        ext = ".exe" if system == "Windows" else ""
        local = self._deps_dir / f"yt-dlp{ext}"
        if local.exists():
            return str(local)
        # Fall back to system PATH
        found = shutil.which("yt-dlp")
        if found:
            return found
        return str(local)  # will fail later if not downloaded

    @property
    def ffmpeg_path(self) -> str:
        """Return path to ffmpeg binary."""
        system = self._system
        ext = ".exe" if system == "Windows" else ""
        local = self._deps_dir / f"ffmpeg{ext}"
        if local.exists():
            return str(local)
        found = shutil.which("ffmpeg")
        if found:
            return found
        return str(local)

    async def ensure_all(self) -> dict[str, str]:
        """Download both yt-dlp and ffmpeg if missing.

        Returns a dict mapping ``"yt-dlp"`` / ``"ffmpeg"`` to their final
        paths.  Errors for individual tools are logged but do not prevent
        the other from being downloaded.
        """
        paths: dict[str, str] = {}
        for name, coro in [("yt-dlp", self.ensure_ytdlp), ("ffmpeg", self.ensure_ffmpeg)]:
            try:
                paths[name] = await coro()
            except Exception:
                logger.exception("Failed to auto-install %s", name)
        return paths

    async def ensure_ytdlp(self) -> str:
        """Download yt-dlp if not already present. Returns the path."""
        if Path(self.ytdlp_path).exists():
            logger.info("yt-dlp found at %s", self.ytdlp_path)
            return self.ytdlp_path

        url = _YTDLP_URLS.get(self._system)
        if not url:
            raise OSError(f"Unsupported platform: {self._system}")

        logger.info("Downloading yt-dlp for %s…", self._system)
        dest = self._deps_dir / ("yt-dlp.exe" if self._system == "Windows" else "yt-dlp")
        await self._download_file(url, dest)
        dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        logger.info("yt-dlp installed at %s", dest)
        return str(dest)

    async def ensure_ffmpeg(self) -> str:
        """Download ffmpeg if not already present. Returns the path."""
        if Path(self.ffmpeg_path).exists():
            return self.ffmpeg_path

        url = FFMPEG_URLS.get(self._system)
        if not url:
            raise OSError(f"Unsupported platform: {self._system}")

        logger.info("Downloading ffmpeg for %s…", self._system)
        dest = self._deps_dir / ("ffmpeg.exe" if self._system == "Windows" else "ffmpeg")
        await self._download_file(url, dest)
        dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        logger.info("ffmpeg installed at %s", dest)
        return str(dest)

    async def _download_file(self, url: str, dest: Path) -> None:
        """Download a file with automatic retries on DNS / network errors."""
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                timeout = aiohttp.ClientTimeout(total=300, connect=30)
                connector = aiohttp.TCPConnector(
                    force_close=True,
                    enable_cleanup_closed=True,
                )
                async with aiohttp.ClientSession(
                    timeout=timeout, connector=connector,
                ) as session:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"Download failed: HTTP {resp.status}")
                        # Write to a temporary file first, then rename atomically
                        tmp = dest.with_suffix(dest.suffix + ".tmp")
                        try:
                            with open(tmp, "wb") as f:
                                async for chunk in resp.content.iter_chunked(256 * 1024):
                                    f.write(chunk)
                            tmp.replace(dest)
                        except BaseException:
                            tmp.unlink(missing_ok=True)
                            raise
                return  # success
            except (
                aiohttp.ClientError,
                OSError,
                RuntimeError,
                asyncio.TimeoutError,
            ) as exc:
                last_exc = exc
                wait = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                logger.warning(
                    "Download attempt %d/%d failed for %s: %s — retrying in %ds",
                    attempt + 1, _MAX_RETRIES, url, exc, wait,
                )
                await asyncio.sleep(wait)
        # All retries exhausted
        raise RuntimeError(
            f"Failed to download {url} after {_MAX_RETRIES} attempts: {last_exc}"
        )

    def check_all(self) -> dict[str, bool]:
        return {
            "yt-dlp": Path(self.ytdlp_path).exists() or shutil.which("yt-dlp") is not None,
            "ffmpeg": Path(self.ffmpeg_path).exists() or shutil.which("ffmpeg") is not None,
        }

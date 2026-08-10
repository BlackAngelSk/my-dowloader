"""Dependency Manager — auto-download and update ffmpeg and yt-dlp binaries."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import stat
from pathlib import Path
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

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
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Download failed: HTTP {resp.status}")
                with open(dest, "wb") as f:
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        f.write(chunk)

    def check_all(self) -> dict[str, bool]:
        return {
            "yt-dlp": Path(self.ytdlp_path).exists() or shutil.which("yt-dlp") is not None,
            "ffmpeg": Path(self.ffmpeg_path).exists() or shutil.which("ffmpeg") is not None,
        }

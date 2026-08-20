"""Media Extractor — yt-dlp + ffmpeg wrapper module."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from omnidownloader.core.base_module import BaseDownloaderModule
from omnidownloader.core.models import DownloadJob, DownloadState
from omnidownloader.core.disk_utils import ensure_directory
from omnidownloader.services.dependency_manager import DependencyManager

logger = logging.getLogger(__name__)

MEDIA_DOMAINS = {
    "youtube.com", "youtu.be", "m.youtube.com",
    "twitter.com", "x.com", "nitter.net",
    "tiktok.com", "vm.tiktok.com",
    "instagram.com",
    "facebook.com", "fb.watch", "fb.com",
    "reddit.com", "v.redd.it",
    "twitch.tv", "clips.twitch.tv", "vimeo.com", "dailymotion.com",
    "bilibili.com", "soundcloud.com", "bandcamp.com",
    "rutube.ru", "www.rutube.ru",
    # Popular platforms
    "kick.com",
    "ok.ru",
    "dzen.ru",
    "nicovideo.jp", "nico.ms",
    "odysee.com", "odys.ly",
    "archive.org",
}

# ── Universal best-quality format strings ──────────────────────
# These let yt-dlp pick the absolute best adaptive streams and merge
# them with ffmpeg, regardless of container format.
#
# ``bv*+ba/b`` — prefer adaptive video-only + audio-only streams (merged
# with ffmpeg), falling back to a single muxed stream if nothing better
# is available.  ``bv*`` (bestvideo*) is more robust than the older
# ``bestvideo`` against YouTube's ever-changing format-ID schemes.
BEST_VIDEO_AUDIO = "bv*+ba/b"
BEST_AUDIO_ONLY = "bestaudio/best"

# ── YouTube-specific client configuration ──────────────────────
# YouTube heavily throttles / hides high-res adaptive streams (1080p+)
# from the default ``web`` player client.  The ``android_vr`` client
# bypasses YouTube's SABR-only streaming experiment and unlocks ALL
# formats up to 4K (2160p).  ``web`` is kept as a fallback so that
# age-gated or region-locked content still works.
YOUTUBE_DOMAINS = {"youtube.com", "youtu.be", "m.youtube.com"}

_YOUTUBE_EXTRACTOR_ARGS = (
    "--extractor-args", "youtube:player_client=android_vr,web"
)


# ── Regex for parsing yt-dlp download progress ─────────────────
# Matches lines like:
#   [download]  45.2% of  100.00MiB at   12.34MiB/s ETA 00:03
#   [download]  99.7% of ~ 281.30MiB at    8.65MiB/s ETA 00:01 (frag 104/105)
_DL_PROGRESS_RE = re.compile(
    r"\[download\]\s+"
    r"([\d.]+)%"                     # 1: percentage
    r"\s+of\s+"
    r"~?\s*"
    r"([\d.]+)\s*"                   # 2: size value
    r"(KiB|MiB|GiB|TiB|B)"          # 3: size unit
    r"\s+at\s+"
    r"([\d.]+)\s*"                   # 4: speed value
    r"(KiB/s|MiB/s|GiB/s|TiB/s|B/s)" # 5: speed unit
    r"(?:\s+ETA\s+"
    r"(\d+:\d+(?::\d+)?))?"          # 6: ETA (optional)
)

_SIZE_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
_SPEED_UNITS = {"B/s": 1, "KiB/s": 1024, "MiB/s": 1024**2, "GiB/s": 1024**3, "TiB/s": 1024**4}


def _parse_size_bytes(value: float, unit: str) -> int:
    """Convert a size value + unit to bytes."""
    return int(value * _SIZE_UNITS.get(unit, 1))


def _parse_speed_bps(value: float, unit: str) -> float:
    """Convert a speed value + unit to bytes per second."""
    return value * _SPEED_UNITS.get(unit, 1)


def _parse_eta_seconds(eta_str: str) -> float | None:
    """Parse an ETA string like '01:23' or '1:02:03' to seconds."""
    if not eta_str:
        return None
    parts = eta_str.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return None
    return None


class MediaExtractor(BaseDownloaderModule):
    MODULE_NAME = "media"

    def __init__(self, ytdlp_path="yt-dlp", ffmpeg_path="ffmpeg", proxy_manager=None):
        self._ytdlp = ytdlp_path
        self._ffmpeg = ffmpeg_path
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._proxy_manager = proxy_manager

    async def _ensure_ytdlp_available(self) -> str:
        """Ensure yt-dlp exists at ``self._ytdlp``, downloading it if necessary.

        Returns the (possibly updated) path to the yt-dlp binary.
        Raises ``RuntimeError`` if the binary cannot be obtained.
        """
        # Check if the path points to an existing file
        if Path(self._ytdlp).exists():
            return self._ytdlp
        # Check if the name is available on PATH (e.g. "yt-dlp" via pip)
        if shutil.which(self._ytdlp):
            return self._ytdlp
        # Try to auto-download
        logger.info("yt-dlp not found — attempting automatic download…")
        try:
            dm = DependencyManager()
            path = await dm.ensure_ytdlp()
            self._ytdlp = path
            return path
        except Exception as exc:
            raise RuntimeError(
                f"yt-dlp not found and auto-download failed: {exc}\n"
                "Install it manually: https://github.com/yt-dlp/yt-dlp#installation"
            ) from exc

    # ── YouTube helper methods ──────────────────────────────────

    @staticmethod
    def _is_youtube_url(url: str) -> bool:
        """Return *True* when *url* points to a YouTube domain."""
        try:
            host = (urlparse(url).hostname or "").removeprefix("www.")
            return host in YOUTUBE_DOMAINS
        except Exception:
            return False

    @staticmethod
    def _append_youtube_args(cmd: list[str], url: str) -> list[str]:
        """Append YouTube extractor-args and geo-bypass to *cmd* when *url* is a YouTube link."""
        if MediaExtractor._is_youtube_url(url):
            cmd += list(_YOUTUBE_EXTRACTOR_ARGS)
            cmd.append("--geo-bypass")
        return cmd

    # ── URL routing ─────────────────────────────────────────────

    def can_handle(self, url):
        """Accept any HTTP/HTTPS URL — yt-dlp supports thousands of sites.

        Excludes direct image file URLs so ImageScraper can handle those,
        and the ``scrape:`` prefix which is an ImageScraper convention.
        """
        try:
            if url.startswith("scrape:"):
                return False  # let ImageScraper handle it
            scheme = urlparse(url).scheme.lower()
            if scheme in ("http", "https"):
                # Exclude direct image file links (ImageScraper territory)
                path = urlparse(url).path.lower()
                _IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
                              ".svg", ".tiff", ".avif")
                if any(path.endswith(e) for e in _IMAGE_EXT):
                    return False
                return True
            # Also accept other schemes yt-dlp understands (rtmp, rtsp, etc.)
            if scheme in ("rtmp", "rtmpe", "rtmps", "rtsp", "mms", "m3u8"):
                return True
            return False
        except Exception:
            return False

    @staticmethod
    async def probe_url(url: str, ytdlp_path: str = "yt-dlp") -> bool:
        """Quick probe: return True if yt-dlp can handle *url*.

        Runs ``yt-dlp --simulate --no-download`` and checks the exit code.
        This is used as a catch-all fallback for URLs not in MEDIA_DOMAINS.
        If yt-dlp is not found, attempts auto-download first.
        """
        # If yt-dlp not found, try to auto-download
        if not Path(ytdlp_path).exists() and not shutil.which(ytdlp_path):
            try:
                dm = DependencyManager()
                ytdlp_path = await dm.ensure_ytdlp()
            except Exception:
                logger.warning("Could not auto-download yt-dlp for probe")
                return False
        if not shutil.which(ytdlp_path) and not Path(ytdlp_path).exists():
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                ytdlp_path, "--simulate", "--no-download",
                "--no-warnings", "--no-check-certificates",
                url,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
            return proc.returncode == 0
        except (asyncio.TimeoutError, OSError):
            return False

    async def extract_metadata(self, url):
        await self._ensure_ytdlp_available()
        cmd = [self._ytdlp, "--dump-json", "--no-download",
               "--no-warnings", "--no-check-certificates"]
        self._append_youtube_args(cmd, url)
        if self._proxy_manager and self._proxy_manager.enabled:
            cmd += self._proxy_manager.get_ytdlp_args()
        cmd.append(url)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"yt-dlp failed: {stderr.decode(errors='replace')[:500]}")
        info = json.loads(stdout.decode(errors="replace"))
        formats = []
        for f in info.get("formats", []):
            formats.append({
                "format_id": f.get("format_id", ""),
                "ext": f.get("ext", ""),
                "resolution": f.get("resolution", "audio only"),
                "filesize": f.get("filesize") or f.get("filesize_approx", 0),
                "vcodec": f.get("vcodec", "none"),
                "acodec": f.get("acodec", "none"),
                "fps": f.get("fps") or 0,
                "abr": f.get("abr") or 0,
                "height": f.get("height") or 0,
                "width": f.get("width") or 0,
                "protocol": f.get("protocol", ""),
                "format_note": f.get("format_note", ""),
                "tbr": f.get("tbr") or 0,  # total bitrate
            })
        return {"title": info.get("title", "Unknown"),
                "thumbnail": info.get("thumbnail", ""),
                "duration": info.get("duration", 0),
                "uploader": info.get("uploader", ""),
                "formats": formats,
                "subtitles": list(info.get("subtitles", {}).keys()),
                "filesize_best": info.get("filesize") or info.get("filesize_approx", -1),
                "has_video": any(f.get("vcodec", "none") != "none" for f in info.get("formats", [])),
                "has_audio": any(f.get("acodec", "none") != "none" for f in info.get("formats", [])),
                "max_height": max((f.get("height") or 0 for f in info.get("formats", [])), default=0),}

    async def start_download(self, job, progress_callback=None):
        job.state = DownloadState.DOWNLOADING
        self._cancel_events[job.id] = asyncio.Event()

        # Ensure yt-dlp is available (auto-download if missing)
        try:
            await self._ensure_ytdlp_available()
        except RuntimeError as exc:
            job.state = DownloadState.FAILED
            job.error_message = str(exc)
            return

        # Resolve ffmpeg path and check availability for merge-heavy downloads
        ffmpeg_dir = self._resolve_ffmpeg_dir()
        needs_merge = not job.metadata.get("audio_only", False)
        if needs_merge and not ffmpeg_dir:
            logger.warning("ffmpeg not found — high-res video+audio merge may fail")

        # Get the user's chosen format, or use universal best
        fmt = job.metadata.get("format", BEST_VIDEO_AUDIO)
        audio_only = job.metadata.get("audio_only", False)

        output_dir = Path(job.file_path).parent if job.file_path else Path.home()/"Downloads"/"OmniDownloader"
        output_tpl = str(output_dir / "%(title)s.%(ext)s")

        # Build the yt-dlp command
        if audio_only:
            audio_fmt = job.metadata.get("audio_format", BEST_AUDIO_ONLY)
            audio_ext = job.metadata.get("audio_format_ext", "mp3")
            cmd = [self._ytdlp, "-f", audio_fmt,
                   "-x", "--audio-format", audio_ext,
                   "--newline", "-o", output_tpl]
        else:
            cmd = [self._ytdlp, "-f", fmt,
                   "--merge-output-format", "mp4",
                   "--newline", "--no-warnings", "-o", output_tpl]

        # Tell yt-dlp where ffmpeg lives so it can merge video+audio streams
        if ffmpeg_dir:
            cmd += ["--ffmpeg-location", ffmpeg_dir]

        if job.metadata.get("subtitles"):
            cmd += ["--write-subs", "--sub-langs", "en"]
        self._append_youtube_args(cmd, job.url)
        if self._proxy_manager and self._proxy_manager.enabled:
            cmd += self._proxy_manager.get_ytdlp_args()
        cmd.append(job.url)

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        if proc.stdout:
            async for line in proc.stdout:
                if self._cancel_events.get(job.id, asyncio.Event()).is_set():
                    proc.terminate()
                    await proc.wait()
                    raise asyncio.CancelledError()
                text = line.decode(errors="replace").strip()
                if "[download]" in text:
                    # Try to parse the full progress line with size, speed, ETA
                    m = _DL_PROGRESS_RE.search(text)
                    if m:
                        pct = float(m.group(1))
                        total_bytes = _parse_size_bytes(float(m.group(2)), m.group(3))
                        speed = _parse_speed_bps(float(m.group(4)), m.group(5))
                        eta = _parse_eta_seconds(m.group(6) or "")

                        job.file_size = total_bytes
                        job.update_speed(speed)
                        # Set downloaded_bytes directly from percentage + total
                        job.downloaded_bytes = int(pct / 100.0 * total_bytes)

                        if progress_callback:
                            progress_callback(job)
                    elif "%" in text:
                        # Fallback: at least parse the percentage
                        try:
                            for part in text.split():
                                if part.endswith("%"):
                                    job.progress_percent = float(part.rstrip("%"))
                                    break
                        except (ValueError, IndexError):
                            pass
                        if progress_callback:
                            progress_callback(job)
                elif "[Merger]" in text or "[ExtractAudio]" in text:
                    job.state = DownloadState.MERGING
                    if progress_callback:
                        progress_callback(job)

        await proc.wait()
        self._cancel_events.pop(job.id, None)
        if proc.returncode != 0:
            stderr_bytes = await proc.stderr.read() if proc.stderr else b""
            stderr_out = stderr_bytes.decode(errors="replace")
            raise RuntimeError(f"yt-dlp failed: {stderr_out[:500]}")
        # Find the downloaded file in the output directory
        candidates = sorted(output_dir.glob("*"), key=os.path.getmtime, reverse=True)
        if candidates:
            job.file_path = str(candidates[0])

    def _resolve_ffmpeg_dir(self) -> str:
        """Return the directory containing ffmpeg, or empty string."""
        ffmpeg_path = self._ffmpeg
        # Check if it's a full path or just a name
        if os.path.isfile(ffmpeg_path):
            return str(Path(ffmpeg_path).parent)
        found = shutil.which(ffmpeg_path)
        if found:
            return str(Path(found).parent)
        return ""

    async def cancel(self, job):
        e = self._cancel_events.get(job.id)
        if e:
            e.set()


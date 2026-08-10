"""Media Extractor — yt-dlp + ffmpeg wrapper module."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from omnidownloader.core.base_module import BaseDownloaderModule
from omnidownloader.core.models import DownloadJob, DownloadState
from omnidownloader.core.disk_utils import ensure_directory

logger = logging.getLogger(__name__)

MEDIA_DOMAINS = {
    "youtube.com", "youtu.be", "m.youtube.com",
    "twitter.com", "x.com", "nitter.net",
    "tiktok.com", "vm.tiktok.com",
    "instagram.com",
    "facebook.com", "fb.watch", "fb.com",
    "reddit.com", "v.redd.it",
    "twitch.tv", "vimeo.com", "dailymotion.com",
    "bilibili.com", "soundcloud.com", "bandcamp.com",
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


class MediaExtractor(BaseDownloaderModule):
    MODULE_NAME = "media"

    def __init__(self, ytdlp_path="yt-dlp", ffmpeg_path="ffmpeg", proxy_manager=None):
        self._ytdlp = ytdlp_path
        self._ffmpeg = ffmpeg_path
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._proxy_manager = proxy_manager

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
        try:
            host = (urlparse(url).hostname or "").removeprefix("www.")
            return host in MEDIA_DOMAINS
        except Exception:
            return False

    async def extract_metadata(self, url):
        if not shutil.which(self._ytdlp):
            raise RuntimeError(
                f"yt-dlp not found at '{self._ytdlp}'. Install it: pacman -S yt-dlp"
            )
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

        # Ensure yt-dlp is available
        if not shutil.which(self._ytdlp):
            job.state = DownloadState.FAILED
            job.error_message = f"yt-dlp not found. Install it: pacman -S yt-dlp"
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
                if "[download]" in text and "%" in text:
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
        parent = Path(job.file_path).parent if job.file_path else Path.cwd()
        candidates = sorted(parent.glob("*"), key=os.path.getmtime, reverse=True)
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


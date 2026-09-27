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

from omnidownloader.core import platform_utils
from omnidownloader.core.base_module import BaseDownloaderModule
from omnidownloader.core.models import DownloadJob, DownloadState
from omnidownloader.core.disk_utils import ensure_directory, verify_checksum
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
#: ``ba/b`` rather than ``bestaudio/best``: clients that advertise no
#: audio-only streams (tv/web_safari, mweb, tv_simply, android…) fail with
#: "Requested format is not available" for ``bestaudio``, while ``ba/b`` falls
#: back to a muxed stream and ``-x`` extracts its audio.  Verified by running
#: both selectors against every client in the matrix below.
BEST_AUDIO_ONLY = "ba/b"

# ── YouTube-specific client configuration ──────────────────────
# YouTube gates its high-res adaptive streams behind a "player client".
# Do NOT use ``android_vr`` here: it advertises the full 2160p AV1
# ladder, but every one of those streams is SABR / PO-token protected
# and the actual media request comes back 403, which surfaces to the
# user as:
#     yt-dlp failed: ERROR: unable to download video data: HTTP Error 403
# ``web_embedded`` is verified to both expose the full 144p–2160p
# ladder (h264 / vp9 / av1) *and* actually serve the data, so it leads
# the candidate list.  The rest are download-verified fallbacks for
# videos where ``web_embedded`` is unavailable ("page needs to be
# reloaded", "requested format is not available", …).
YOUTUBE_DOMAINS = {"youtube.com", "youtu.be", "m.youtube.com"}

_YOUTUBE_CLIENT_CANDIDATES: tuple[str, ...] = (
    "web_embedded",
    "tv,web_safari",
    "web_safari",
    "web",
    "android",
    "mweb",
    "tv_simply",
)

#: Clients deliberately NOT in the list above, and why (measured, audio+video):
#:   android_vr  — advertises 2160p but every stream 403s on download
#:   tv          — "The page needs to be reloaded"
#:   ios         — "Requested format is not available"
#: Re-check with the matrix in tools/test_audio_only.py before adding any back.

# stderr fragments from yt-dlp that mean "this player client is being
# rejected — try the next one" rather than "this URL is undownloadable".
_RETRYABLE_YTDLP_MARKERS: tuple[str, ...] = (
    "403",
    "forbidden",
    "page needs to be reloaded",
    # YouTube intermittently returns formats without URLs (SABR-only) for a
    # client that worked moments ago: the same request usually succeeds.
    "sabr",
    "missing a url",
    "some web_embedded client",
    "requested format is not available",
    "sign in to confirm",
    "unable to extract",
    "player response",
)

_PARTIAL_SUFFIXES = (".part", ".ytdl", ".temp")


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
        # Last YouTube player_client that actually worked — tried first next time
        self._yt_client: str | None = None
        # Live yt-dlp processes, so pause/resume/cancel can act on them
        self._procs: dict[str, asyncio.subprocess.Process] = {}

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
        """Return *True* when *url* points to a YouTube domain (incl. subdomains)."""
        try:
            host = (urlparse(url).hostname or "").removeprefix("www.")
            return MediaExtractor._host_matches(host, YOUTUBE_DOMAINS)
        except Exception:
            return False

    @staticmethod
    def _client_candidates(url: str, preferred: str | None = None) -> tuple[str | None, ...]:
        """Return the ordered ``youtube:player_client`` values to try for *url*.

        Non-YouTube URLs get a single ``None`` entry — no extractor args and
        no client retries.
        """
        if not MediaExtractor._is_youtube_url(url):
            return (None,)
        order: list[str] = []
        if preferred:
            order.append(preferred)
        for candidate in _YOUTUBE_CLIENT_CANDIDATES:
            if candidate not in order:
                order.append(candidate)
        return tuple(order)

    @staticmethod
    def _is_retryable_failure(stderr: str) -> bool:
        """Return *True* when *stderr* looks like a rejected player client."""
        low = stderr.lower()
        return any(marker in low for marker in _RETRYABLE_YTDLP_MARKERS)

    @staticmethod
    def _append_youtube_args(cmd: list[str], url: str, client: str | None = None) -> list[str]:
        """Append YouTube player-client args + geo-bypass to *cmd* for YouTube links."""
        if MediaExtractor._is_youtube_url(url):
            client = client or _YOUTUBE_CLIENT_CANDIDATES[0]
            cmd += ["--extractor-args", f"youtube:player_client={client}"]
            cmd.append("--geo-bypass")
        return cmd

    @staticmethod
    def _cleanup_partial_downloads(directory: Path) -> None:
        """Remove ``.part`` / ``.ytdl`` leftovers from a failed attempt."""
        try:
            for path in directory.glob("*"):
                if path.is_file() and path.name.endswith(_PARTIAL_SUFFIXES):
                    path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("Could not clean partial downloads in %s: %s", directory, exc)

    # ── URL routing ─────────────────────────────────────────────

    #: Schemes only yt-dlp understands (never a plain HTTP fetch).
    STREAMING_SCHEMES = ("rtmp", "rtmpe", "rtmps", "rtsp", "mms", "m3u8")

    @staticmethod
    def _host_matches(host: str, domains: set[str]) -> bool:
        """True when *host* is a domain in *domains* or a subdomain of one."""
        if not host:
            return False
        host = host.removeprefix("www.")
        return any(host == d or host.endswith("." + d) for d in domains)

    def can_handle(self, url):
        """Claim URLs this module is actually the right tool for.

        Deliberately NOT a catch-all for http(s): the download manager routes
        unknown-but-yt-dlp-supported URLs here itself (see
        ``DownloadManager._dispatch_job``), so claiming every URL would just
        starve the multi-connection HTTPDownloader — every direct file
        download would go through yt-dlp instead of the segmented engine.
        """
        try:
            if url.startswith("scrape:"):
                return False  # let ImageScraper handle it
            parsed = urlparse(url)
            scheme = parsed.scheme.lower()
            if scheme in self.STREAMING_SCHEMES:
                return True
            if scheme in ("http", "https"):
                path = parsed.path.lower()
                _IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
                              ".svg", ".tiff", ".avif")
                if any(path.endswith(e) for e in _IMAGE_EXT):
                    return False  # ImageScraper territory
                return self._host_matches(parsed.hostname or "", MEDIA_DOMAINS)
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
            cmd = [ytdlp_path, "--simulate", "--no-download",
                   "--no-warnings", "--no-check-certificates"]
            MediaExtractor._append_youtube_args(cmd, url)
            cmd.append(url)
            proc = await asyncio.create_subprocess_exec(
                *cmd, **platform_utils.subprocess_kwargs(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
            return proc.returncode == 0
        except (asyncio.TimeoutError, OSError):
            return False

    async def _dump_json(self, url: str, single: bool = False,
                         client: Optional[str] = None) -> dict:
        """Run yt-dlp and return parsed JSON for *url*.

        ``single=True`` uses ``--dump-single-json --flat-playlist``, which
        emits exactly one document for playlists too — a plain ``--dump-json``
        on a playlist prints one JSON object per entry, and parsing that as a
        single document failed with "Extra data: line 2 column 1".

        ``client`` forces one specific player client (used by the degraded
        ladder retry); otherwise the cached/ordered candidates are tried.
        """
        await self._ensure_ytdlp_available()
        candidates = (client,) if client else self._client_candidates(url, self._yt_client)
        stdout = b""
        last_err = ""
        for idx, cand in enumerate(candidates):
            cmd = [self._ytdlp, "--no-download", "--no-warnings",
                   "--no-check-certificates"]
            cmd += (["--dump-single-json", "--flat-playlist"] if single
                    else ["--dump-json"])
            self._append_youtube_args(cmd, url, cand)
            if self._proxy_manager and self._proxy_manager.enabled:
                cmd += self._proxy_manager.get_ytdlp_args()
            cmd.append(url)
            proc = await asyncio.create_subprocess_exec(
                *cmd, **platform_utils.subprocess_kwargs(),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                if cand:
                    self._yt_client = cand
                break
            last_err = stderr.decode(errors="replace")[:500]
            if cand is None or idx == len(candidates) - 1 or not self._is_retryable_failure(last_err):
                raise RuntimeError(f"yt-dlp failed: {last_err}")
            logger.warning(
                "yt-dlp metadata failed with player_client=%s (%s) — retrying with the next client",
                cand, last_err.strip().splitlines()[0][:160] if last_err.strip() else "unknown error",
            )
        try:
            return json.loads(stdout.decode(errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"yt-dlp returned unreadable JSON: {exc}") from exc

    async def extract_metadata(self, url):
        info = await self._dump_json(url, single=True)
        if info.get("_type") == "playlist" or "entries" in info:
            return await self._playlist_metadata(info)
        meta = self._video_metadata(info)
        if self._is_youtube_url(url) and self._looks_degraded(meta):
            # YouTube intermittently answers with a reduced format set (a few
            # 360p formats and storyboards only) for the client that just
            # succeeded.  Ask the other clients before settling for that.
            meta = await self._retry_for_better_ladder(url, meta)
        if self._is_youtube_url(url) and self._has_audio(meta) and not self._audio_choices(meta):
            # Some player clients (tv/web_safari, mweb, tv_simply) expose no
            # separate audio streams at all — and the working client is cached,
            # so an earlier fallback would silently leave the Audio tab empty
            # for the rest of the session ("I can't download only audio").
            meta = await self._retry_for_audio_choices(url, meta)
        return meta

    @staticmethod
    def _audio_choices(meta: dict) -> list[dict]:
        """Formats that are audio-only (no video stream, but real audio)."""
        return [
            f for f in meta.get("formats", [])
            if (f.get("vcodec") in ("none", "", None)
                and f.get("acodec") not in ("none", "", None))
        ]

    @staticmethod
    def _has_audio(meta: dict) -> bool:
        return any(f.get("acodec") not in ("none", "", None)
                   for f in meta.get("formats", []))

    async def _retry_for_audio_choices(self, url: str, best_meta: dict) -> dict:
        """Find a player client that offers separate audio streams.

        ``web_embedded`` is tried first, because it is the client that
        advertises the per-bitrate audio ladder on YouTube.
        """
        candidates = self._client_candidates(url, "web_embedded")
        for client in candidates:
            if client == self._yt_client and self._audio_choices(best_meta):
                continue
            if client == self._yt_client:
                continue
            try:
                candidate = self._video_metadata(await self._dump_json(url, client=client))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Audio-choice retry with player_client=%s failed: %s", client, exc)
                continue
            if self._audio_choices(candidate):
                logger.info(
                    "player_client=%s offers no audio streams — switching to %s for audio choices",
                    self._yt_client, client,
                )
                self._yt_client = client
                return candidate
        # No client helped: keep what we have.  The dialog still offers a
        # synthetic "best audio" option, so audio downloads remain possible.
        logger.debug("No player client offered separate audio streams for %s", url)
        return best_meta

    @staticmethod
    def _looks_degraded(meta: dict) -> bool:
        """True when the format list is suspiciously poor for a video."""
        video_formats = [
            f for f in meta.get("formats", [])
            if f.get("vcodec") not in ("none", "")
        ]
        return len(video_formats) <= 6 and meta.get("max_height", 0) <= 360

    async def _retry_for_better_ladder(self, url: str, best_meta: dict) -> dict:
        """Try the remaining player clients and keep the richest format list."""
        for client in self._client_candidates(url, None):
            if client == self._yt_client:
                continue
            try:
                candidate = self._video_metadata(
                    await self._dump_json(url, client=client)
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Ladder retry with player_client=%s failed: %s", client, exc)
                continue
            if candidate.get("max_height", 0) > best_meta.get("max_height", 0):
                logger.info(
                    "Reduced format ladder (%sp) — player_client=%s offers %sp",
                    best_meta.get("max_height", 0), client,
                    candidate.get("max_height", 0),
                )
                self._yt_client = client
                best_meta = candidate
                if not self._looks_degraded(candidate):
                    break
        return best_meta

    async def _playlist_metadata(self, info: dict) -> dict:
        """Describe a playlist: its entries plus the first entry's formats.

        The format list comes from the first entry so the quality dialog still
        means something; the chosen quality is then applied to every entry.
        """
        items: list[dict] = []
        for entry in (info.get("entries") or []):
            if not entry:
                continue
            entry_url = entry.get("url") or entry.get("webpage_url")
            if not entry_url and entry.get("id"):
                entry_url = f"https://www.youtube.com/watch?v={entry['id']}"
            if not entry_url or not str(entry_url).startswith("http"):
                continue
            items.append({
                "url": entry_url,
                "title": entry.get("title") or "",
                "duration": entry.get("duration") or 0,
            })

        video_meta: dict = {}
        if items:
            try:
                video_meta = self._video_metadata(
                    await self._dump_json(items[0]["url"])
                )
            except Exception as exc:  # noqa: BLE001 - playlist still usable
                logger.debug("Could not read formats from the first entry: %s", exc)

        formats = video_meta.get("formats", [])
        return {
            "title": info.get("title") or "Playlist",
            "thumbnail": info.get("thumbnail") or "",
            "uploader": info.get("uploader") or info.get("channel") or "",
            "duration": 0,
            "is_playlist": True,
            "playlist_count": len(items),
            "entries": items,
            "formats": formats,
            "subtitles": video_meta.get("subtitles", []),
            "filesize_best": -1,
            "has_video": bool(video_meta.get("has_video")),
            "has_audio": bool(video_meta.get("has_audio")),
            "max_height": video_meta.get("max_height", 0),
        }

    @staticmethod
    def _video_metadata(info: dict) -> dict:
        def _codec(value) -> str:
            """Normalise a codec field: extractors send ``"none"`` or ``null``."""
            if value is None:
                return "none"
            return str(value).strip() or "none"

        formats = []
        for f in info.get("formats", []):
            formats.append({
                "format_id": f.get("format_id", ""),
                "ext": f.get("ext", ""),
                "resolution": f.get("resolution", "audio only"),
                "filesize": f.get("filesize") or f.get("filesize_approx", 0),
                "vcodec": _codec(f.get("vcodec")),
                "acodec": _codec(f.get("acodec")),
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
                "has_video": any(f["vcodec"] != "none" for f in formats),
                "has_audio": any(f["acodec"] != "none" for f in formats),
                "max_height": max((f["height"] for f in formats), default=0)}

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
        audio_only = bool(job.metadata.get("audio_only", False))
        # Audio *extraction* (-x) needs ffmpeg too, not just video merging.
        needs_ffmpeg = not audio_only or bool(job.metadata.get("audio_format_ext"))
        if needs_ffmpeg and not ffmpeg_dir:
            logger.warning("ffmpeg not found — high-res video+audio merge may fail")

        # Get the user's chosen format, or use universal best
        fmt = job.metadata.get("format", BEST_VIDEO_AUDIO)

        output_dir = Path(job.file_path).parent if job.file_path else Path.home()/"Downloads"/"OmniDownloader"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_tpl = str(output_dir / "%(title)s.%(ext)s")
        before = {p.name for p in output_dir.glob("*")}

        # Try the player client that last worked (or the first candidate),
        # then fall back down the list when YouTube rejects this one.
        candidates = self._client_candidates(job.url, self._yt_client)
        errors: list[tuple[str, str]] = []
        success = False
        for idx, client in enumerate(candidates):
            # One extra try on the *same* client: YouTube's "SABR-only" /
            # "formats missing a URL" answer is intermittent — the identical
            # request usually succeeds a second later.
            for attempt in range(2):
                # The user's format_id was read with a *different* player client
                # on a fallback attempt, so it may not exist there — widen to
                # the universal selector instead of failing outright.  For an
                # audio-only job that selector must stay audio-only, or the
                # retry quietly downloads the whole video.
                if idx == 0 and attempt == 0 and fmt:
                    attempt_fmt = fmt
                elif job.metadata.get("audio_only", False):
                    attempt_fmt = BEST_AUDIO_ONLY
                else:
                    attempt_fmt = BEST_VIDEO_AUDIO
                cmd = self._build_download_cmd(job, client, attempt_fmt, ffmpeg_dir, output_tpl)
                returncode, stderr_out = await self._stream_download(cmd, job, progress_callback)
                if returncode == 0:
                    success = True
                    break

                last_err = stderr_out[:500]
                errors.append((client or "(default)", last_err))
                if not self._is_retryable_failure(last_err):
                    break
                logger.warning(
                    "yt-dlp download failed with player_client=%s (%s) — %s",
                    client,
                    last_err.strip().splitlines()[0][:160] if last_err.strip() else "unknown error",
                    "retrying the same client" if attempt == 0 else "trying the next client",
                )
                self._cleanup_partial_downloads(output_dir)
            if success:
                if client:
                    self._yt_client = client
                last_err = ""
                break
            if client is None:
                break

        self._cancel_events.pop(job.id, None)
        if not success:
            # Report the *first* failure: the last one is usually a downstream
            # client that was never viable (a PO-token warning tells the user
            # nothing), while the first attempt is the one that nearly worked.
            first_client, first_err = errors[0] if errors else ("(default)", "unknown error")
            if len(errors) > 1:
                logger.error(
                    "All %d player clients failed for %s; first error (%s): %s",
                    len(errors), job.url, first_client, first_err[:300],
                )
            raise RuntimeError(
                f"yt-dlp failed: {first_err}"
                + (f" (also tried {len(errors) - 1} more player clients)"
                   if len(errors) > 1 else "")
            )

        # Point the job at the file this run actually produced
        produced = self._find_downloaded_file(output_dir, before)
        if produced:
            job.file_path = produced
        else:
            logger.warning("yt-dlp reported success but no new file appeared in %s", output_dir)

        expected = job.metadata.get("expected_checksum")
        if expected and job.file_path:
            ok, detail = verify_checksum(job.file_path, expected)
            if not ok:
                logger.error("Checksum verification failed for %s: %s", job.file_path, detail)
                job.state = DownloadState.FAILED
                job.error_message = f"Checksum verification failed — {detail}"
                return
            job.metadata["checksum_verified"] = detail
            logger.info("Checksum verified for %s (%s)", job.file_path, detail)

    def _build_download_cmd(self, job, client: str | None, fmt: str,
                            ffmpeg_dir: str, output_tpl: str) -> list[str]:
        """Assemble the yt-dlp argv for one download attempt."""
        if job.metadata.get("audio_only", False):
            # The selector comes from the caller so a retry can widen it: the
            # dialog's per-row choice ("140/best") does not exist on every
            # player client, and ignoring this argument made every fallback
            # attempt re-request the same missing format.
            audio_fmt = (fmt or job.metadata.get("audio_format")
                         or BEST_AUDIO_ONLY)
            audio_ext = job.metadata.get("audio_format_ext", "")
            cmd = [self._ytdlp, "-f", audio_fmt, "-x", "--newline", "-o", output_tpl]
            if audio_ext:
                # Only re-encode when the user asked for it: converting to mp3
                # by default threw away quality the source already had.
                cmd += ["--audio-format", audio_ext]
        else:
            cmd = [self._ytdlp, "-f", fmt,
                   "--merge-output-format", "mp4",
                   "--newline", "--no-warnings", "-o", output_tpl]

        # Tell yt-dlp where ffmpeg lives so it can merge video+audio streams
        if ffmpeg_dir:
            cmd += ["--ffmpeg-location", ffmpeg_dir]

        if job.metadata.get("subtitles"):
            cmd += ["--write-subs", "--sub-langs", "en"]
        self._append_youtube_args(cmd, job.url, client)
        if self._proxy_manager and self._proxy_manager.enabled:
            cmd += self._proxy_manager.get_ytdlp_args()
        cmd.append(job.url)
        return cmd

    async def _stream_download(self, cmd: list[str], job,
                              progress_callback=None) -> tuple[int, str]:
        """Run *cmd*, streaming progress into *job*.

        Returns ``(returncode, stderr_text)``.
        """
        proc = await asyncio.create_subprocess_exec(
            *cmd, **platform_utils.subprocess_kwargs(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

        # Drain stderr concurrently: an undrained pipe fills its ~64 KiB buffer
        # and deadlocks the child until we call proc.wait() forever.
        async def _drain(stream) -> bytes:
            if stream is None:
                return b""
            return await stream.read()

        stderr_task = asyncio.create_task(_drain(proc.stderr))
        # Publish the process so pause/resume can signal it.
        self._procs[job.id] = proc
        try:
            if proc.stdout:
                async for line in proc.stdout:
                    if self._cancel_events.get(job.id, asyncio.Event()).is_set():
                        # yt-dlp spawns ffmpeg for the merge: kill the tree,
                        # not just the parent, or ffmpeg keeps writing.
                        platform_utils.kill_process_tree(proc)
                        await proc.wait()
                        raise asyncio.CancelledError()
                    text = line.decode(errors="replace").strip()
                    self._handle_progress_line(job, text, progress_callback)
            returncode = await proc.wait()
            stderr_out = (await stderr_task).decode(errors="replace")
        except BaseException:
            stderr_task.cancel()
            raise
        finally:
            if self._procs.get(job.id) is proc:
                self._procs.pop(job.id, None)
        return returncode, stderr_out

    @staticmethod
    def _handle_progress_line(job, text: str, progress_callback=None) -> None:
        """Map one yt-dlp stdout line onto the job's progress fields."""
        if "[download]" in text:
            # Try to parse the full progress line with size, speed, ETA
            m = _DL_PROGRESS_RE.search(text)
            if m:
                pct = float(m.group(1))
                total_bytes = _parse_size_bytes(float(m.group(2)), m.group(3))
                speed = _parse_speed_bps(float(m.group(4)), m.group(5))
                _parse_eta_seconds(m.group(6) or "")  # ETA kept for parity

                job.file_size = total_bytes
                job.update_speed(speed)
                # Set downloaded_bytes directly from percentage + total
                job.downloaded_bytes = int(pct / 100.0 * total_bytes)

                if progress_callback:
                    progress_callback(job)
            elif "%" in text:
                # Fallback: at least parse the percentage
                for part in text.split():
                    if part.endswith("%"):
                        try:
                            job.progress_percent = float(part.rstrip("%"))
                        except ValueError:
                            pass
                        break
                if progress_callback:
                    progress_callback(job)
        elif "[Merger]" in text or "[ExtractAudio]" in text:
            job.state = DownloadState.MERGING
            if progress_callback:
                progress_callback(job)

    @staticmethod
    def _find_downloaded_file(output_dir: Path, before: set[str]) -> str:
        """Return the newest real file produced in *output_dir*.

        Files that already existed before the run and ``.part`` / ``.ytdl``
        leftovers are ignored, so a stale file can't be reported as the result.
        """
        def _real(path: Path) -> bool:
            return path.is_file() and not path.name.endswith(_PARTIAL_SUFFIXES)

        produced = [p for p in output_dir.glob("*") if _real(p) and p.name not in before]
        if not produced:
            produced = [p for p in output_dir.glob("*") if _real(p)]
        if not produced:
            return ""
        return str(max(produced, key=os.path.getmtime))

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

    # ── pause / resume ──────────────────────────────────────────

    async def pause(self, job) -> None:
        """Suspend the running yt-dlp process for *job*.

        Without this the base class' no-op made the UI claim PAUSED while the
        download kept running — the file just kept growing.
        """
        proc = self._procs.get(job.id)
        if proc is None or proc.returncode is not None:
            logger.warning("pause: no active yt-dlp process for job %s", job.id)
            return
        if self._suspend_process(proc.pid):
            logger.info("Paused yt-dlp (pid %s) for job %s", proc.pid, job.id)
        else:
            logger.warning("pause: could not suspend yt-dlp (pid %s)", proc.pid)

    async def resume(self, job) -> None:
        """Resume a suspended yt-dlp process for *job*."""
        proc = self._procs.get(job.id)
        if proc is None or proc.returncode is not None:
            logger.warning("resume: no active yt-dlp process for job %s", job.id)
            return
        if self._resume_process(proc.pid):
            logger.info("Resumed yt-dlp (pid %s) for job %s", proc.pid, job.id)
        else:
            logger.warning("resume: could not resume yt-dlp (pid %s)", proc.pid)

    @staticmethod
    def _suspend_process(pid: int) -> bool:
        """Freeze a process (SIGSTOP, or NtSuspendProcess on Windows)."""
        return platform_utils.suspend_process(pid)

    @staticmethod
    def _resume_process(pid: int) -> bool:
        """Thaw a process frozen by :meth:`_suspend_process`."""
        return platform_utils.resume_process(pid)


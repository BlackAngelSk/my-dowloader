"""Torrent Downloader — aria2c-based with libtorrent fallback."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from omnidownloader.core import platform_utils
from omnidownloader.core.base_module import BaseDownloaderModule
from omnidownloader.core.torrent_meta import (
    BencodeError, find_saved_metadata, read_metadata,
)
from omnidownloader.core.disk_utils import ensure_directory
from omnidownloader.core.models import DownloadJob, DownloadState

logger = logging.getLogger(__name__)

try:
    import libtorrent as lt
    HAS_LIBTORRENT = True
except ImportError:
    lt = None  # type: ignore[assignment]
    HAS_LIBTORRENT = False

HAS_ARIA2C = bool(platform_utils.find_aria2c())
if not HAS_LIBTORRENT and not HAS_ARIA2C:
    logger.warning("Neither libtorrent nor aria2c found — torrent downloads unavailable")


def _size_to_bytes(text: str) -> int:
    """Parse aria2c's size tokens ("756MiB", "1.2GiB", "512KiB", "0B")."""
    text = text.strip()
    units = {
        "B": 1, "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3, "TIB": 1024 ** 4,
        "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4,
    }
    for suffix, factor in sorted(units.items(), key=lambda kv: -len(kv[0])):
        if text.upper().endswith(suffix):
            number = text[:len(text) - len(suffix)]
            try:
                return int(float(number) * factor)
            except ValueError:
                return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


class TorrentDownloader(BaseDownloaderModule):
    MODULE_NAME = "torrent"

    def __init__(self, save_path="", max_upload_rate=0, max_download_rate=0,
                 listen_port=6881, proxy_manager=None, bandwidth_manager=None):
        self._save_path = save_path or str(Path.home() / "Downloads" / "OmniDownloader")
        self._session: Optional[Any] = None
        self._max_upload = max_upload_rate
        self._max_download = max_download_rate
        self._listen_port = listen_port
        self._handles: dict[str, Any] = {}
        self._cancel_flags: dict[str, bool] = {}
        self._proxy_manager = proxy_manager
        self._bw = bandwidth_manager
        # Resolved once: Windows needs aria2c.exe, which PATH lookup handles.
        self._aria2c = platform_utils.find_aria2c()
        #: Per-job aria2c phase: "metadata" or "transfer" (see the parser).
        self._phases: dict[str, str] = {}

    def can_handle(self, url: str) -> bool:
        if not HAS_LIBTORRENT and not HAS_ARIA2C:
            return False
        if url.startswith("magnet:"):
            return True
        if urlparse(url).path.lower().endswith(".torrent"):
            return True
        return False

    async def extract_metadata(self, url):
        if HAS_LIBTORRENT:
            return await self._meta_lt(url)
        if url.startswith("magnet:"):
            # No libtorrent: let aria2c fetch the metadata, then read the
            # .torrent it saves.  Without this the UI showed "Torrent /
            # unknown size / no files" for every magnet link.
            meta = await self._meta_aria2(url)
            if meta is not None:
                return meta.as_dict()
        else:
            # A .torrent path or URL can be read directly — no peer contact.
            meta = self._meta_from_file(url)
            if meta is not None:
                return meta.as_dict()
        return {"name": "Torrent", "total_size": -1, "files": [], "thumbnail": ""}

    def _meta_from_file(self, url: str):
        """Read a local .torrent (or a URL we already downloaded) directly."""
        candidate = Path(url.replace("file://", "")) if url else None
        if candidate is None or not candidate.is_file():
            return None
        try:
            return read_metadata(candidate)
        except (OSError, BencodeError) as exc:
            logger.warning("Could not read torrent metadata from %s: %s", candidate, exc)
            return None

    async def _meta_aria2(self, url: str, timeout: int = 90):
        """Resolve a magnet's metadata with aria2c (no libtorrent needed)."""
        if not self._aria2c:
            return None
        work_dir = Path(self._save_path) / ".metadata"
        work_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            self._aria2c,
            "--dir", str(work_dir),
            "--bt-metadata-only=true",
            "--bt-save-metadata=true",
            f"--bt-stop-timeout={timeout}",
            "--enable-dht=true",
            "--dht-entry-point=router.bittorrent.com:6881",
            "--dht-entry-point=router.utorrent.com:6881",
            "--console-log-level=warn",
            "--enable-color=false",
            "--summary-interval=0",
            url,
        ]
        logger.info("Resolving magnet metadata with aria2c (max %ss)", timeout)
        proc = await asyncio.create_subprocess_exec(
            *cmd, **platform_utils.subprocess_kwargs(),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 15)
        except asyncio.TimeoutError:
            platform_utils.kill_process_tree(proc)
            logger.warning("Magnet metadata resolution timed out")
            return None

        saved = find_saved_metadata(work_dir)
        if saved is None:
            detail = (stderr or b"").decode(errors="replace").strip().splitlines()
            logger.warning(
                "aria2c could not resolve the magnet metadata%s",
                f": {detail[-1][:200]}" if detail else "",
            )
            return None
        try:
            meta = read_metadata(saved)
        except (OSError, BencodeError) as exc:
            logger.warning("Saved magnet metadata was unreadable: %s", exc)
            return None
        logger.info(
            "Magnet resolved: %s (%d files, %.1f MiB)",
            meta.name, len(meta.files), meta.total_size / 1024 ** 2,
        )
        return meta

    async def _meta_lt(self, url):
        assert lt is not None  # guarded by HAS_LIBTORRENT
        session = self._get_lt()
        if url.startswith("magnet:"):
            handle = lt.add_magnet_uri(session, url, {"save_path": self._save_path})
            await asyncio.sleep(5)
        else:
            info = lt.torrent_info(url)
            handle = session.add_torrent({"ti": info, "save_path": self._save_path})
            await asyncio.sleep(1)
        tf = handle.torrent_file()
        files = []
        if tf:
            for i in range(tf.num_files()):
                fi = tf.file_at(i)
                files.append({"index": i, "path": fi.path, "size": fi.size})
        session.remove_torrent(handle)
        return {"name": tf.name() if tf else "Unknown",
                "total_size": tf.total_length() if tf else 0,
                "num_files": len(files), "files": files, "thumbnail": ""}

    async def start_download(self, job, progress_callback=None):
        ensure_directory(self._save_path)
        if HAS_ARIA2C:
            await self._dl_aria2(job, progress_callback)
        elif HAS_LIBTORRENT:
            await self._dl_lt(job, progress_callback)
        else:
            job.state = DownloadState.FAILED
            job.error_message = (
                "Torrent support needs aria2c (" + platform_utils.install_hint("aria2c")
                + ") or the python libtorrent bindings ("
                + platform_utils.install_hint("libtorrent") + ")."
            )
            return

    async def _dl_aria2(self, job, progress_callback):
        job.state = DownloadState.DOWNLOADING
        before = self._snapshot_dir(Path(self._save_path))
        cmd = [self._aria2c or "aria2c", "--dir", self._save_path, "--seed-time=0",
               "--bt-stop-timeout=300", "--summary-interval=1",
               # notice (not warn): aria2c prints its progress summary AND the
               # "[METADATA]name" line at notice level — hiding them left the
               # card at a frozen 0% for the whole download.
               "--enable-color=false", "--console-log-level=notice",
               "--continue=true",
               # Keep the metadata aria2c fetches for a magnet: it is the only
               # way to know the name/size without libtorrent, and it lets the
               # job card show something other than "Torrent / unknown".
               "--bt-save-metadata=true",
               # DHT entry points: a bare magnet (xt+dn, no trackers) otherwise
               # has no way to find peers at all ("No DHT entry point").
               "--enable-dht=true",
               "--dht-entry-point=router.bittorrent.com:6881",
               "--dht-entry-point=router.utorrent.com:6881",
               "--bt-enable-lpd=true"]
        if self._max_download > 0:
            # ``max_download_rate`` is a KiB/s budget (as the CLI flag expects).
            cmd += ["--max-overall-download-limit", f"{self._max_download}K"]
        if self._proxy_manager and self._proxy_manager.enabled:
            proxy = self._proxy_manager.get_proxy_url()
            if proxy:
                cmd += ["--all-proxy", proxy]
        cmd.append(job.url)
        logger.info("Starting aria2c torrent download")

        proc = await asyncio.create_subprocess_exec(
            *cmd, **platform_utils.subprocess_kwargs(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

        # Drain stderr: aria2c writes there freely, and once the 64 KiB pipe
        # buffer fills it blocks forever — stdout never closes and the
        # download hangs with no error.
        tracker_errors: list[str] = []

        def _collect_errors(text: str) -> None:
            """aria2c reports tracker rejections on stdout *and* stderr."""
            low = text.lower()
            if "failure reason" in low or "not authorized" in low:
                tracker_errors.append(text.strip()[:200])

        async def _drain(stream) -> None:
            if stream is None:
                return
            while True:
                line = await stream.read(4096)
                if not line:
                    return
                _collect_errors(line.decode(errors="replace"))

        drain_task = asyncio.create_task(_drain(proc.stderr))
        # aria2c writes the .torrent it resolves a magnet into its directory;
        # picking it up while the transfer runs gives the card a real name and
        # size within seconds instead of "Torrent / unknown" until the end.
        meta_task = asyncio.create_task(self._watch_metadata(job, progress_callback))
        try:
            if proc.stdout:
                async for line in proc.stdout:
                    if self._cancel_flags.get(job.id, False):
                        # Kill the tree: aria2c spawns helper processes.
                        platform_utils.kill_process_tree(proc)
                        await proc.wait()
                        raise asyncio.CancelledError()
                    text = line.decode(errors="replace").strip()
                    _collect_errors(text)
                    self._parse_torrent_progress(job, text, progress_callback)
                    if text.startswith("[METADATA]"):
                        # aria2c saved the .torrent it just fetched — use it for
                        # the real size and file list while the download runs.
                        self._apply_saved_metadata(job)
            await proc.wait()
        finally:
            drain_task.cancel()
            meta_task.cancel()
            self._phases.pop(job.id, None)
            self._cancel_flags.pop(job.id, None)

        # Fill in what the download actually produced: nothing set these for
        # the aria2c backend, so the card, history and "open folder" showed
        # nothing even after a successful download.
        self._describe_result(job, before)

        if proc.returncode not in (0, None):
            detail = tracker_errors[-1] if tracker_errors else ""
            raise RuntimeError(
                f"aria2c exited with code {proc.returncode}"
                + (f" — {detail}" if detail else "")
            )
        if tracker_errors and not job.progress_percent:
            # aria2c exited 0 but never got data: surface the tracker's own
            # words instead of a silently empty download.
            raise RuntimeError(f"torrent could not start — {tracker_errors[-1]}")

    def _parse_torrent_progress(self, job, text: str, progress_callback) -> None:
        """Map one aria2c output line onto the job.

        aria2c goes through phases for a magnet and says so in its ``FILE:``
        line::

            FILE: [MEMORY][METADATA]debian-13.7.0-amd64-netinst.iso

        During that phase the summary reads ``59KiB/59KiB(100%)`` — the size of
        the *.torrent* being fetched, not the payload.  Taking that as the
        download size made a magnet look instantly "100% complete / 59 KiB", and
        the real name and size never appeared.
        """
        phase = self._phases.get(job.id, "unknown")

        if text.startswith("FILE:"):
            target = text[len("FILE:"):].strip()
            if "[METADATA]" in target:
                self._phases[job.id] = "metadata"
                name = target.split("[METADATA]", 1)[1].strip()
                if name:
                    job.file_name = name
                job.state = DownloadState.EXTRACTING
                if progress_callback:
                    progress_callback(job)
            else:
                self._phases[job.id] = "transfer"
                if target:
                    job.file_name = Path(target).name or job.file_name
                if progress_callback:
                    progress_callback(job)
            return

        if text.startswith("[METADATA]"):
            job.file_name = text[len("[METADATA]"):].strip() or job.file_name
            job.state = DownloadState.EXTRACTING
            if progress_callback:
                progress_callback(job)
            return

        if "(" in text and "%" in text:
            try:
                percent = float(text.split("(")[1].split("%")[0])
            except (IndexError, ValueError):
                return
            if phase == "metadata":
                # Not payload progress — ignore it entirely.
                return
            # Size first: Job.progress_percent is *derived* from
            # downloaded_bytes / file_size, so feeding it a percentage before
            # the size is known silently does nothing (progress stuck at 0%).
            for token in text.split():
                if "/" in token:
                    # "756MiB/756MiB(50%)" — the percentage rides on the
                    # denominator, and _size_to_bytes would choke on it.
                    total = _size_to_bytes(token.split("/")[1].split("(")[0])
                    if total:
                        job.file_size = total
                elif token.startswith("DL:"):
                    speed = _size_to_bytes(token.split(":", 1)[1])
                    if speed:
                        job.update_speed(float(speed))
            if job.file_size > 0:
                job.downloaded_bytes = int(job.file_size * percent / 100)
            if progress_callback:
                progress_callback(job)
        elif job.state != DownloadState.DOWNLOADING:
            job.state = DownloadState.DOWNLOADING
            if progress_callback:
                progress_callback(job)

    @staticmethod
    def _snapshot_dir(path: Path) -> set[str]:
        try:
            return {p.name for p in path.iterdir()}
        except OSError:
            return set()

    async def _watch_metadata(self, job, progress_callback=None,
                              attempts: int = 60, interval: float = 2.0) -> None:
        """Pick up the .torrent aria2c saves once a magnet resolves."""
        if not job.url.startswith("magnet:"):
            return
        for _ in range(attempts):
            await asyncio.sleep(interval)
            if self._apply_saved_metadata(job):
                logger.info(
                    "Magnet resolved to %s (%.1f MiB)",
                    job.file_name, (job.file_size or 0) / 1024 ** 2,
                )
                if progress_callback:
                    progress_callback(job)
                return

    def _apply_saved_metadata(self, job) -> bool:
        """Fill name/size (and the file list) from aria2c's saved .torrent."""
        saved = find_saved_metadata(self._save_path)
        if saved is None:
            return False
        try:
            meta = read_metadata(saved)
        except (OSError, BencodeError) as exc:
            logger.debug("Saved torrent metadata unreadable: %s", exc)
            return False
        if meta.name:
            job.file_name = meta.name
        if meta.total_size:
            job.file_size = meta.total_size
        job.metadata["torrent_files"] = [
            {"path": f.path, "length": f.length} for f in meta.files
        ]
        return True

    def _describe_result(self, job, before: set[str]) -> None:
        """Point the job at what aria2c produced (file or directory)."""
        save_path = Path(self._save_path)
        self._apply_saved_metadata(job)

        new_entries = [
            p for p in save_path.iterdir()
            if p.name not in before and not p.name.endswith(".aria2")
            and not p.name.endswith(".torrent")
        ]
        if not new_entries:
            return
        # A single-file torrent lands as one file; a multi-file one as a
        # directory named after the torrent.
        target = max(new_entries, key=lambda p: (p.is_dir(), p.stat().st_size))
        job.file_path = str(target)
        if not job.file_name:
            job.file_name = target.name
        if target.is_file() and not job.file_size:
            job.file_size = target.stat().st_size

    async def _dl_lt(self, job, progress_callback):
        assert lt is not None  # guarded by HAS_LIBTORRENT
        session = self._get_lt()
        job.state = DownloadState.DOWNLOADING
        self._cancel_flags[job.id] = False

        if job.url.startswith("magnet:"):
            handle = lt.add_magnet_uri(session, job.url, {"save_path": self._save_path})
            await asyncio.sleep(5)
        else:
            info = lt.torrent_info(job.url)
            handle = session.add_torrent({"ti": info, "save_path": self._save_path})
        tf = handle.torrent_file()
        job.file_name = tf.name() if tf else "Unknown"
        job.file_size = tf.total_length() if tf else 0
        job.file_path = str(Path(self._save_path) / job.file_name)
        self._handles[job.id] = handle

        # Sequential download mode
        if job.sequential:
            # OR the flag in — set_flags(x) replaces every flag on the handle.
            handle.set_flags(handle.flags() | lt.sequential_download)
            logger.info("Torrent sequential mode enabled")

        # Create streaming buffer for in-progress file
        from omnidownloader.core.streaming_buffer import StreamingBuffer
        job.streaming_buffer = StreamingBuffer(job.file_path, job.file_size)

        while not self._cancel_flags.get(job.id, False):
            status = handle.status()
            job.downloaded_bytes = status.total_done
            job.update_speed(status.download_rate)
            # Update streaming buffer
            if job.streaming_buffer:
                job.streaming_buffer.update_progress(job.downloaded_bytes)
            # Bandwidth throttle
            if self._bw and status.download_rate > 0:
                await self._bw.throttle(job.id, max(1, int(status.download_rate * 0.5)))
            if progress_callback:
                progress_callback(job)
            if handle.is_seed():
                if job.streaming_buffer:
                    job.streaming_buffer.mark_complete()
                break
            await asyncio.sleep(0.5)

        session.remove_torrent(handle)
        self._handles.pop(job.id, None)
        self._cancel_flags.pop(job.id, None)

    def _get_lt(self) -> Any:
        if self._session is None:
            assert lt is not None  # guarded by HAS_LIBTORRENT
            settings = {"listen_interfaces": f"0.0.0.0:{self._listen_port}"}
            proxy_cfg = {}
            if self._proxy_manager and self._proxy_manager.enabled:
                # Route libtorrent through the proxy and disable the discovery
                # mechanisms that would otherwise announce the real IP.
                proxy_cfg = self._proxy_manager.get_libtorrent_proxy_settings()
                settings.update({"enable_dht": False, "enable_lsd": False,
                                 "enable_natpmp": False, "enable_upnp": False})
            else:
                settings.update({"enable_dht": True, "enable_lsd": True,
                                 "enable_natpmp": True, "enable_upnp": True})
            settings.update(proxy_cfg)
            self._session = lt.session(settings)
            if not proxy_cfg:
                self._session.add_dht_router("router.bittorrent.com", 6881)
                self._session.add_dht_router("dht.transmissionbt.com", 6881)
                self._session.start_dht()
                self._session.start_lsd()
                self._session.start_upnp()
                self._session.start_natpmp()
        return self._session

    async def cancel(self, job):
        # Set the flag and leave it: _dl_lt/_dl_aria2 poll it and clear it
        # themselves.  Popping it here meant the loop kept seeing False and
        # the torrent ran on forever.
        self._cancel_flags[job.id] = True
        handle = self._handles.get(job.id)
        if handle and self._session:
            try:
                self._session.remove_torrent(handle)
            except Exception as exc:  # noqa: BLE001
                logger.debug("remove_torrent failed for %s: %s", job.id, exc)

    async def close(self) -> None:
        """Release the libtorrent session."""
        self._handles.clear()
        self._cancel_flags.clear()
        self._session = None

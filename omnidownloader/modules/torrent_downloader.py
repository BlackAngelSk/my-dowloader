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
        return {"name": "Torrent", "total_size": -1, "files": [], "thumbnail": ""}

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
        cmd = [self._aria2c or "aria2c", "--dir", self._save_path, "--seed-time=0",
               "--bt-stop-timeout=300", "--summary-interval=1",
               "--enable-color=false", "--console-log-level=notice",
               "--continue=true"]
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
        async def _drain(stream) -> None:
            if stream is None:
                return
            while await stream.read(4096):
                pass

        drain_task = asyncio.create_task(_drain(proc.stderr))
        try:
            if proc.stdout:
                async for line in proc.stdout:
                    if self._cancel_flags.get(job.id, False):
                        # Kill the tree: aria2c spawns helper processes.
                        platform_utils.kill_process_tree(proc)
                        await proc.wait()
                        raise asyncio.CancelledError()
                    text = line.decode(errors="replace").strip()
                    if "(" in text and "%" in text:
                        try:
                            pct = float(text.split("(")[1].split("%")[0])
                            job.progress_percent = pct
                        except (IndexError, ValueError):
                            pass
                        if progress_callback:
                            progress_callback(job)
            await proc.wait()
        finally:
            drain_task.cancel()
            self._cancel_flags.pop(job.id, None)
        if proc.returncode not in (0, None):
            raise RuntimeError(f"aria2c exited with code {proc.returncode}")

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

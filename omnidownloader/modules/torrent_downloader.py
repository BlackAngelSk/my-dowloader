"""Torrent Downloader — aria2c-based with libtorrent fallback."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from omnidownloader.core.base_module import BaseDownloaderModule
from omnidownloader.core.disk_utils import ensure_directory
from omnidownloader.core.models import DownloadJob, DownloadState

logger = logging.getLogger(__name__)

try:
    import libtorrent as lt
    HAS_LIBTORRENT = True
except ImportError:
    HAS_LIBTORRENT = False

HAS_ARIA2C = shutil.which("aria2c") is not None
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

    def can_handle(self, url: str) -> bool:
        if not HAS_LIBTORRENT and not HAS_ARIA2C:
            return False
        if url.startswith("magnet:"):
            return True

    async def extract_metadata(self, url):
        if HAS_LIBTORRENT:
            return await self._meta_lt(url)
        return {"name": "Torrent", "total_size": -1, "files": [], "thumbnail": ""}

    async def _meta_lt(self, url):
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
            job.error_message = "Install aria2 (pacman -S aria2) for torrent support"
            return

    async def _dl_aria2(self, job, progress_callback):
        job.state = DownloadState.DOWNLOADING
        cmd = ["aria2c", "--dir", self._save_path, "--seed-time=0",
               "--bt-stop-timeout=300", "--summary-interval=1",
               "--enable-color=false", "--console-log-level=notice",
               "--continue=true"]
        if self._max_download > 0:
            cmd += ["--max-overall-download-limit", f"{self._max_download}K"]
        if self._proxy_manager and self._proxy_manager.enabled:
            proxy = self._proxy_manager.get_proxy_url()
            if proxy:
                cmd += ["--all-proxy", proxy]
        cmd.append(job.url)
        logger.info("Starting aria2c torrent download")

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

        if proc.stdout:
            async for line in proc.stdout:
                if self._cancel_flags.get(job.id, False):
                    proc.terminate()
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

    async def _dl_lt(self, job, progress_callback):
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

        # Sequential download mode: prioritize pieces in order
        if job.sequential:
            handle.set_flags(lt.sequential_download)
            num_pieces = tf.num_pieces() if tf else 0
            # Set piece priorities: all pieces to normal priority
            for i in range(num_pieces):
                handle.piece_priority(i, 4)  # 4 = top_priority
            logger.info("Torrent sequential mode enabled for %d pieces", num_pieces)

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

    def _get_lt(self):
        if self._session is None:
            settings = {"listen_interfaces": f"0.0.0.0:{self._listen_port}",
                        "enable_dht": True, "enable_lsd": True,
                        "enable_natpmp": True, "enable_upnp": True}
            self._session = lt.session(settings)
            self._session.add_dht_router("router.bittorrent.com", 6881)
            self._session.add_dht_router("dht.transmissionbt.com", 6881)
            self._session.start_dht()
            self._session.start_lsd()
            self._session.start_upnp()
            self._session.start_natpmp()
        return self._session

    async def cancel(self, job):
        self._cancel_flags[job.id] = True
        handle = self._handles.pop(job.id, None)
        if handle and self._session:
            self._session.remove_torrent(handle)

        self._cancel_flags.pop(job.id, None)
        if proc.returncode != 0 and proc.returncode != 137:
            stderr = (await proc.stderr.read()).decode(errors="replace")[:500] if proc.stderr else ""
            if "cancelled" not in stderr.lower():
                raise RuntimeError(f"aria2c failed: {stderr}")
        candidates = sorted(Path(self._save_path).glob("*"), key=os.path.getmtime, reverse=True)
        if candidates:
            job.file_path = str(candidates[0])
            job.file_name = candidates[0].name

            job.state = DownloadState.FAILED
            job.error_message = "Install aria2 (pacman -S aria2) for torrent support"

        return urlparse(url).path.endswith(".torrent")

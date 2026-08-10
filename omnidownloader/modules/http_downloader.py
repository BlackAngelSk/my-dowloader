"""Multi-threaded HTTP/HTTPS/FTP chunked downloader."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse

import aiohttp

from omnidownloader.core.base_module import BaseDownloaderModule
from omnidownloader.core.disk_utils import ensure_directory, get_available_space, preallocate_file
from omnidownloader.core.models import DownloadJob, DownloadState, SegmentProgress
from omnidownloader.core.ram_buffer import DynamicBufferSizer, RAMRingBuffer

logger = logging.getLogger(__name__)
SUPPORTED_SCHEMES = ("http", "https", "ftp")


class HTTPDownloader(BaseDownloaderModule):
    MODULE_NAME = "http"

    def __init__(self, proxy_manager=None, bandwidth_manager=None):
        self._session = None
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._pause_events: dict[str, asyncio.Event] = {}
        self._proxy_manager = proxy_manager
        self._bw = bandwidth_manager

    def can_handle(self, url):
        try:
            scheme = urlparse(url).scheme.lower()
            if scheme not in SUPPORTED_SCHEMES:
                return False
            # Skip URLs handled by specialized modules (YouTube, Twitter, etc.)
            host = (urlparse(url).hostname or "").removeprefix("www.")
            media_hosts = {
                "youtube.com", "youtu.be", "m.youtube.com",
                "twitter.com", "x.com", "nitter.net",
                "tiktok.com", "vm.tiktok.com",
                "instagram.com", "facebook.com", "fb.watch",
                "reddit.com", "v.redd.it", "twitch.tv",
                "vimeo.com", "soundcloud.com",
            }
            if host in media_hosts:
                return False
            return True
        except Exception:
            return False

    async def start_download(self, job, progress_callback=None):
        session = await self._get_session()
        if job.file_size <= 0:
            meta = await self.extract_metadata(job.url)
            job.file_size = meta.get("file_size", -1)
            if not job.file_name:
                job.file_name = meta.get("title", "download")
            accept_ranges = meta.get("accept_ranges", "none")
        else:
            accept_ranges = "bytes"
        if not job.file_name:
            job.file_name = self._extract_filename(job.url, "")
        if not job.file_path:
            job.file_path = str(Path.home() / "Downloads" / "OmniDownloader" / job.file_name)
        ensure_directory(job.file_path)

        use_range = accept_ranges.lower() == "bytes" and job.file_size > 0
        job.thread_count = DynamicBufferSizer.calculate_threads(job.file_size) if use_range else 1

        if job.file_size > 0:
            avail = get_available_space(job.file_path)
            if avail < job.file_size:
                job.state = DownloadState.FAILED
                job.error_message = "Not enough disk space."
                return
            fd = preallocate_file(job.file_path, job.file_size)
            os.close(fd)

        buf_size = DynamicBufferSizer.calculate_buffer_size(job.file_size)
        fd = os.open(job.file_path, os.O_WRONLY | os.O_CREAT, 0o644)
        ring = RAMRingBuffer(fd, buffer_size=buf_size)
        segments = (self._build_segments(job.file_size, job.thread_count)
                    if use_range and job.thread_count > 1
                    else [SegmentProgress(0, 0, -1)])

        # Sequential mode: force segment 0 (file header) to be first
        if job.sequential and len(segments) > 1:
            segments.sort(key=lambda s: s.segment_index)

        job.segments = segments
        job.state = DownloadState.DOWNLOADING
        job.started_at = time.monotonic()

        # Create streaming buffer for in-app preview
        from omnidownloader.core.streaming_buffer import StreamingBuffer
        stream_buf = StreamingBuffer(job.file_path, job.file_size, ring)
        job.streaming_buffer = stream_buf
        self._cancel_events[job.id] = asyncio.Event()
        self._pause_events[job.id] = asyncio.Event()
        self._pause_events[job.id].set()
        try:
            await self._download_all(job, session, segments, ring, progress_callback)
            await ring.flush()
            if job.streaming_buffer:
                job.streaming_buffer.mark_complete()
        finally:
            os.close(fd)
            ring.close()
            self._cancel_events.pop(job.id, None)
            self._pause_events.pop(job.id, None)

    @staticmethod
    def _build_segments(file_size, thread_count):
        segs = []
        cs = file_size // thread_count
        for i in range(thread_count):
            s = i * cs
            e = (i + 1) * cs - 1 if i < thread_count - 1 else file_size - 1
            segs.append(SegmentProgress(i, s, e))
        return segs

    async def _download_all(self, job, session, segments, ring, cb):
        tasks = []
        for seg in segments:
            if seg.end_byte < 0:
                tasks.append(asyncio.create_task(self._stream(job, session, ring, cb)))
            else:
                tasks.append(asyncio.create_task(self._seg(job, session, seg, ring, cb)))
        await asyncio.gather(*tasks)

    async def _seg(self, job, session, seg, ring, cb):
        headers = {"Range": f"bytes={seg.start_byte}-{seg.end_byte}"}
        samples = []
        async with session.get(job.url, headers=headers) as resp:
            if resp.status not in (200, 206):
                raise aiohttp.ClientError(f"HTTP {resp.status}")
            async for chunk in resp.content.iter_chunked(256 * 1024):
                if self._cancel_events.get(job.id, asyncio.Event()).is_set():
                    raise asyncio.CancelledError()
                pe = self._pause_events.get(job.id)
                if pe and not pe.is_set():
                    await pe.wait()
                await ring.write(chunk)
                seg.downloaded_bytes += len(chunk)
                job.mark_downloaded(len(chunk))
                # Update streaming buffer progress
                if job.streaming_buffer:
                    job.streaming_buffer.update_progress(job.downloaded_bytes)
                # Bandwidth throttle
                if self._bw:
                    await self._bw.throttle(job.id, len(chunk))
                now = time.monotonic()
                samples.append((now, len(chunk)))
                if len(samples) > 20:
                    samples.pop(0)
                if samples:
                    dt = samples[-1][0] - samples[0][0]
                    db = sum(s[1] for s in samples)
                    seg.speed_bps = db / max(dt, 0.001)
                    job.update_speed(sum(s.speed_bps for s in job.segments) / max(1, len(job.segments)))
                if cb:
                    cb(job)
        seg.completed = True

    async def _stream(self, job, session, ring, cb):
        samples = []
        parsed_url = urlparse(job.url)
        referer = f"{parsed_url.scheme}://{parsed_url.netloc}/"
        headers = {"Referer": referer}

        for attempt in range(3):
            async with session.get(job.url, headers=headers) as resp:
                if resp.status == 403 and attempt < 2:
                    logger.warning("403 on attempt %d for %s, retrying...", attempt + 1, job.url)
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                if resp.status not in (200, 206):
                    raise aiohttp.ClientError(
                        f"HTTP {resp.status} — {job.url}\n"
                        f"The server rejected the request. The site may require authentication or block automated downloads."
                    )
                cl = resp.headers.get("Content-Length")
                if cl:
                    job.file_size = int(cl)
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    if self._cancel_events.get(job.id, asyncio.Event()).is_set():
                        raise asyncio.CancelledError()
                    pe = self._pause_events.get(job.id)
                    if pe and not pe.is_set():
                        await pe.wait()
                    await ring.write(chunk)
                    job.mark_downloaded(len(chunk))
                    # Update streaming buffer progress
                    if job.streaming_buffer:
                        job.streaming_buffer.update_progress(job.downloaded_bytes)
                    # Bandwidth throttle
                    if self._bw:
                        await self._bw.throttle(job.id, len(chunk))
                    now = time.monotonic()
                    samples.append((now, len(chunk)))
                    if len(samples) > 20:
                        samples.pop(0)
                    if samples:
                        dt = samples[-1][0] - samples[0][0]
                        db = sum(s[1] for s in samples)
                        job.update_speed(db / max(dt, 0.001))
                    if cb:
                        cb(job)
                break  # success — exit retry loop

    async def pause(self, job):
        e = self._pause_events.get(job.id)
        if e:
            e.clear()

    async def resume(self, job):
        e = self._pause_events.get(job.id)
        if e:
            e.set()

    async def cancel(self, job):
        e = self._cancel_events.get(job.id)
        if e:
            e.set()

    async def _get_session(self):
        if self._session is None or self._session.closed:
            connector = None
            if self._proxy_manager and self._proxy_manager.enabled:
                connector = self._proxy_manager.get_aiohttp_connector()
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            }
            if connector:
                self._session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=aiohttp.ClientTimeout(total=30),
                    headers=headers,
                )
            else:
                self._session = aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(limit=32),
                    timeout=aiohttp.ClientTimeout(total=30),
                    headers=headers,
                )
        return self._session

    async def extract_metadata(self, url):
        session = await self._get_session()
        async with session.head(url, allow_redirects=True) as resp:
            h = resp.headers
            return {
                "title": self._extract_filename(url, h.get("Content-Disposition", "")),
                "file_size": int(h.get("Content-Length", -1)),
                "content_type": h.get("Content-Type", "application/octet-stream"),
                "accept_ranges": h.get("Accept-Ranges", "none"),
                "url": str(resp.url),
            }

    @staticmethod
    def _extract_filename(url, content_disp):
        if "filename=" in content_disp:
            return unquote(content_disp.split("filename=")[-1].strip('" '))
        name = unquote(urlparse(url).path.split("/")[-1])
        return name if name else "download"


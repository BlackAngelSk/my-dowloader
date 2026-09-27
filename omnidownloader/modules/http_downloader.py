"""Multi-threaded HTTP/HTTPS/FTP chunked downloader."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import aiohttp

from omnidownloader.core.base_module import BaseDownloaderModule
from omnidownloader.core.disk_utils import (
    cleanup_partial_file,
    ensure_directory,
    get_available_space,
    preallocate_file,
    sidecar_path,
    truncate_file,
    verify_checksum,
)
from omnidownloader.core.models import DownloadJob, DownloadState, SegmentProgress
from omnidownloader.core.ram_buffer import DynamicBufferSizer, RAMRingBuffer
from omnidownloader.core.streaming_buffer import StreamingBuffer

logger = logging.getLogger(__name__)
#: aiohttp has no FTP client — advertising it produced jobs that could only
#: fail with "Unsupported scheme".
SUPPORTED_SCHEMES = ("http", "https")

#: Read granularity per connection.
CHUNK_SIZE = 256 * 1024

#: Minimum seconds between resume-state writes (one write per chunk would
#: dominate the download on fast links).
RESUME_SAVE_INTERVAL = 3.0

#: Hosts handled by the media (yt-dlp) module instead of a raw HTTP fetch.
MEDIA_HOSTS = {
    "youtube.com", "youtu.be", "m.youtube.com",
    "twitter.com", "x.com", "nitter.net",
    "tiktok.com", "vm.tiktok.com",
    "instagram.com", "facebook.com", "fb.watch",
    "reddit.com", "v.redd.it", "twitch.tv", "clips.twitch.tv",
    "vimeo.com", "soundcloud.com", "dailymotion.com",
    "bilibili.com", "bandcamp.com",
    "rutube.ru",
    "kick.com", "ok.ru", "dzen.ru",
    "nicovideo.jp", "nico.ms",
    "odysee.com", "odys.ly",
    "archive.org",
}


class RangeUnsupported(Exception):
    """Raised when a server ignores a Range request and sends the whole file."""


class HTTPDownloader(BaseDownloaderModule):
    MODULE_NAME = "http"

    def __init__(self, proxy_manager=None, bandwidth_manager=None):
        self._session = None
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._pause_events: dict[str, asyncio.Event] = {}
        self._proxy_manager = proxy_manager
        self._bw = bandwidth_manager
        #: Last time resume state was written per job (throttles sidecar I/O).
        self._last_resume_save: dict[str, float] = {}

    def can_handle(self, url):
        try:
            scheme = urlparse(url).scheme.lower()
            if scheme not in SUPPORTED_SCHEMES:
                return False
            # Skip URLs handled by specialized modules (YouTube, Twitter, etc.)
            host = (urlparse(url).hostname or "").removeprefix("www.")
            if host in MEDIA_HOSTS:
                return False
            return True
        except Exception:
            return False

    # ── resume state ────────────────────────────────────────────

    def _load_resume_state(self, job) -> Optional[dict]:
        """Read the sidecar for an interrupted download, if it is compatible."""
        path = sidecar_path(job.file_path)
        try:
            if not path.is_file():
                return None
            state = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring unreadable resume file %s: %s", path, exc)
            return None
        if state.get("url") != job.url:
            logger.info("Resume file belongs to a different URL — starting over")
            return None
        if not Path(job.file_path).is_file():
            return None
        recorded_size = state.get("file_size")
        if (isinstance(recorded_size, int) and recorded_size > 0
                and job.file_size > 0 and recorded_size != job.file_size):
            logger.info("Remote file changed size (%s → %s) — starting over",
                        recorded_size, job.file_size)
            return None
        return state

    def _save_resume_state(self, job, segments, force: bool = False) -> None:
        """Persist per-segment progress so the download can be resumed."""
        now = time.monotonic()
        if not force and (now - self._last_resume_save.get(job.id, 0.0)) < RESUME_SAVE_INTERVAL:
            return
        self._last_resume_save[job.id] = now
        state = {
            "version": 1,
            "url": job.url,
            "file_size": job.file_size,
            "updated": time.time(),
            "segments": [
                {"segment_index": s.segment_index, "start_byte": s.start_byte,
                 "end_byte": s.end_byte, "downloaded_bytes": s.downloaded_bytes}
                for s in segments
            ],
        }
        path = sidecar_path(job.file_path)
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(state))
            tmp.replace(path)
        except OSError as exc:
            logger.debug("Could not write resume state for %s: %s", job.file_path, exc)

    @staticmethod
    def _clear_resume_state(job) -> None:
        sidecar_path(job.file_path).unlink(missing_ok=True)

    def _resume_segments(self, job, segments, state) -> tuple[list, int]:
        """Rewind *segments* to what is already on disk.

        ``SegmentProgress.downloaded_bytes`` means "bytes already durable from
        ``start_byte``", so it accumulates across sessions; the request offset
        is always ``start_byte + downloaded_bytes``.  The layout itself is left
        untouched, and only segments whose recorded layout matches the current
        one are reused, so a changed remote file can't produce a mixed file.
        """
        recorded = {s["segment_index"]: s for s in state.get("segments", [])}
        done_total = 0
        resumed: list[SegmentProgress] = []
        for seg in segments:
            old = recorded.get(seg.segment_index)
            if not old or old.get("start_byte") != seg.start_byte or old.get("end_byte") != seg.end_byte:
                resumed.append(seg)
                continue
            length = seg.end_byte - seg.start_byte + 1
            done = max(0, min(int(old.get("downloaded_bytes") or 0), length))
            seg.downloaded_bytes = done
            done_total += done
            if done >= length:
                seg.completed = True
            resumed.append(seg)
        return resumed, done_total

    @staticmethod
    def _request_offset(seg: SegmentProgress) -> int:
        """Absolute file offset the next byte for *seg* belongs at."""
        return seg.start_byte + seg.downloaded_bytes

    def _sync_segment_progress(self, segments, writers) -> None:
        """Align recorded progress with what the rings actually flushed.

        ``downloaded_bytes`` counts bytes *received*; if a write fails or a
        flush is interrupted the file holds less, and a resume based on the
        optimistic figure would skip data and corrupt the result.
        """
        for seg in segments:
            entry = writers.get(seg.segment_index)
            if not entry:
                continue
            _, ring = entry
            on_disk = max(0, ring.file_position - seg.start_byte)
            if on_disk != seg.downloaded_bytes:
                logger.debug(
                    "Segment %s progress corrected %d → %d bytes (flushed)",
                    seg.segment_index, seg.downloaded_bytes, on_disk,
                )
                seg.downloaded_bytes = on_disk

    # ── job setup ───────────────────────────────────────────────

    async def start_download(self, job, progress_callback=None):
        session = await self._get_session()

        # Always ask the server what it actually supports.  Assuming range
        # support because we happen to know the size produces corrupt files
        # against servers that ignore Range.
        meta: dict = {}
        try:
            meta = await self.extract_metadata(job.url)
        except Exception as exc:  # noqa: BLE001 - probe is best-effort
            logger.warning("Probe failed for %s: %s", job.url, exc)

        reported = meta.get("file_size", -1)
        if isinstance(reported, int) and reported > 0:
            job.file_size = reported
        if not job.file_name:
            job.file_name = meta.get("title") or self._extract_filename(job.url, "")
        if not job.file_path:
            job.file_path = str(
                Path.home() / "Downloads" / "OmniDownloader" / job.file_name
            )
        ensure_directory(job.file_path)

        if job.file_size > 0:
            avail = get_available_space(job.file_path)
            if avail < job.file_size:
                job.state = DownloadState.FAILED
                job.error_message = (
                    f"Not enough disk space: need {job.file_size} bytes, "
                    f"{avail} available."
                )
                return

        accept_ranges = str(meta.get("accept_ranges", "none")).lower()
        use_range = accept_ranges == "bytes" and job.file_size > 0
        job.thread_count = (
            DynamicBufferSizer.calculate_threads(job.file_size) if use_range else 1
        )

        segments = (
            self._build_segments(job.file_size, job.thread_count)
            if use_range and job.thread_count > 1
            else [SegmentProgress(0, 0, -1)]
        )
        if job.sequential and len(segments) > 1:
            segments.sort(key=lambda s: s.segment_index)

        # ── resume an interrupted download ──────────────────────
        resume_state = self._load_resume_state(job)
        already_done = 0
        if resume_state:
            segments, already_done = self._resume_segments(job, segments, resume_state)
            job.downloaded_bytes = already_done
            logger.info(
                "Resuming %s at %s of %s bytes (%d segment(s))",
                job.file_path, f"{already_done:,}",
                f"{job.file_size:,}" if job.file_size > 0 else "?",
                sum(1 for s in segments if not s.completed),
            )
        elif job.file_size > 0:
            # Fresh download: make sure no stale bytes/tail from an earlier
            # attempt survive.
            truncate_file(job.file_path)
            job.downloaded_bytes = 0
            if job.file_size > 0:
                fd = preallocate_file(job.file_path, job.file_size)
                os.close(fd)

        pending = [s for s in segments if not s.completed]
        total_buffer = DynamicBufferSizer.calculate_buffer_size(job.file_size)
        per_buffer = DynamicBufferSizer.calculate_segment_buffer_size(
            total_buffer, max(1, len(pending))
        )

        job.segments = segments
        job.state = DownloadState.DOWNLOADING
        job.started_at = time.monotonic()

        stream_buf = StreamingBuffer(job.file_path, job.file_size)
        if (segments and segments[0].end_byte < 0
                and segments[0].downloaded_bytes > 0):
            # Contiguous prefix already on disk (single-connection resume).
            stream_buf.add_range(0, segments[0].downloaded_bytes)
        job.streaming_buffer = stream_buf

        self._cancel_events[job.id] = asyncio.Event()
        self._pause_events[job.id] = asyncio.Event()
        self._pause_events[job.id].set()   # set == running
        self._last_resume_save.pop(job.id, None)

        writers: dict[int, tuple[int, RAMRingBuffer]] = {}
        try:
            if pending:
                writers = self._open_writers(job, pending, per_buffer, stream_buf)
                self._save_resume_state(job, segments, force=True)
                try:
                    await self._download_all(job, session, pending, writers,
                                             progress_callback, all_segments=segments)
                except RangeUnsupported as exc:
                    # The server answered a Range request with the whole file.
                    # Start over sequentially into a truncated file.
                    logger.info("Falling back to a single connection for %s (%s)",
                                job.url, exc)
                    self._close_writers(writers)
                    truncate_file(job.file_path)
                    self._clear_resume_state(job)
                    segments = [SegmentProgress(0, 0, -1)]
                    job.thread_count = 1
                    job.segments = segments
                    job.downloaded_bytes = 0
                    stream_buf.reset()
                    writers = self._open_writers(job, segments, per_buffer, stream_buf)
                    await self._download_all(job, session, segments, writers,
                                             progress_callback, all_segments=segments)

            for _, ring in writers.values():
                await ring.flush()
            self._sync_segment_progress(job.segments, writers)
            # A single-connection download is a contiguous prefix; make the
            # preview's view of the file whole now that it is complete.
            stream_buf.mark_complete()

            expected = job.metadata.get("expected_checksum")
            if expected:
                ok, detail = verify_checksum(job.file_path, expected)
                if not ok:
                    logger.error("Checksum verification failed for %s: %s",
                                 job.file_path, detail)
                    job.state = DownloadState.FAILED
                    job.error_message = f"Checksum verification failed — {detail}"
                    return
                job.metadata["checksum_verified"] = detail
                logger.info("Checksum verified for %s (%s)", job.file_path, detail)

            self._clear_resume_state(job)
        except asyncio.CancelledError:
            # The user asked to stop: discard the partial file and its state.
            cleanup_partial_file(job.file_path)
            raise
        except Exception:
            # A failure (dropped connection, server error) is resumable: flush
            # what we still hold, record exactly what reached the disk, and
            # keep the file so the next attempt continues instead of restarting.
            for _, ring in writers.values():
                try:
                    await ring.flush()
                except Exception:  # noqa: BLE001
                    logger.debug("Flush during failure handling failed")
            self._sync_segment_progress(job.segments, writers)
            self._save_resume_state(job, job.segments, force=True)
            raise
        finally:
            self._close_writers(writers)
            self._cancel_events.pop(job.id, None)
            self._pause_events.pop(job.id, None)

    def _open_writers(self, job, segments, buffer_size, stream_buf):
        """One file descriptor + one ring buffer per segment.

        Each writer owns a distinct region of the file, and each gets its own
        OS file descriptor so ``lseek``-based writes on platforms without
        ``os.pwrite`` (Windows) cannot race with another segment.
        """
        writers: dict[int, tuple[int, RAMRingBuffer]] = {}
        for seg in segments:
            fd = os.open(job.file_path, os.O_WRONLY | os.O_CREAT, 0o644)
            # The ring writes positionally from here: for a resumed download
            # that is the first byte we still need.
            base = seg.start_byte + seg.downloaded_bytes
            ring = RAMRingBuffer(
                fd,
                buffer_size=buffer_size,
                base_offset=base,
                on_flush=stream_buf.add_range,
            )
            writers[seg.segment_index] = (fd, ring)
        return writers

    @staticmethod
    def _close_writers(writers) -> None:
        for fd, ring in writers.values():
            try:
                ring.close()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to close ring buffer")
            try:
                os.close(fd)
            except OSError:
                pass
        writers.clear()

    @staticmethod
    def _build_segments(file_size, thread_count):
        segs = []
        cs = file_size // thread_count
        for i in range(thread_count):
            s = i * cs
            e = (i + 1) * cs - 1 if i < thread_count - 1 else file_size - 1
            segs.append(SegmentProgress(i, s, e))
        return segs

    # ── transfer ────────────────────────────────────────────────

    async def _download_all(self, job, session, segments, writers, cb,
                            all_segments=None):
        """Run every segment concurrently, failing the job as a whole.

        ``asyncio.gather`` alone would report only the first error and leave
        the other segment tasks running against a file that is about to be
        closed, so siblings are cancelled explicitly and the first real
        error is re-raised.  ``all_segments`` is the complete layout (including
        already-finished segments) used when saving resume state.
        """
        state_segments = all_segments if all_segments is not None else segments
        tasks = []
        for seg in segments:
            fd, ring = writers[seg.segment_index]
            if seg.end_byte < 0:
                tasks.append(asyncio.create_task(
                    self._stream(job, session, ring, cb, state_segments)))
            else:
                tasks.append(asyncio.create_task(
                    self._seg(job, session, seg, ring, cb, state_segments)))
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        for task in tasks:
            if not task.done():
                task.cancel()
        errors = [
            r for r in results
            if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError)
        ]
        if errors:
            raise errors[0]
        if any(isinstance(r, asyncio.CancelledError) for r in results):
            # The user cancelled: every segment stopped.  This must propagate,
            # otherwise download_manager sees a clean return and marks a
            # cancelled job as COMPLETED.
            raise asyncio.CancelledError()

    async def _check_control(self, job) -> None:
        """Raise on cancel; block while paused."""
        if self._cancel_events.get(job.id, asyncio.Event()).is_set():
            raise asyncio.CancelledError()
        pe = self._pause_events.get(job.id)
        if pe and not pe.is_set():
            # Pause must not be able to block cancellation forever.
            await_pe = asyncio.create_task(pe.wait())
            cancel_ev = self._cancel_events.get(job.id)
            if cancel_ev is not None:
                waiter = asyncio.create_task(cancel_ev.wait())
                done, pending = await asyncio.wait(
                    {await_pe, waiter}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                if waiter in done:
                    raise asyncio.CancelledError()
            else:
                await await_pe

    def _record_chunk(self, job, seg, ring, chunk, samples, cb, segmented):
        """Bookkeeping shared by the segmented and single-stream paths."""
        seg.downloaded_bytes += len(chunk)
        job.mark_downloaded(len(chunk))
        now = time.monotonic()
        samples.append((now, len(chunk)))
        if len(samples) > 20:
            samples.pop(0)
        dt = samples[-1][0] - samples[0][0]
        db = sum(s[1] for s in samples)
        seg.speed_bps = db / max(dt, 0.001)
        if segmented:
            job.update_speed(
                sum(s.speed_bps for s in job.segments) / max(1, len(job.segments))
            )
        else:
            job.update_speed(seg.speed_bps)
        if cb:
            cb(job)

    async def _seg(self, job, session, seg, ring, cb, state_segments=None):
        offset = self._request_offset(seg)
        expected = seg.end_byte - offset + 1
        headers = {
            "Range": f"bytes={offset}-{seg.end_byte}",
            "Accept-Encoding": "identity",
        }
        samples: list[tuple[float, int]] = []

        async with session.get(job.url, headers=headers) as resp:
            if resp.status == 200:
                # Range ignored — the body is the entire file.
                raise RangeUnsupported(
                    f"server returned 200 for bytes={offset}-{seg.end_byte}"
                )
            if resp.status != 206:
                raise aiohttp.ClientError(f"HTTP {resp.status} for range request")
            if resp.content_length is not None and resp.content_length != expected:
                logger.debug(
                    "Segment %s: server announced %d bytes, expected %d",
                    seg.segment_index, resp.content_length, expected,
                )
            async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                await self._check_control(job)
                await ring.write(chunk)
                if self._bw:
                    await self._bw.throttle(job.id, len(chunk))
                self._record_chunk(job, seg, ring, chunk, samples, cb,
                                   segmented=True)
                self._save_resume_state(job, state_segments or [seg])

        if seg.downloaded_bytes != seg.end_byte - seg.start_byte + 1:
            # A short segment silently corrupts the file — fail loudly.
            raise aiohttp.ClientError(
                f"segment {seg.segment_index} truncated: have "
                f"{seg.downloaded_bytes} of {seg.end_byte - seg.start_byte + 1} bytes"
            )
        seg.completed = True
        self._save_resume_state(job, state_segments or [seg], force=True)

    async def _stream(self, job, session, ring, cb, state_segments=None):
        samples: list[tuple[float, int]] = []
        parsed_url = urlparse(job.url)
        referer = f"{parsed_url.scheme}://{parsed_url.netloc}/"
        seg = job.segments[0] if job.segments else SegmentProgress(0, 0, -1)
        resume_from = self._request_offset(seg) if seg.end_byte < 0 else 0
        headers = {"Referer": referer, "Accept-Encoding": "identity"}
        if resume_from > 0:
            # Continue a single-connection download from where it stopped.
            headers["Range"] = f"bytes={resume_from}-"

        for attempt in range(3):
            async with session.get(job.url, headers=headers) as resp:
                if resp.status == 403 and attempt < 2:
                    logger.warning(
                        "403 on attempt %d for %s, retrying...", attempt + 1, job.url
                    )
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                if resume_from > 0 and resp.status == 200:
                    # We asked to continue and got the whole file from byte 0;
                    # appending it would corrupt the result.
                    raise RangeUnsupported(
                        f"server ignored resume Range bytes={resume_from}-"
                    )
                if resp.status not in (200, 206):
                    raise aiohttp.ClientError(
                        f"HTTP {resp.status} — {job.url}\n"
                        f"The server rejected the request. The site may require "
                        f"authentication or block automated downloads."
                    )
                cl = resp.headers.get("Content-Length")
                if cl and resp.status == 200:
                    try:
                        job.file_size = int(cl)
                    except ValueError:
                        logger.debug("Non-numeric Content-Length %r for %s", cl, job.url)
                async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                    await self._check_control(job)
                    await ring.write(chunk)
                    if self._bw:
                        await self._bw.throttle(job.id, len(chunk))
                    self._record_chunk(job, seg, ring, chunk, samples, cb,
                                       segmented=False)
                    self._save_resume_state(job, state_segments or [seg])
                break  # success — exit retry loop

        if job.file_size > 0 and job.downloaded_bytes < job.file_size:
            raise aiohttp.ClientError(
                f"connection closed early: received {job.downloaded_bytes} of "
                f"{job.file_size} bytes"
            )
        self._save_resume_state(job, state_segments or [seg], force=True)

    # ── control ─────────────────────────────────────────────────

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

    # ── session / metadata ──────────────────────────────────────

    async def _get_session(self):
        if self._session is None or self._session.closed:
            connector = None
            if self._proxy_manager and self._proxy_manager.enabled:
                connector = self._proxy_manager.get_aiohttp_connector()
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            }
            self._session = aiohttp.ClientSession(
                connector=connector or aiohttp.TCPConnector(limit=32),
                timeout=aiohttp.ClientTimeout(total=None, connect=30, sock_read=120),
                headers=headers,
            )
        return self._session

    async def close(self) -> None:
        """Release the HTTP session (called on shutdown)."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def extract_metadata(self, url):
        """Describe *url* via HEAD, falling back to a 1-byte ranged GET.

        Many servers (and CDNs) reject HEAD, so the GET fallback is what makes
        size and range support detectable in practice.
        """
        session = await self._get_session()
        probe_headers = {"Accept-Encoding": "identity"}

        try:
            async with session.head(url, allow_redirects=True,
                                    headers=probe_headers) as resp:
                if resp.status < 400 and resp.headers.get("Content-Length"):
                    return self._meta_from_headers(url, resp, resp.headers)
                logger.debug("HEAD probe returned %s for %s — trying ranged GET",
                             resp.status, url)
        except aiohttp.ClientError as exc:
            logger.debug("HEAD probe failed for %s: %s", url, exc)

        headers = dict(probe_headers)
        headers["Range"] = "bytes=0-0"
        async with session.get(url, allow_redirects=True, headers=headers) as resp:
            if resp.status >= 400:
                raise aiohttp.ClientError(f"HTTP {resp.status} — {url}")
            meta = self._meta_from_headers(url, resp, resp.headers)
            total = -1
            content_range = resp.headers.get("Content-Range", "")
            if "/" in content_range:
                try:
                    total = int(content_range.rsplit("/", 1)[-1])
                except ValueError:
                    total = -1
            if total <= 0:
                try:
                    total = int(resp.headers.get("Content-Length", -1))
                except ValueError:
                    total = -1
            meta["file_size"] = total
            meta["accept_ranges"] = "bytes" if resp.status == 206 else "none"
            return meta

    @staticmethod
    def _meta_from_headers(url, resp, headers) -> dict:
        try:
            size = int(headers.get("Content-Length", -1))
        except (TypeError, ValueError):
            size = -1
        return {
            "title": HTTPDownloader._extract_filename(
                url, headers.get("Content-Disposition", "")
            ),
            "file_size": size,
            "content_type": headers.get("Content-Type", "application/octet-stream"),
            "accept_ranges": headers.get("Accept-Ranges", "none"),
            "url": str(resp.url),
        }

    @staticmethod
    def _extract_filename(url, content_disp):
        """Best-effort filename from Content-Disposition or the URL path.

        Only the final path component is kept: a header like
        ``filename=../../authorized_keys`` must not escape the download
        directory by way of ``job.file_name`` → ``job.file_path``.
        """
        if "filename=" in content_disp:
            candidate = unquote(content_disp.split("filename=")[-1].strip('" '))
            name = Path(candidate.replace("\\", "/")).name
            if name:
                return name
        name = Path(unquote(urlparse(url).path)).name
        return name if name else "download"

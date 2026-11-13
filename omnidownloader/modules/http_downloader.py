"""Multi-threaded HTTP/HTTPS/FTP chunked downloader."""

from __future__ import annotations

import asyncio
import email.utils
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
from omnidownloader.core.segment_planner import SegmentPlanner, plan_chunks
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

#: A connection that moves no bytes for this long loses its piece, which is
#: then re-split and shared out.  Local testing on a bad link needs this small;
#: the cost when everything is healthy is one 2 s watchdog tick.
STALL_SECONDS = 15.0

#: How often the stall watchdog looks at the pool.
WATCHDOG_INTERVAL = 2.0

#: Retries for one piece on a transient error (connection reset, 429, 5xx).
CHUNK_ATTEMPTS = 4

#: Upper bound on a server-suggested Retry-After (seconds).
MAX_RETRY_AFTER = 30.0

#: First retry delay; doubles per attempt (1 s, 2 s, 4 s, 8 s…).
RETRY_BASE_DELAY = 1.0

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


class _Retryable(Exception):
    """A failure worth another attempt (reset connection, 429, 5xx, short body)."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = max(0.0, float(retry_after or 0.0))


def _retry_after(resp) -> float:
    """Seconds the server asked us to wait (``Retry-After``), capped."""
    raw = (resp.headers.get("Retry-After") or "").strip()
    if not raw:
        return 0.0
    try:
        return min(MAX_RETRY_AFTER, max(0.0, float(raw)))
    except ValueError:
        pass
    try:                                   # HTTP-date form
        when = email.utils.parsedate_to_datetime(raw)
        delay = when.timestamp() - time.time()
        return min(MAX_RETRY_AFTER, max(0.0, delay))
    except (TypeError, ValueError):
        return 0.0


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
        #: ETag / Last-Modified seen at probe time, per job: they go into the
        #: resume sidecar and into If-Range on every range request.
        self._validators: dict[str, dict[str, str]] = {}
        #: Last time each active piece advanced, for the stall watchdog.
        self._chunk_progress: dict[tuple[str, int], float] = {}
        #: Last time the job as a whole advanced (the watchdog needs both).
        self._job_progress: dict[str, float] = {}

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

    def _load_resume_state(self, job, meta: Optional[dict] = None) -> Optional[dict]:
        """Read the sidecar for an interrupted download, if it is compatible.

        A file can be replaced on the server with a *different* file of the same
        size, which a size check alone cannot see — resuming then mixes two
        versions into one corrupt result.  The validator (ETag / Last-Modified)
        recorded at probe time is compared as well.
        """
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
        if meta and self._validator_changed(state, meta):
            logger.info("Remote file changed (%s) — discarding the partial file",
                        state.get("etag") or state.get("last_modified"))
            return None
        return state

    @staticmethod
    def _validator_changed(state: dict, meta: dict) -> bool:
        """True when the server's current ETag/Last-Modified differs from the sidecar's."""
        for key in ("etag", "last_modified"):
            old = str(state.get(key) or "").strip()
            new = str(meta.get(key) or "").strip()
            if old and new and old != new:
                return True
        return False

    def _save_resume_state(self, job, segments, force: bool = False) -> None:
        """Persist per-segment progress so the download can be resumed."""
        now = time.monotonic()
        if not force and (now - self._last_resume_save.get(job.id, 0.0)) < RESUME_SAVE_INTERVAL:
            return
        self._last_resume_save[job.id] = now
        validators = self._validators.get(job.id, {})
        state = {
            "version": 2,
            "url": job.url,
            "file_size": job.file_size,
            #: Freshness markers, so a replaced file is not resumed into.
            "etag": validators.get("etag", ""),
            "last_modified": validators.get("last_modified", ""),
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
            # The start byte is the identity that matters: pieces are planned
            # deterministically, and a piece that was re-split in an earlier
            # session comes back with a *different end* (the split truncated it)
            # but the same start.  Requiring both to match threw away progress
            # that was already on disk; the recorded prefix is still valid, so
            # only the start has to agree, and `done` is clamped to the length.
            if not old or old.get("start_byte") != seg.start_byte:
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

        # The validator pair travels with the resume state: if the file was
        # replaced on the server, bytes from two versions must never mix.
        self._validators[job.id] = {
            "etag": str(meta.get("etag") or ""),
            "last_modified": str(meta.get("last_modified") or ""),
        }

        # More pieces than connections (see core/segment_planner): a free
        # connection takes another piece instead of idling, which is what
        # shortens the tail of the download.
        segments = (
            self._build_segments(job.file_size, job.thread_count)
            if use_range
            else [SegmentProgress(0, 0, -1)]
        )
        if job.sequential and len(segments) > 1:
            segments.sort(key=lambda s: s.segment_index)

        # ── resume an interrupted download ──────────────────────
        resume_state = self._load_resume_state(job, meta)
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
        # Split the RAM budget across *connections*, not across pieces: there
        # are several pieces per connection, and dividing by them all left each
        # ring buffer at its 1 MiB floor.
        per_buffer = DynamicBufferSizer.calculate_segment_buffer_size(
            total_buffer, max(1, job.thread_count or 1)
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

        # Active writers only: the pool opens one when it claims a piece and
        # closes it on release (the piece table is finer than the pool).
        writers: dict[int, tuple[int, RAMRingBuffer]] = {}
        try:
            if pending:
                self._save_resume_state(job, segments, force=True)
                try:
                    await self._download_all(job, session, pending, writers,
                                             progress_callback, all_segments=segments,
                                             buffer_size=per_buffer,
                                             stream_buf=stream_buf)
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
                    writers = {}
                    await self._download_all(job, session, segments, writers,
                                             progress_callback, all_segments=segments,
                                             buffer_size=per_buffer,
                                             stream_buf=stream_buf)

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
            self._validators.pop(job.id, None)
            self._last_resume_save.pop(job.id, None)

    def _open_writer(self, job, seg, buffer_size, stream_buf):
        """A file descriptor + ring buffer for the piece *seg* is about to write.

        Each writer owns a distinct region of the file, and each gets its own
        OS file descriptor so ``lseek``-based writes on platforms without
        ``os.pwrite`` (Windows) cannot race with another segment.  Writers are
        created when a connection claims a piece and closed when it releases it:
        the piece table is now finer than the connection pool, so pre-opening one
        per piece would mean hundreds of descriptors.
        """
        fd = os.open(job.file_path, os.O_WRONLY | os.O_CREAT, 0o644)
        # The ring writes positionally from here: for a resumed download that is
        # the first byte we still need.
        base = seg.start_byte + seg.downloaded_bytes
        ring = RAMRingBuffer(
            fd,
            buffer_size=buffer_size,
            base_offset=base,
            on_flush=stream_buf.add_range,
        )
        return fd, ring

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
        """The piece grid for a file (see :mod:`omnidownloader.core.segment_planner`)."""
        return plan_chunks(file_size, thread_count)

    # ── transfer ────────────────────────────────────────────────

    async def _download_all(self, job, session, segments, writers, cb,
                            all_segments=None, buffer_size=None, stream_buf=None):
        """Fetch *segments* with a pool of connections that share the work.

        Each worker repeatedly claims the largest piece nobody is working on, so
        a connection that finishes early takes more work instead of idling and
        the tail of the file is never left to a single connection.  A piece that
        stops moving bytes is cancelled by :meth:`_watchdog` and its remainder
        re-split, so one dead connection cannot hold a download hostage.

        ``all_segments`` is the complete layout (finished pieces included) used
        for resume state; it must be the same list object the planner mutates,
        so a re-split stays visible to the sidecar.
        """
        state_segments = all_segments if all_segments is not None else segments
        planner = SegmentPlanner(state_segments, sequential=job.sequential)
        pool_size = max(1, min(job.thread_count or 1, max(1, len(state_segments))))
        wakeup = asyncio.Event()
        wakeup.set()
        errors: list[BaseException] = []
        active: dict[int, asyncio.Task] = {}
        stalled: set[int] = set()

        def record_disk(seg, ring) -> None:
            """Set ``downloaded_bytes`` to what actually reached the file."""
            try:
                on_disk = max(0, ring.file_position - seg.start_byte)
            except Exception:  # noqa: BLE001
                return
            seg.downloaded_bytes = min(on_disk, seg.length) if seg.length else on_disk

        async def worker(_slot: int) -> None:
            while True:
                seg = planner.claim()
                if seg is None:
                    if not planner.pending():
                        return
                    # Every remaining piece is already being fetched: wait for
                    # one to be released rather than spinning.
                    wakeup.clear()
                    try:
                        await asyncio.wait_for(wakeup.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                    continue

                fd, ring = self._open_writer(job, seg, buffer_size, stream_buf)
                writers[seg.segment_index] = (fd, ring)
                self._chunk_progress[(job.id, seg.segment_index)] = time.monotonic()
                piece = asyncio.create_task(
                    self._seg(job, session, seg, ring, cb, state_segments))
                active[seg.segment_index] = piece
                try:
                    await piece
                except asyncio.CancelledError:
                    cancel_ev = self._cancel_events.get(job.id)
                    if cancel_ev is not None and cancel_ev.is_set():
                        raise                      # the user stopped the job
                    await ring.flush()
                    record_disk(seg, ring)
                    if seg.segment_index not in stalled:
                        raise                      # not our cancellation
                    stalled.discard(seg.segment_index)
                    new_seg = planner.split_remainder(seg)
                    if new_seg is None:
                        planner.release(seg, completed=False)
                        errors.append(aiohttp.ClientError(
                            f"connection stalled with {seg.remaining:,} bytes left "
                            f"and the remainder is too small to re-share"))
                        return
                    logger.warning(
                        "Segment %s stalled — its remaining %s bytes went to "
                        "segment %s", seg.segment_index,
                        f"{new_seg.remaining:,}", new_seg.segment_index)
                    continue
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                    await ring.flush()
                    record_disk(seg, ring)
                    planner.release(seg, completed=False)
                    return
                else:
                    await ring.flush()
                    record_disk(seg, ring)
                    planner.release(seg, completed=True)
                finally:
                    active.pop(seg.segment_index, None)
                    self._chunk_progress.pop((job.id, seg.segment_index), None)
                    writers.pop(seg.segment_index, None)
                    self._close_writers({seg.segment_index: (fd, ring)})
                    wakeup.set()
                    self._save_resume_state(job, state_segments, force=True)
        async def watchdog() -> None:
            """Drop connections that stopped moving bytes; fail a dead job.

            Two rules, and the difference matters:

            * one frozen piece while the rest of the job is still moving means
              a dead connection — cancel it so its worker re-shares the rest;
            * *nothing* moving for twice as long means the link itself is gone
              — fail the job with a message instead of hanging forever.
            """
            while True:
                await asyncio.sleep(WATCHDOG_INTERVAL)
                cancel_ev = self._cancel_events.get(job.id)
                if cancel_ev is not None and cancel_ev.is_set():
                    return
                if not planner.pending():
                    return
                if self._bw is not None and self._bw.is_limited(job.id):
                    # Throttled on purpose: silence is the rate limit, not a
                    # dead peer.
                    continue

                now = time.monotonic()
                job_moved = now - self._job_progress.get(job.id, now)
                if job_moved >= 2 * STALL_SECONDS:
                    errors.append(aiohttp.ClientError(
                        f"no data received for {job_moved:.0f}s — the connection "
                        "appears to be dead"))
                    for piece in list(active.values()):
                        piece.cancel()
                    return

                for seg in planner.active():
                    if seg.segment_index in stalled:
                        continue
                    piece = active.get(seg.segment_index)
                    if piece is None or piece.done():
                        continue
                    last = self._chunk_progress.get((job.id, seg.segment_index), now)
                    if now - last < STALL_SECONDS:
                        continue
                    if job_moved >= STALL_SECONDS:
                        # Everything is quiet, not just this piece: give it one
                        # more window before declaring the job dead.
                        continue
                    stalled.add(seg.segment_index)
                    logger.warning(
                        "Segment %s got no data for %.0fs with %s bytes left — "
                        "dropping the connection and re-sharing the rest",
                        seg.segment_index, now - last, f"{seg.remaining:,}")
                    seg.speed_bps = 0.0
                    piece.cancel()

        tasks = [asyncio.create_task(worker(slot)) for slot in range(pool_size)]
        doggie = asyncio.create_task(watchdog())
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            doggie.cancel()
            await asyncio.gather(doggie, return_exceptions=True)
            for key in [k for k in self._chunk_progress if k[0] == job.id]:
                self._chunk_progress.pop(key, None)
            self._job_progress.pop(job.id, None)

        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        cancel_ev = self._cancel_events.get(job.id)
        if cancel_ev is not None and cancel_ev.is_set():
            # Cancellation must not look like a finished download.
            raise asyncio.CancelledError()

        for exc in errors:
            if not isinstance(exc, asyncio.CancelledError):
                raise exc
        if errors:
            raise errors[0]
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                raise exc
        if planner.pending():
            raise aiohttp.ClientError(
                "the connection pool stopped before every piece was downloaded"
            )

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
        self._chunk_progress[(job.id, seg.segment_index)] = now
        self._job_progress[job.id] = now
        samples.append((now, len(chunk)))
        if len(samples) > 20:
            samples.pop(0)
        dt = samples[-1][0] - samples[0][0]
        db = sum(s[1] for s in samples)
        seg.speed_bps = db / max(dt, 0.001)
        if segmented:
            # Only the pieces a connection is holding report a speed (finished
            # ones are zeroed on release), so the sum is the pool's throughput.
            # Averaging over every piece — the old code — divided a 12-piece
            # download by 12 and showed a third of the real speed.
            job.update_speed(sum(s.speed_bps for s in job.segments))
        else:
            job.update_speed(seg.speed_bps)
        if cb:
            cb(job)

    async def _seg(self, job, session, seg, ring, cb, state_segments=None):
        """Download one piece, retrying transient failures without losing bytes.

        A piece is not an all-or-nothing unit: each attempt resumes at
        ``start_byte + downloaded_bytes``, so a connection that dies half-way is
        retried from where it stopped, and the bytes already buffered stay.
        """
        if seg.end_byte < 0:
            # Unknown size: one connection streams until the server closes.
            await self._stream(job, session, ring, cb, state_segments)
            return
        attempt = 0
        while True:
            try:
                await self._seg_once(job, session, seg, ring, cb, state_segments)
                return
            except _Retryable as exc:
                attempt += 1
                if attempt >= CHUNK_ATTEMPTS:
                    raise aiohttp.ClientError(
                        f"segment {seg.segment_index}: giving up after "
                        f"{attempt} attempts — {exc}") from exc
                delay = exc.retry_after or min(RETRY_BASE_DELAY * (2.0 ** attempt), 8.0)
                logger.info(
                    "Segment %s: %s — retrying in %.1fs (%d/%d)",
                    seg.segment_index, exc, delay, attempt, CHUNK_ATTEMPTS - 1)
                await asyncio.sleep(delay)

    async def _seg_once(self, job, session, seg, ring, cb, state_segments=None):
        offset = self._request_offset(seg)
        expected = seg.end_byte - offset + 1
        if expected <= 0:
            return
        headers = {
            "Range": f"bytes={offset}-{seg.end_byte}",
            "Accept-Encoding": "identity",
        }
        # If-Range makes the server answer 200 (whole file) instead of a 206 for
        # a file that was replaced since the probe: mixing two versions is the
        # one thing a resumed download must never do.
        etag = self._validators.get(job.id, {}).get("etag")
        if etag and offset > 0:
            headers["If-Range"] = etag
        samples: list[tuple[float, int]] = []

        try:
            async with session.get(job.url, headers=headers) as resp:
                if resp.status == 200:
                    # Range ignored — the body is the entire file.
                    raise RangeUnsupported(
                        f"server returned 200 for bytes={offset}-{seg.end_byte}"
                    )
                if resp.status in (429, 503) or resp.status >= 500:
                    raise _Retryable(f"HTTP {resp.status}",
                                     retry_after=_retry_after(resp))
                if resp.status != 206:
                    raise aiohttp.ClientError(
                        f"HTTP {resp.status} for range request")
                if (resp.content_length is not None
                        and resp.content_length != expected):
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
        except asyncio.CancelledError:
            raise
        except RangeUnsupported:
            raise
        except aiohttp.ClientError as exc:
            # Connection reset, timeout, malformed response: worth another try.
            raise _Retryable(str(exc)) from exc

        if seg.downloaded_bytes < seg.end_byte - seg.start_byte + 1:
            got = seg.downloaded_bytes
            want = seg.end_byte - seg.start_byte + 1
            raise _Retryable(f"body ended early ({got:,} of {want:,} bytes)")

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

        max_attempts = CHUNK_ATTEMPTS
        for attempt in range(1, max_attempts + 1):
            # Recomputing the resume point each attempt matters: a dropped
            # connection keeps the bytes already written and continues from
            # there instead of asking for the file again.
            if attempt > 1:
                resume_from = self._request_offset(seg) if seg.end_byte < 0 else 0
                if resume_from > 0:
                    headers["Range"] = f"bytes={resume_from}-"
            async with session.get(job.url, headers=headers) as resp:
                if resp.status == 403 and attempt < max_attempts:
                    logger.warning(
                        "403 on attempt %d for %s, retrying...", attempt, job.url
                    )
                    await asyncio.sleep(2 * attempt)
                    continue
                if resp.status in (429, 503) or resp.status >= 500:
                    # A rate limit or an overloaded mirror: wait as asked, then
                    # try again rather than failing the whole download.
                    wait = _retry_after(resp) or min(
                        RETRY_BASE_DELAY * (2.0 ** attempt), MAX_RETRY_AFTER)
                    if attempt >= max_attempts:
                        raise aiohttp.ClientError(
                            f"HTTP {resp.status} — {job.url}\n"
                            f"The server kept refusing the request (rate limit "
                            f"or outage). Try again later or use a proxy.")
                    logger.warning("HTTP %s for %s — retrying in %.0fs (%d/%d)",
                                   resp.status, job.url, wait, attempt, max_attempts - 1)
                    await asyncio.sleep(wait)
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

                # The body ended.  Either everything arrived, or the peer cut
                # the stream — in which case the next attempt continues from
                # the last stored byte instead of restarting the file.
                if job.file_size > 0 and job.downloaded_bytes < job.file_size:
                    if attempt < max_attempts:
                        logger.warning(
                            "Stream for %s ended early at %s of %s bytes — "
                            "resuming (%d/%d)", job.url,
                            f"{job.downloaded_bytes:,}", f"{job.file_size:,}",
                            attempt, max_attempts - 1)
                        continue
                    raise aiohttp.ClientError(
                        f"connection closed early: received "
                        f"{job.downloaded_bytes} of {job.file_size} bytes"
                    )
                break  # success — exit retry loop

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
            # Freshness markers: recorded in the resume sidecar and sent back as
            # If-Range, so a file replaced on the server is never resumed into.
            "etag": headers.get("ETag", ""),
            "last_modified": headers.get("Last-Modified", ""),
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

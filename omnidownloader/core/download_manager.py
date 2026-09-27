"""Download Manager & Scheduler with Bandwidth Control."""

from __future__ import annotations

import asyncio
import heapq
import logging
import time
from pathlib import Path
from typing import Any, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from omnidownloader.core import platform_utils
from omnidownloader.core.base_module import BaseDownloaderModule
from omnidownloader.core.models import (
    DownloadJob, DownloadModule, DownloadState, Priority, PRIORITY_WEIGHTS,
)
from omnidownloader.core.bandwidth_limiter import BandwidthManager
from omnidownloader.core.disk_utils import cleanup_partial_file

logger = logging.getLogger(__name__)

# Priority sort key: lower = dispatched first
_PRIORITY_SORT = {Priority.HIGH: 0, Priority.NORMAL: 1, Priority.LOW: 2}

#: Characters that are unsafe in filenames on Windows and/or POSIX.
_UNSAFE_FILENAME_CHARS = '<>:"/\\|?*\n\r\t'


def _safe_filename(name: str, limit: int = 120) -> str:
    """Sanitise a title into a file name usable on Linux, macOS *and* Windows.

    Delegates to the platform layer so Windows' reserved device names, trailing
    dots/spaces and its 260-character path limit are handled in one place.
    """
    return platform_utils.safe_filename(name, max_length=limit)


class DownloadManager(QObject):
    job_added = pyqtSignal(str)
    job_removed = pyqtSignal(str)
    job_state_changed = pyqtSignal(str, str)
    job_progress = pyqtSignal(str, float, float)
    global_speed_update = pyqtSignal(float)
    format_selection_needed = pyqtSignal(str, dict)  # job_id, metadata

    def __init__(self, max_concurrent=4, global_max_speed=0.0,
                 download_dir="", parent=None):
        super().__init__(parent)
        self._max_concurrent = max_concurrent
        self._global_max_speed = global_max_speed
        self._download_dir = download_dir or str(
            Path.home() / "Downloads" / "OmniDownloader"
        )
        Path(self._download_dir).mkdir(parents=True, exist_ok=True)
        self._modules: list[BaseDownloaderModule] = []
        self._jobs: dict[str, DownloadJob] = {}
        self._active_tasks: dict[str, asyncio.Task] = {}
        # Dispatch queue: a heap of (priority_rank, sequence, job_id) plus an
        # Event to wake the dispatcher.  A plain FIFO queue ignored priority
        # entirely, so "High" never actually jumped ahead.
        self._heap: list[tuple[int, int, str]] = []
        self._heap_seq = 0
        self._wakeup = asyncio.Event()
        # Job ids added before the engine loop was running; drained by run()
        self._deferred_ids: list[str] = []
        self._speed_timer_task: Optional[asyncio.Task] = None
        self._proxy_manager = None
        self._tor_manager = None
        # ── Bandwidth management ─────────────────────────────────
        self._bw_manager = BandwidthManager(global_rate=global_max_speed)
        self._scheduler = None  # set later by main.py
        self._format_futures: dict[str, asyncio.Future] = {}
        self._format_loops: dict[str, asyncio.AbstractEventLoop] = {}

    @property
    def bandwidth_manager(self) -> BandwidthManager:
        return self._bw_manager

    def set_scheduler(self, scheduler) -> None:
        self._scheduler = scheduler

    def resolve_format(self, job_id: str, format_data: dict) -> None:
        """Resolve the format selection future for a job (called by UI)."""
        future = self._format_futures.get(job_id)
        loop = self._format_loops.get(job_id)
        if future and not future.done() and loop:
            loop.call_soon_threadsafe(future.set_result, format_data)

    # ── New Qt signals for proxy/anonymity ────────────────────
    kill_switch_activated = pyqtSignal(str)
    kill_switch_cleared = pyqtSignal()

    def set_proxy_manager(self, proxy_manager) -> None:
        self._proxy_manager = proxy_manager

    def register_module(self, module: BaseDownloaderModule) -> None:
        self._modules.append(module)
        logger.info("Registered module: %s", module.display_name())


    def enqueue(self, url: str, module_hint=None, download_path=None,
                priority=Priority.NORMAL, sequential=False, **kwargs):
        job = DownloadJob(url=url, priority=priority, sequential=sequential)
        if module_hint and module_hint != DownloadModule.UNKNOWN:
            job.module = module_hint
        else:
            mod = self.find_module_for_url(url)
            if mod is None:
                # No module matched — schedule a yt-dlp probe in the background
                # The job stays PENDING; _dispatch_job will probe and assign.
                job.module = DownloadModule.UNKNOWN
            else:
                # A plugin's MODULE_NAME may not exist in the built-in enum;
                # raising ValueError here aborted enqueue() inside a UI slot.
                job.module = DownloadModule._value2member_map_.get(
                    mod.MODULE_NAME.lower(), DownloadModule.UNKNOWN
                )
        if download_path:
            job.file_path = download_path
        self._jobs[job.id] = job
        self.job_added.emit(job.id)
        # Thread-safe: use the stored loop reference to schedule the queue put
        self._schedule_queue_put(job.id)
        return job

    def _schedule_queue_put(self, job_id: str) -> None:
        """Thread-safe: queue *job_id* on the background asyncio loop."""
        import omnidownloader.main as main_mod
        loop = main_mod._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._push_job, job_id)
        else:
            # The engine loop isn't up yet (a job added in the first moments
            # after launch, or after the engine thread died).  Dropping the id
            # here left the job stuck at PENDING with no error anywhere, so
            # remember it and let run() drain the list once the loop is live.
            logger.debug("Engine loop not running — deferring job %s", job_id)
            self._deferred_ids.append(job_id)

    def _push_job(self, job_id: str) -> None:
        """Add *job_id* to the dispatch heap (runs on the engine loop)."""
        job = self._jobs.get(job_id)
        if job is None:
            return
        rank = _PRIORITY_SORT.get(job.priority, 1)
        heapq.heappush(self._heap, (rank, self._heap_seq, job_id))
        self._heap_seq += 1
        self._wakeup.set()

    def set_max_concurrent(self, value: int) -> None:
        """Change how many downloads may run at once."""
        self._max_concurrent = max(1, int(value))
        logger.info("Max concurrent downloads set to %d", self._max_concurrent)
        self._wakeup.set()

    def set_default_task_rate(self, rate: float) -> None:
        """Set the default per-task bandwidth cap for all downloads."""
        self._bw_manager.set_default_task_rate(rate)

    def resume_all_active_jobs(self) -> None:
        """Resume every PAUSED job — used when the kill switch clears."""
        for job_id, job in list(self._jobs.items()):
            if job.state == DownloadState.PAUSED:
                self.resume_job(job_id)

    def _post_to_loop(self, coro) -> None:
        """Run *coro* on the engine loop from the Qt thread, logging failures.

        ``asyncio.ensure_future`` from the GUI thread raises (no running loop)
        and a bare future silently swallows exceptions, which is how "pause"
        could do nothing at all without a single line in the log.
        """
        import omnidownloader.main as main_mod
        loop = main_mod._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._spawn_logged, coro)
        else:
            logger.warning("Engine loop not running — cannot run %r", coro)
            # Close it: an un-awaited coroutine leaks and warns (and the
            # caller has already moved on).
            coro.close()

    def _spawn_logged(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        task.add_done_callback(self._log_task_result)

    @staticmethod
    def _log_task_result(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Background task failed: %s", exc, exc_info=exc)

    def remove_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        if job_id in self._active_tasks:
            self._active_tasks[job_id].cancel()
            del self._active_tasks[job_id]
        if job.file_path and job.state in (DownloadState.DOWNLOADING,
                                          DownloadState.PAUSED,
                                          DownloadState.EXTRACTING):
            cleanup_partial_file(job.file_path)
        self._bw_manager.remove_task_limiter(job_id)
        self._format_futures.pop(job_id, None)
        self._format_loops.pop(job_id, None)
        del self._jobs[job_id]
        self.job_removed.emit(job_id)

    def pause_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job and job.state == DownloadState.DOWNLOADING:
            mod = self.find_module_for_url(job.url)
            job.state = DownloadState.PAUSED
            self.job_state_changed.emit(job_id, job.state.value)
            if mod:
                self._post_to_loop(mod.pause(job))

    def resume_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job and job.state == DownloadState.PAUSED:
            mod = self.find_module_for_url(job.url)
            job.state = DownloadState.DOWNLOADING
            self.job_state_changed.emit(job_id, job.state.value)
            if mod:
                self._post_to_loop(mod.resume(job))
            self._rebalance_bandwidth()

    def set_job_priority(self, job_id: str, priority: Priority) -> None:
        """Change a job's priority and rebalance bandwidth."""
        job = self._jobs.get(job_id)
        if job:
            job.priority = priority
            # A queued job must be re-ranked, otherwise "High" still waits
            # behind everything already in the heap.
            if job.state == DownloadState.PENDING and job_id not in self._active_tasks:
                self._schedule_queue_put(job_id)
            self._rebalance_bandwidth()

    def _rebalance_bandwidth(self) -> None:
        """Distribute bandwidth pool among active jobs by priority weight."""
        active = self.active_jobs()
        if not active:
            return
        total_weight = sum(PRIORITY_WEIGHTS.get(j.priority, 2) for j in active)
        global_rate = self._bw_manager.global_rate
        for job in active:
            weight = PRIORITY_WEIGHTS.get(job.priority, 2)
            self._bw_manager.allocate_for_priority(
                job.id, global_rate, weight, total_weight,
            )

    def get_job(self, job_id):
        return self._jobs.get(job_id)

    def all_jobs(self):
        return list(self._jobs.values())

    def active_jobs(self):
        return [j for j in self._jobs.values()
                if j.state in (DownloadState.DOWNLOADING, DownloadState.EXTRACTING)]

    def queued_jobs(self):
        return [j for j in self._jobs.values() if j.state == DownloadState.PENDING]

    def completed_jobs(self):
        return [j for j in self._jobs.values() if j.state == DownloadState.COMPLETED]

    @property
    def download_dir(self):
        return self._download_dir

    @download_dir.setter
    def download_dir(self, path):
        self._download_dir = path
        Path(path).mkdir(parents=True, exist_ok=True)

    async def run(self):
        self._speed_timer_task = asyncio.create_task(self._speed_reporter())
        # Drain jobs queued before this loop was running, so nothing is left
        # sitting at PENDING forever.
        if self._deferred_ids:
            logger.info("Draining %d deferred job(s) now the engine is live",
                        len(self._deferred_ids))
            for jid in self._deferred_ids:
                self._push_job(jid)
            self._deferred_ids.clear()
        while True:
            if not self._heap:
                self._wakeup.clear()
                await self._wakeup.wait()
                continue
            _, _, job_id = heapq.heappop(self._heap)
            job = self._jobs.get(job_id)
            # Skip stale heap entries: the job may have been removed, already
            # dispatched via a re-ranked duplicate, or cancelled while queued.
            if (job is None or job.state != DownloadState.PENDING
                    or job_id in self._active_tasks):
                continue
            while self._active_job_count() >= self._max_concurrent:
                await self._wait_for_slot()
            task = asyncio.create_task(self._dispatch_job(job))
            self._active_tasks[job_id] = task

    def _active_job_count(self) -> int:
        """Number of live dispatch tasks, pruning finished ones."""
        self._active_tasks = {
            jid: t for jid, t in self._active_tasks.items() if not t.done()
        }
        return len(self._active_tasks)

    async def _wait_for_slot(self) -> None:
        """Wait for a running download to finish before dispatching more."""
        pending = list(self._active_tasks.values())
        if not pending:
            return
        # Event-driven instead of polling with a fixed sleep, which added up
        # to 0.5 s of dispatch latency per queued job.
        await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED, timeout=1.0)

    async def _dispatch_job(self, job):
        mod = await self._resolve_module(job)
        if mod is None:
            return

        # Create per-task bandwidth limiter
        self._bw_manager.create_task_limiter(job.id, rate=0.0)
        self._rebalance_bandwidth()

        def on_progress(j):
            self.job_progress.emit(j.id, j.progress_percent, j.speed_bps)

        job.state_callback = on_progress

        # For media jobs: extract metadata first, then request format selection
        if job.module == DownloadModule.MEDIA:
            job.state = DownloadState.EXTRACTING
            self.job_state_changed.emit(job.id, job.state.value)
            try:
                meta = await mod.extract_metadata(job.url)
                job.metadata.update(meta)
                job.file_name = meta.get("title", "Unknown")
            except Exception as exc:
                logger.exception("Metadata extraction failed for %s: %s", job.id, exc)
                # Fall through with defaults — start_download will handle it
                job.metadata.setdefault("formats", [])

            # A playlist has no single media to download: queue one job per
            # entry (after picking a quality once, which they all inherit).
            if job.metadata.get("is_playlist"):
                await self._enqueue_playlist(job)
                return

            # Only show format dialog if we have formats
            if job.metadata.get("formats") and not job.metadata.get("skip_format_dialog"):
                loop = asyncio.get_running_loop()
                self._format_loops[job.id] = loop
                self._format_futures[job.id] = loop.create_future()
                self.format_selection_needed.emit(job.id, job.metadata)

                future = self._format_futures.get(job.id)
                if future:
                    try:
                        chosen = await asyncio.wait_for(future, timeout=180)
                        job.metadata.update(chosen)
                    except (asyncio.TimeoutError, TypeError):
                        logger.info("Format selection timed out/cancelled for %s", job.id)
                    finally:
                        self._format_futures.pop(job.id, None)
                        self._format_loops.pop(job.id, None)

            job.file_name = job.metadata.get("title", job.file_name or "Unknown")

        job.started_at = time.monotonic()
        job.state = DownloadState.DOWNLOADING
        self.job_state_changed.emit(job.id, job.state.value)
        try:
            await mod.start_download(job, progress_callback=on_progress)
            # A module may have decided the download failed without raising
            # (e.g. not enough disk space) — never overwrite that as success.
            if job.state not in (DownloadState.CANCELLED, DownloadState.FAILED):
                job.state = DownloadState.COMPLETED
                job.completed_at = time.monotonic()
                self.job_state_changed.emit(job.id, job.state.value)
                self.job_progress.emit(job.id, 100.0, 0.0)
            elif job.state == DownloadState.FAILED:
                self.job_state_changed.emit(job.id, job.state.value)
        except asyncio.CancelledError:
            job.state = DownloadState.CANCELLED
            self.job_state_changed.emit(job.id, job.state.value)
        except Exception as exc:
            logger.exception("Job %s failed: %s", job.id, exc)
            job.state = DownloadState.FAILED
            job.error_message = str(exc)
            self.job_state_changed.emit(job.id, job.state.value)
        finally:
            # Clean up per-task limiter and rebalance remaining jobs
            self._bw_manager.remove_task_limiter(job.id)
            self._rebalance_bandwidth()

    async def _enqueue_playlist(self, job) -> None:
        """Queue one job per playlist entry, inheriting the chosen quality.

        The playlist itself is never downloaded: it becomes a container whose
        children appear as individual cards, which is also what yt-dlp users
        expect.  The quality dialog runs once, for the playlist, and the
        selection is copied into every child so they don't each prompt.
        """
        entries = job.metadata.get("entries") or []
        if not entries:
            job.state = DownloadState.FAILED
            job.error_message = "Playlist contains no downloadable entries."
            self.job_state_changed.emit(job.id, job.state.value)
            return

        # Ask for the quality once, using the first entry's format list.
        if job.metadata.get("formats") and not job.metadata.get("skip_format_dialog"):
            loop = asyncio.get_running_loop()
            self._format_loops[job.id] = loop
            self._format_futures[job.id] = loop.create_future()
            self.format_selection_needed.emit(job.id, job.metadata)
            future = self._format_futures.get(job.id)
            if future:
                try:
                    chosen = await asyncio.wait_for(future, timeout=180)
                    job.metadata.update(chosen)
                except (asyncio.TimeoutError, TypeError):
                    logger.info("Playlist format selection timed out for %s", job.id)
                finally:
                    self._format_futures.pop(job.id, None)
                    self._format_loops.pop(job.id, None)

        inherited = {
            "format": job.metadata.get("format", ""),
            "audio_only": job.metadata.get("audio_only", False),
            "audio_format": job.metadata.get("audio_format", "bestaudio"),
            "audio_format_ext": job.metadata.get("audio_format_ext", "mp3"),
            "quality_label": job.metadata.get("quality_label", ""),
            "subtitles": job.metadata.get("subtitles", []),
            # Children must not re-prompt: the quality was chosen once above.
            "skip_format_dialog": True,
            "playlist_title": job.metadata.get("title", ""),
        }
        target_dir = str(Path(job.file_path).parent) if job.file_path else ""

        queued = 0
        for entry in entries:
            child = DownloadJob(url=entry["url"], module=DownloadModule.MEDIA,
                                priority=job.priority, sequential=job.sequential)
            title = entry.get("title") or ""
            if target_dir:
                # MediaExtractor derives its output directory from
                # Path(job.file_path).parent, so point the child at the same
                # folder the parent was configured to use.
                safe = _safe_filename(title) or "video"
                child.file_path = str(Path(target_dir) / f"{safe}.mp4")
            child.file_name = title
            child.metadata.update(inherited)
            child.metadata["playlist_entry_title"] = title
            self._jobs[child.id] = child
            self.job_added.emit(child.id)
            self._schedule_queue_put(child.id)
            queued += 1

        job.state = DownloadState.COMPLETED
        job.completed_at = time.monotonic()
        job.file_name = f"{job.metadata.get('title', 'Playlist')} ({queued} items)"
        job.metadata["playlist_queued"] = queued
        job.error_message = None
        logger.info("Playlist %s: queued %d entry downloads", job.id, queued)
        self.job_state_changed.emit(job.id, job.state.value)
        self.job_progress.emit(job.id, 100.0, 0.0)

    async def _resolve_module(self, job):
        """Pick the module for *job*, probing when the answer is unclear.

        Two cases need a probe:

        * nothing claims the URL — ask yt-dlp whether it is media at all;
        * the HTTP module claims it but the URL turns out to serve an HTML
          page rather than a file — hand it to yt-dlp, which knows how to
          extract the real stream from thousands of sites.

        Returns the module, or ``None`` after marking the job FAILED.
        """
        from omnidownloader.modules.http_downloader import HTTPDownloader
        from omnidownloader.modules.media_extractor import MediaExtractor

        mod = self.find_module_for_url(job.url)
        media = self.find_module_by_type(MediaExtractor)
        ytdlp = getattr(media, "_ytdlp", "yt-dlp")
        ffmpeg = getattr(media, "_ffmpeg", "ffmpeg")

        needs_probe = mod is None

        if isinstance(mod, HTTPDownloader):
            # Ask the server what it is before committing to a plain fetch.
            probe_meta: dict = {}
            try:
                probe_meta = await mod.extract_metadata(job.url)
            except Exception as exc:  # noqa: BLE001
                logger.debug("HTTP probe failed for %s: %s", job.url, exc)
            if self._looks_like_webpage(probe_meta):
                logger.info("URL serves an HTML page — probing with yt-dlp: %s", job.url)
                needs_probe = True

        if needs_probe:
            job.state = DownloadState.EXTRACTING
            self.job_state_changed.emit(job.id, job.state.value)
            if await MediaExtractor.probe_url(job.url, ytdlp):
                mod = media or MediaExtractor(ytdlp_path=ytdlp, ffmpeg_path=ffmpeg)
                job.module = DownloadModule.MEDIA
                logger.info("yt-dlp probe succeeded — routing to MediaExtractor")
                return mod
            if mod is None:
                job.state = DownloadState.FAILED
                job.error_message = (
                    "No module can handle this URL and yt-dlp does not support it."
                )
                self.job_state_changed.emit(job.id, job.state.value)
                return None
            logger.info("yt-dlp cannot handle %s — using the HTTP downloader", job.url)

        if mod is None:
            job.state = DownloadState.FAILED
            job.error_message = "No module can handle this URL."
            self.job_state_changed.emit(job.id, job.state.value)
            return None
        return mod

    @staticmethod
    def _looks_like_webpage(meta: dict) -> bool:
        """True when a probe returned an HTML document instead of a file."""
        content_type = str(meta.get("content_type", "")).lower()
        return content_type.startswith("text/html")


    async def _speed_reporter(self):
        while True:
            await asyncio.sleep(1.0)
            total = sum(j.speed_bps for j in self.active_jobs())
            self.global_speed_update.emit(total)

    def pause_all_active_jobs(self) -> None:
        """Pause all active downloads — called by kill switch."""
        logger.critical("Kill switch: pausing ALL active downloads")
        for job_id, job in list(self._jobs.items()):
            if job.state == DownloadState.DOWNLOADING:
                mod = self.find_module_for_url(job.url)
                job.state = DownloadState.PAUSED
                self.job_state_changed.emit(job_id, job.state.value)
                if mod:
                    self._post_to_loop(mod.pause(job))
        self.kill_switch_activated.emit("All downloads paused by kill switch")

    def cancel_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        if job_id in self._active_tasks:
            self._active_tasks[job_id].cancel()
            del self._active_tasks[job_id]
        self._bw_manager.remove_task_limiter(job_id)
        mod = self.find_module_for_url(job.url)
        job.state = DownloadState.CANCELLED
        self.job_state_changed.emit(job_id, job.state.value)
        if mod:
            self._post_to_loop(mod.cancel(job))
        self._rebalance_bandwidth()

    def find_module_for_url(self, url: str) -> Optional[BaseDownloaderModule]:
        for m in self._modules:
            if m.can_handle(url):
                return m
        return None

    def find_module_by_type(self, cls) -> Optional[BaseDownloaderModule]:
        """Return the first registered module that is an instance of *cls*."""
        for m in self._modules:
            if isinstance(m, cls):
                return m
        return None

    async def shutdown(self) -> None:
        """Release module resources (aiohttp sessions, torrent handles, …)."""
        for mod in self._modules:
            closer = getattr(mod, "close", None)
            if closer is None:
                continue
            try:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to close module %s: %s",
                               type(mod).__name__, exc)

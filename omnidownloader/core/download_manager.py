"""Download Manager & Scheduler with Bandwidth Control."""

from __future__ import annotations

import asyncio
import heapq
import logging
import time
from pathlib import Path
from typing import Any, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from omnidownloader.core.base_module import BaseDownloaderModule
from omnidownloader.core.models import (
    DownloadJob, DownloadModule, DownloadState, Priority, PRIORITY_WEIGHTS,
)
from omnidownloader.core.bandwidth_limiter import BandwidthManager
from omnidownloader.core.disk_utils import cleanup_partial_file

logger = logging.getLogger(__name__)

# Priority sort key: lower = dispatched first
_PRIORITY_SORT = {Priority.HIGH: 0, Priority.NORMAL: 1, Priority.LOW: 2}


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
        self._queue: asyncio.Queue[str] = asyncio.Queue()
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
                job.module = DownloadModule(mod.MODULE_NAME.lower())
        if download_path:
            job.file_path = download_path
        self._jobs[job.id] = job
        self.job_added.emit(job.id)
        # Thread-safe: use the stored loop reference to schedule the queue put
        self._schedule_queue_put(job.id)
        return job

    def _schedule_queue_put(self, job_id: str) -> None:
        """Thread-safe: schedule a put_nowait on the background asyncio loop."""
        import omnidownloader.main as main_mod
        loop = main_mod._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._queue.put_nowait, job_id)
        else:
            # Fallback: direct put (may lose ordering but won't crash)
            try:
                self._queue.put_nowait(job_id)
            except Exception:
                pass

    def remove_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        if job_id in self._active_tasks:
            self._active_tasks[job_id].cancel()
            del self._active_tasks[job_id]
        if job.file_path and job.state == DownloadState.DOWNLOADING:
            cleanup_partial_file(job.file_path)
        del self._jobs[job_id]
        self.job_removed.emit(job_id)

    def pause_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job and job.state == DownloadState.DOWNLOADING:
            job.state = DownloadState.PAUSED
            self.job_state_changed.emit(job_id, job.state.value)
            mod = self.find_module_for_url(job.url)
            if mod:
                import omnidownloader.main as main_mod
                loop = main_mod._loop
                if loop and loop.is_running():
                    loop.call_soon_threadsafe(asyncio.ensure_future, mod.pause(job))

    def resume_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job and job.state == DownloadState.PAUSED:
            job.state = DownloadState.DOWNLOADING
            self.job_state_changed.emit(job_id, job.state.value)
            mod = self.find_module_for_url(job.url)
            if mod:
                import omnidownloader.main as main_mod
                loop = main_mod._loop
                if loop and loop.is_running():
                    loop.call_soon_threadsafe(asyncio.ensure_future, mod.resume(job))
            self._rebalance_bandwidth()

    def set_job_priority(self, job_id: str, priority: Priority) -> None:
        """Change a job's priority and rebalance bandwidth."""
        job = self._jobs.get(job_id)
        if job:
            job.priority = priority
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
        while True:
            job_id = await self._queue.get()
            job = self._jobs.get(job_id)
            if job is None or job.state != DownloadState.PENDING:
                continue
            while len(self._active_tasks) >= self._max_concurrent:
                done_ids = [jid for jid, t in self._active_tasks.items() if t.done()]
                for did in done_ids:
                    del self._active_tasks[did]
                if len(self._active_tasks) >= self._max_concurrent:
                    await asyncio.sleep(0.5)
            task = asyncio.create_task(self._dispatch_job(job))
            self._active_tasks[job_id] = task

    async def _dispatch_job(self, job):
        mod = self.find_module_for_url(job.url)

        # Catch-all fallback: if no module claimed the URL, probe with yt-dlp
        if mod is None and job.module == DownloadModule.UNKNOWN:
            from omnidownloader.modules.media_extractor import MediaExtractor
            # Find existing MediaExtractor to get its configured paths
            existing: MediaExtractor | None = self.find_module_by_type(MediaExtractor)  # type: ignore[assignment]
            ytdlp = existing._ytdlp if existing else "yt-dlp"
            ffmpeg = existing._ffmpeg if existing else "ffmpeg"
            logger.info("No module matched URL, probing with yt-dlp: %s", job.url)
            job.state = DownloadState.EXTRACTING
            self.job_state_changed.emit(job.id, job.state.value)
            is_media = await MediaExtractor.probe_url(job.url, ytdlp)
            if is_media:
                if existing:
                    mod = existing
                else:
                    mod = MediaExtractor(ytdlp_path=ytdlp, ffmpeg_path=ffmpeg)
                job.module = DownloadModule.MEDIA
                logger.info("yt-dlp probe succeeded — routing to MediaExtractor")
            else:
                job.state = DownloadState.FAILED
                job.error_message = "No module can handle this URL and yt-dlp does not support it."
                self.job_state_changed.emit(job.id, job.state.value)
                return

        if mod is None:
            job.state = DownloadState.FAILED
            job.error_message = "No module can handle this URL."
            self.job_state_changed.emit(job.id, job.state.value)
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

            # Only show format dialog if we have formats
            if job.metadata.get("formats"):
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
            if job.state != DownloadState.CANCELLED:
                job.state = DownloadState.COMPLETED
                job.completed_at = time.monotonic()
                self.job_state_changed.emit(job.id, job.state.value)
                self.job_progress.emit(job.id, 100.0, 0.0)
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

    async def _speed_reporter(self):
        while True:
            await asyncio.sleep(1.0)
            total = sum(j.speed_bps for j in self.active_jobs())
            self.global_speed_update.emit(total)

    def pause_all_active_jobs(self) -> None:
        """Pause all active downloads — called by kill switch."""
        logger.critical("Kill switch: pausing ALL active downloads")
        for job_id, job in self._jobs.items():
            if job.state == DownloadState.DOWNLOADING:
                job.state = DownloadState.PAUSED
                self.job_state_changed.emit(job_id, job.state.value)
                mod = self.find_module_for_url(job.url)
                if mod:
                    asyncio.ensure_future(mod.pause(job))
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
        if mod:
            import omnidownloader.main as main_mod
            loop = main_mod._loop
            if loop and loop.is_running():
                loop.call_soon_threadsafe(asyncio.ensure_future, mod.cancel(job))
        job.state = DownloadState.CANCELLED
        self.job_state_changed.emit(job_id, job.state.value)
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

"""Abstract base class that every download module must implement."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

from omnidownloader.core.models import DownloadJob


class BaseDownloaderModule(ABC):
    """Interface contract for all download provider modules.

    Every module (HTTP, Torrent, Media, Image) must subclass this
    and implement the three abstract methods below.  The download
    manager routes URLs to the first module whose ``can_handle``
    returns *True*.
    """

    # Human-readable name shown in the UI (e.g. "Direct HTTP Download")
    MODULE_NAME: str = "BaseModule"

    # ── URL routing ───────────────────────────────────────────

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """Return *True* if this module can process *url*."""
        ...

    # ── Metadata extraction ───────────────────────────────────

    @abstractmethod
    async def extract_metadata(self, url: str) -> dict[str, Any]:
        """Probe *url* and return metadata.

        Expected keys (module-dependent):
          - ``title``          – human-readable name
          - ``file_size``      – total bytes (-1 if unknown)
          - ``thumbnail``      – preview image URL (optional)
          - ``formats``        – list of available formats (media)
          - ``files``          – list of files in a torrent bundle
          - ``images``         – list of image URLs (image scraper)

        This method should be *fast* and side-effect-free.
        """
        ...

    # ── Download ──────────────────────────────────────────────

    @abstractmethod
    async def start_download(
        self,
        job: DownloadJob,
        progress_callback: Optional[Callable] = None,
    ) -> None:
        """Begin downloading.  Must update *job* state and progress.

        The implementation is responsible for:
          1. Setting ``job.state = DownloadState.DOWNLOADING``
          2. Periodically calling ``progress_callback(job)``
          3. Setting ``job.state = DownloadState.COMPLETED`` on success
          4. Pre-allocating disk space via ``disk_utils.preallocate_file``
          5. Using ``RAMRingBuffer`` for buffered writes
        """
        ...

    # ── Optional lifecycle hooks ──────────────────────────────

    async def pause(self, job: DownloadJob) -> None:
        """Pause an in-progress download (default: no-op)."""

    async def resume(self, job: DownloadJob) -> None:
        """Resume a paused download (default: no-op)."""

    async def cancel(self, job: DownloadJob) -> None:
        """Cancel a download and clean up partial files (default: no-op)."""

    # ── Utility ───────────────────────────────────────────────

    def display_name(self) -> str:
        return self.MODULE_NAME

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} ({self.MODULE_NAME})>"

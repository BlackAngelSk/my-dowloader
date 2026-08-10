"""Core data models for OmniDownloader."""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


class DownloadState(enum.Enum):
    """Lifecycle states for a download job."""
    PENDING = "pending"
    EXTRACTING = "extracting"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    MERGING = "merging"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DownloadModule(enum.Enum):
    """Which engine module handles this job."""
    HTTP = "http"
    TORRENT = "torrent"
    MEDIA = "media"
    IMAGE = "image"
    UNKNOWN = "unknown"


class Priority(enum.Enum):
    """Priority levels for the dynamic bandwidth scheduler."""
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


# Bandwidth weights for priority-based allocation
PRIORITY_WEIGHTS = {
    Priority.HIGH: 5,
    Priority.NORMAL: 2,
    Priority.LOW: 1,
}


@dataclass
class SegmentProgress:
    """Tracks per-segment (chunk) download progress."""
    segment_index: int
    start_byte: int
    end_byte: int
    downloaded_bytes: int = 0
    speed_bps: float = 0.0
    completed: bool = False


@dataclass
class DownloadJob:
    """Represents a single download task managed by the DownloadManager."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    url: str = ""
    module: DownloadModule = DownloadModule.UNKNOWN
    state: DownloadState = DownloadState.PENDING
    error_message: Optional[str] = None

    # File info
    file_name: str = ""
    file_path: str = ""
    file_size: int = 0
    downloaded_bytes: int = 0

    # Speed tracking
    speed_bps: float = 0.0
    avg_speed_bps: float = 0.0
    _speed_samples: list[float] = field(default_factory=list, repr=False)
    _start_time: float = field(default_factory=time.monotonic, repr=False)

    # Segments (HTTP chunked only)
    segments: list[SegmentProgress] = field(default_factory=list)
    thread_count: int = 0

    # Configuration
    max_speed_bps: float = 0.0
    sequential: bool = False
    priority: Priority = Priority.NORMAL

    # Metadata

    def update_speed(self, speed: float) -> None:
        """Update instantaneous speed and keep a rolling average."""
        self.speed_bps = speed
        self._speed_samples.append(speed)
        if len(self._speed_samples) > 30:
            self._speed_samples.pop(0)
        self.avg_speed_bps = (
            sum(self._speed_samples) / len(self._speed_samples)
            if self._speed_samples
            else 0.0
        )

    def mark_downloaded(self, byte_count: int) -> None:
        self.downloaded_bytes += byte_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "url": self.url,
            "module": self.module.value, "state": self.state.value,
            "error_message": self.error_message,
            "file_name": self.file_name, "file_path": self.file_path,
            "file_size": self.file_size, "downloaded_bytes": self.downloaded_bytes,
            "speed_bps": self.speed_bps, "thread_count": self.thread_count,
            "priority": self.priority.value,
            "sequential": self.sequential,
            "metadata": self.metadata, "created_at": self.created_at,
            "started_at": self.started_at, "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DownloadJob:
        return cls(
            id=data.get("id", uuid.uuid4().hex[:12]),
            url=data.get("url", ""),
            module=DownloadModule(data.get("module", "unknown")),
            state=DownloadState(data.get("state", "pending")),
            error_message=data.get("error_message"),
            file_name=data.get("file_name", ""),
            file_path=data.get("file_path", ""),
            file_size=data.get("file_size", 0),
            downloaded_bytes=data.get("downloaded_bytes", 0),
            thread_count=data.get("thread_count", 0),
            priority=Priority(data.get("priority", "normal")),
            sequential=data.get("sequential", False),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", time.time()),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
        )

    metadata: dict[str, Any] = field(default_factory=dict)

    # Timestamps
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None

    # Callbacks
    progress_callback: Any = field(default=None, repr=False)
    state_callback: Any = field(default=None, repr=False)

    # Streaming
    streaming_buffer: Any = field(default=None, repr=False)

    @property
    def progress_percent(self) -> float:
        if self.file_size <= 0:
            return 0.0
        return min(100.0, (self.downloaded_bytes / self.file_size) * 100.0)

    @progress_percent.setter
    def progress_percent(self, value: float) -> None:
        """Set progress by computing downloaded_bytes from percentage."""
        if self.file_size > 0:
            self.downloaded_bytes = int(value / 100.0 * self.file_size)

    @property
    def eta_seconds(self) -> Optional[float]:
        remaining = self.file_size - self.downloaded_bytes
        if self.speed_bps <= 0 or remaining <= 0:
            return None
        return remaining / self.speed_bps

    @property
    def elapsed_seconds(self) -> float:
        start = self.started_at or self.created_at
        end = self.completed_at or time.monotonic()
        return max(0.0, end - start)

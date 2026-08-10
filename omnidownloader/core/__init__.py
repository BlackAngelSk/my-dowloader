"""Core engine: download manager, buffering, disk utilities, and data models."""

from omnidownloader.core.models import DownloadJob, DownloadState, DownloadModule
from omnidownloader.core.base_module import BaseDownloaderModule
from omnidownloader.core.download_manager import DownloadManager
from omnidownloader.core.ram_buffer import RAMRingBuffer
from omnidownloader.core.disk_utils import preallocate_file

__all__ = [
    "DownloadJob",
    "DownloadState",
    "DownloadModule",
    "BaseDownloaderModule",
    "DownloadManager",
    "RAMRingBuffer",
    "preallocate_file",
]

"""Streaming Buffer — read API for partially-downloaded files.

Provides a thread-safe way for the in-app media player to read data
from an incomplete file + RAM ring buffer without locking the file handle.

The player polls ``available_bytes()`` and calls ``read()`` to get data
that has been flushed to disk (or is still in the ring buffer).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class StreamingBuffer:
    """Read-side API for an in-progress download.

    Parameters
    ----------
    file_path : str
        Path to the partially-downloaded file.
    file_size : int
        Total expected size of the file (-1 if unknown).
    ring_buffer : RAMRingBuffer | None
        Reference to the write-side ring buffer for reading in-RAM data.
    """

    def __init__(
        self,
        file_path: str,
        file_size: int = 0,
        ring_buffer=None,
    ) -> None:
        self._file_path = file_path
        self._file_size = file_size
        self._ring = ring_buffer
        self._highest_byte: int = 0
        self._lock = threading.Lock()
        self._complete = False

    @property
    def file_path(self) -> str:
        return self._file_path

    @property
    def file_size(self) -> int:
        return self._file_size

    def update_progress(self, downloaded_bytes: int) -> None:
        """Called by the download loop to update how far data is written."""
        with self._lock:
            self._highest_byte = max(self._highest_byte, downloaded_bytes)

    def mark_complete(self) -> None:
        with self._lock:
            self._complete = True
            self._highest_byte = self._file_size

    def available_bytes(self) -> int:
        """Number of bytes that can be read (from offset 0)."""
        with self._lock:
            return self._highest_byte

    def is_complete(self) -> bool:
        return self._complete

    def read(self, offset: int, length: int) -> bytes:
        """Read *length* bytes starting at *offset* from the partial file.

        Returns fewer bytes than requested if we haven't downloaded that
        far yet.  Returns empty bytes if offset is beyond available data.
        """
        with self._lock:
            avail = self._highest_byte

        if offset >= avail:
            return b""

        # Clamp read to available data
        end = min(offset + length, avail)
        read_len = end - offset

        try:
            with open(self._file_path, "rb") as f:
                f.seek(offset)
                data = f.read(read_len)
            return data
        except (OSError, FileNotFoundError) as exc:
            logger.warning("StreamingBuffer read failed: %s", exc)
            return b""

    def read_all_available(self) -> bytes:
        """Read everything available from offset 0."""
        return self.read(0, self._highest_byte)

    async def async_read(self, offset: int, length: int) -> bytes:
        """Async version — offloads blocking file I/O to executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.read, offset, length)

    def __repr__(self) -> str:
        return (
            f"<StreamingBuffer {self._file_path} "
            f"{self._highest_byte}/{self._file_size} bytes "
            f"{'complete' if self._complete else 'in-progress'}>"
        )

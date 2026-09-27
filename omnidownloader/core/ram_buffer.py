"""RAM Ring Buffer for high-speed disk I/O buffering.

Accumulates incoming stream packets into a contiguous memory buffer
(default 16 MB – 64 MB) before issuing a single sequential disk write,
preventing disk thrashing on high-speed connections.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# 16 MB default, max 64 MB
DEFAULT_BUFFER_SIZE = 16 * 1024 * 1024
MAX_BUFFER_SIZE = 64 * 1024 * 1024
FLUSH_THRESHOLD_PERCENT = 90.0  # flush when buffer is this % full


class RAMRingBuffer:
    """A contiguous RAM buffer that flushes to disk when full.

    Writes are *positional*: the buffer owns the byte range starting at
    ``base_offset`` and advances through the file as it flushes, so several
    buffers can feed different regions of one file concurrently (segmented
    downloads) without corrupting each other.

    Usage::

        buf = RAMRingBuffer(file_handle, buffer_size=32 * 1024 * 1024)
        await buf.write(chunk_bytes)
        # ... more writes ...
        await buf.flush()   # force remaining data to disk
        buf.close()
    """

    def __init__(
        self,
        file_handle: int,
        buffer_size: int = DEFAULT_BUFFER_SIZE,
        flush_threshold: float = FLUSH_THRESHOLD_PERCENT,
        base_offset: int = 0,
        on_flush: "Callable[[int, int], None] | None" = None,
    ) -> None:
        self._fd = file_handle
        self._buf_size = max(1, min(buffer_size, MAX_BUFFER_SIZE))
        self._threshold = flush_threshold
        self._buffer = bytearray(self._buf_size)
        self._write_pos = 0   # next write position in buffer
        self._file_pos = base_offset  # next absolute offset in the file
        self._on_flush = on_flush     # notified with (start, end) per flush
        self._flush_lock = asyncio.Lock()
        self._total_written = 0

    @property
    def buffered_bytes(self) -> int:
        """Number of bytes currently held in RAM (not yet flushed)."""
        return self._write_pos

    @property
    def file_position(self) -> int:
        """Absolute file offset the next byte will be written at."""
        return self._file_pos

    @property
    def capacity(self) -> int:
        return self._buf_size

    @property
    def utilization_percent(self) -> float:
        return (self._write_pos / self._buf_size) * 100.0

    @property
    def total_flushed(self) -> int:
        return self._total_written

    async def write(self, data: bytes) -> None:
        """Write *data* into the ring buffer, flushing to disk when full."""
        if not data:
            return
        offset = 0
        remaining = len(data)

        while remaining > 0:
            space = self._buf_size - self._write_pos
            chunk_len = min(remaining, space)
            if chunk_len <= 0:          # buffer full — flush and retry
                await self.flush()
                continue

            # Copy data into buffer
            self._buffer[self._write_pos : self._write_pos + chunk_len] = (
                data[offset : offset + chunk_len]
            )
            self._write_pos += chunk_len
            offset += chunk_len
            remaining -= chunk_len

            # Auto-flush once the buffer passes the threshold
            if self._write_pos >= int(self._buf_size * self._threshold / 100.0):
                await self.flush()

    async def flush(self) -> int:
        """Write buffered data to disk and reset the buffer.

        Returns the number of bytes flushed.
        """
        async with self._flush_lock:
            if self._write_pos == 0:
                return 0

            bytes_to_write = self._write_pos
            file_offset = self._file_pos
            # Copy out under the lock so a concurrent write() can't mutate the
            # region while the executor thread reads it.
            data = bytes(memoryview(self._buffer)[:bytes_to_write])

            # Offload the blocking write to the thread pool
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._blocking_write, file_offset, data
            )

            self._file_pos += bytes_to_write
            self._total_written += bytes_to_write
            self._write_pos = 0

            if self._on_flush is not None:
                try:
                    self._on_flush(file_offset, file_offset + bytes_to_write)
                except Exception:  # noqa: BLE001 - observers must not break I/O
                    logger.exception("on_flush callback failed")

            logger.debug(
                "Flushed %d bytes to disk at offset %d (total: %d)",
                bytes_to_write, file_offset, self._total_written,
            )
            return bytes_to_write

    def _blocking_write(self, offset: int, data: bytes) -> None:
        """Perform the actual blocking OS write at *offset*.

        ``os.write`` may write fewer bytes than requested (signals, pipes,
        device limits), so loop until everything is on disk.  ``os.pwrite``
        is used when available because it ignores — and never disturbs — the
        shared file offset, which is what makes concurrent segmented writes
        safe.
        """
        pwrite = getattr(os, "pwrite", None)
        view = memoryview(data)
        written = 0
        while written < len(view):
            if pwrite is not None:
                n = pwrite(self._fd, view[written:], offset + written)
            else:
                # Windows: this buffer owns its own file descriptor, so
                # seeking it cannot race with another segment's buffer.
                os.lseek(self._fd, offset + written, os.SEEK_SET)
                n = os.write(self._fd, view[written:])
            if n <= 0:
                raise OSError(f"short write at offset {offset + written}")
            written += n

    def discard(self) -> None:
        """Drop buffered data without warning (used when a download fails).

        Failure paths delete the partial file, so flushing would be wasted
        work — and warning about it would be misleading.
        """
        self._buffer = bytearray(0)
        self._write_pos = 0

    def close(self) -> None:
        """Close the buffer (does NOT close the file descriptor).

        Unflushed data is discarded — call ``await flush()`` first.  Use
        ``discard()`` instead when dropping that data is intentional.
        """
        if self._write_pos:
            logger.warning(
                "RAMRingBuffer closed with %d unflushed bytes (offset %d)",
                self._write_pos, self._file_pos,
            )
        self._buffer = bytearray(0)
        self._write_pos = 0

    def __repr__(self) -> str:
        return (
            f"<RAMRingBuffer {self._write_pos}/{self._buf_size} bytes "
            f"({self.utilization_percent:.1f}% full, "
            f"at offset {self._file_pos}, "
            f"{self._total_written} total flushed)>"
        )


class DynamicBufferSizer:
    """Calculate optimal buffer size and thread count based on file size.

    Rules from the spec:
      - Threads = min(32, max(4, sqrt(FileSizeInMB / 50)))
      - Buffer  = clamp(16 MB … 64 MB) proportional to thread count

    With one correction: the formula always yields its 4-thread floor for
    anything under 50 MB, which used to mean four server connections and four
    buffers for a 200 KB file.  Thread count is therefore also capped by how
    many *useful* segments the file can actually be cut into.
    """

    MIN_THREADS = 4
    MAX_THREADS = 32
    MIN_BUFFER = 16 * 1024 * 1024   # 16 MB
    MAX_BUFFER = 64 * 1024 * 1024   # 64 MB
    #: Don't split a file into segments smaller than this — the per-request
    #: overhead outweighs the parallelism.
    MIN_SEGMENT_BYTES = 1 * 1024 * 1024
    #: Buffer handed to each segment (kept small so N segments can't multiply
    #: the configured total RAM budget by N).
    SEGMENT_MIN_BUFFER = 1 * 1024 * 1024
    SEGMENT_MAX_BUFFER = 8 * 1024 * 1024

    @classmethod
    def calculate_threads(cls, file_size_bytes: int) -> int:
        import math
        if file_size_bytes <= 0:
            return cls.MIN_THREADS
        size_mb = file_size_bytes / (1024 * 1024)
        threads = int(math.sqrt(size_mb / 50.0))
        threads = max(cls.MIN_THREADS, min(cls.MAX_THREADS, threads))
        # Never cut the file into uselessly small pieces.
        usable = max(1, file_size_bytes // cls.MIN_SEGMENT_BYTES)
        return max(1, min(threads, usable))

    @classmethod
    def calculate_buffer_size(cls, file_size_bytes: int) -> int:
        threads = cls.calculate_threads(file_size_bytes)
        # Scale buffer linearly between min and max with thread count
        ratio = (threads - cls.MIN_THREADS) / max(
            1, cls.MAX_THREADS - cls.MIN_THREADS
        )
        buf = cls.MIN_BUFFER + int(ratio * (cls.MAX_BUFFER - cls.MIN_BUFFER))
        return max(cls.MIN_BUFFER, min(cls.MAX_BUFFER, buf))

    @classmethod
    def calculate_segment_buffer_size(cls, total_buffer: int, segments: int) -> int:
        """Split the RAM budget across *segments* instead of multiplying it."""
        if segments <= 1:
            return max(cls.SEGMENT_MIN_BUFFER,
                       min(cls.MAX_BUFFER, total_buffer))
        per_segment = total_buffer // segments
        return max(cls.SEGMENT_MIN_BUFFER,
                   min(cls.SEGMENT_MAX_BUFFER, per_segment))

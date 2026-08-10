"""RAM Ring Buffer for high-speed disk I/O buffering.

Accumulates incoming stream packets into a contiguous memory buffer
(default 16 MB – 64 MB) before issuing a single sequential disk write,
preventing disk thrashing on high-speed connections.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# 16 MB default, max 64 MB
DEFAULT_BUFFER_SIZE = 16 * 1024 * 1024
MAX_BUFFER_SIZE = 64 * 1024 * 1024
FLUSH_THRESHOLD_PERCENT = 90.0  # flush when buffer is this % full


class RAMRingBuffer:
    """A contiguous RAM buffer that flushes to disk when full.

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
    ) -> None:
        self._fd = file_handle
        self._buf_size = min(buffer_size, MAX_BUFFER_SIZE)
        self._threshold = flush_threshold
        self._buffer = bytearray(self._buf_size)
        self._write_pos = 0   # next write position in buffer
        self._flush_lock = asyncio.Lock()
        self._total_written = 0

    @property
    def buffered_bytes(self) -> int:
        """Number of bytes currently held in RAM (not yet flushed)."""
        return self._write_pos

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
        offset = 0
        remaining = len(data)

        while remaining > 0:
            space = self._buf_size - self._write_pos
            chunk_len = min(remaining, space)

            # Copy data into buffer
            self._buffer[self._write_pos : self._write_pos + chunk_len] = (
                data[offset : offset + chunk_len]
            )
            self._write_pos += chunk_len
            offset += chunk_len
            remaining -= chunk_len

            # Auto-flush when buffer is full (at threshold)
            if self._write_pos >= int(self._buf_size * self._threshold / 100.0):
                await self.flush()

    async def flush(self) -> int:
        """Write buffered data to disk and reset the buffer.

        Returns the number of bytes flushed.
        """
        async with self._flush_lock:
            if self._write_pos == 0:
                return 0

            data_view = memoryview(self._buffer)[: self._write_pos]
            bytes_to_write = self._write_pos

            # Use asyncio to offload blocking write to thread pool
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._blocking_write, bytes_to_write
            )

            self._total_written += bytes_to_write
            self._write_pos = 0

            logger.debug(
                "Flushed %d bytes to disk (total: %d)",
                bytes_to_write,
                self._total_written,
            )
            return bytes_to_write

    def _blocking_write(self, length: int) -> None:
        """Perform the actual blocking OS write."""
        data = bytes(self._buffer[:length])
        os.write(self._fd, data)

    def close(self) -> None:
        """Close the buffer (does NOT close the file descriptor)."""
        self._buffer = bytearray(0)
        self._write_pos = 0

    def __repr__(self) -> str:
        return (
            f"<RAMRingBuffer {self._write_pos}/{self._buf_size} bytes "
            f"({self.utilization_percent:.1f}% full, "
            f"{self._total_written} total flushed)>"
        )


class DynamicBufferSizer:
    """Calculate optimal buffer size and thread count based on file size.

    Rules from the spec:
      - Threads = min(32, max(4, sqrt(FileSizeInMB / 50)))
      - Buffer  = clamp(16 MB … 64 MB) proportional to thread count
    """

    MIN_THREADS = 4
    MAX_THREADS = 32
    MIN_BUFFER = 16 * 1024 * 1024   # 16 MB
    MAX_BUFFER = 64 * 1024 * 1024   # 64 MB

    @classmethod
    def calculate_threads(cls, file_size_bytes: int) -> int:
        import math
        if file_size_bytes <= 0:
            return cls.MIN_THREADS
        size_mb = file_size_bytes / (1024 * 1024)
        threads = int(math.sqrt(size_mb / 50.0))
        return max(cls.MIN_THREADS, min(cls.MAX_THREADS, threads))

    @classmethod
    def calculate_buffer_size(cls, file_size_bytes: int) -> int:
        threads = cls.calculate_threads(file_size_bytes)
        # Scale buffer linearly between min and max with thread count
        ratio = (threads - cls.MIN_THREADS) / max(
            1, cls.MAX_THREADS - cls.MIN_THREADS
        )
        buf = cls.MIN_BUFFER + int(ratio * (cls.MAX_BUFFER - cls.MIN_BUFFER))
        return max(cls.MIN_BUFFER, min(cls.MAX_BUFFER, buf))

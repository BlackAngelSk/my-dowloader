"""Token Bucket Bandwidth Limiter.

A thread-safe, async-aware rate limiter that intercepts network chunks and
throttles speed based on configurable bytes-per-second caps.

Two-tier architecture:
  - Global limiter: single instance shared across all downloads.
  - Per-task limiter: optional per-download cap.
"""

from __future__ import annotations

import asyncio
import logging
import time
import threading

logger = logging.getLogger(__name__)


class TokenBucket:
    """Thread-safe token bucket rate limiter.

    ``rate`` is bytes/s.  ``0`` means unlimited.
    ``capacity`` defaults to *rate* (1-second burst).
    """

    def __init__(self, rate: float = 0.0, capacity: float | None = None) -> None:
        self._rate: float = rate
        self._capacity: float = capacity if capacity is not None else rate
        self._tokens: float = self._capacity
        self._last_refill: float = time.monotonic()
        self._lock: threading.Lock = threading.Lock()

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def is_unlimited(self) -> bool:
        return self._rate <= 0

    def set_rate(self, new_rate: float, burst: float | None = None) -> None:
        """Dynamically adjust the refill rate (called by scheduler)."""
        with self._lock:
            self._rate = new_rate
            self._capacity = burst if burst is not None else new_rate
            self._tokens = min(self._tokens, self._capacity)
            logger.debug("TokenBucket rate changed to %.1f KB/s", new_rate / 1024)

    def try_acquire(self, n: int) -> bool:
        """Non-blocking: consume *n* tokens if available."""
        if self.is_unlimited:
            return True
        self._refill()
        with self._lock:
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    async def acquire(self, n: int) -> None:
        """Async blocking: wait until *n* tokens are available, then deduct."""
        if self.is_unlimited or n <= 0:
            return
        while True:
            self._refill()
            with self._lock:
                if self._tokens >= n:
                    self._tokens -= n
                    return
            deficit = n - self._tokens
            wait_time = deficit / self._rate if self._rate > 0 else 0.05
            await asyncio.sleep(min(wait_time, 0.05))

    def reset(self) -> None:
        with self._lock:
            self._tokens = self._capacity
            self._last_refill = time.monotonic()

    def _refill(self) -> None:
        if self.is_unlimited:
            return
        now = time.monotonic()
        elapsed = now - self._last_refill
        with self._lock:
            self._last_refill = now
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)

    def __repr__(self) -> str:
        if self.is_unlimited:
            return "<TokenBucket unlimited>"
        return (
            f"<TokenBucket {self._tokens:.0f}/{self._capacity:.0f} "
            f"rate={self._rate / 1024:.1f} KB/s>"
        )


class BandwidthManager:
    """Manages global and per-task bandwidth limiting.

    Instantiated once by the DownloadManager and injected into download
    modules that need throttling.
    """

    def __init__(self, global_rate: float = 0.0) -> None:
        self._global = TokenBucket(rate=global_rate)
        self._task_limiters: dict[str, TokenBucket] = {}

    @property
    def global_rate(self) -> float:
        return self._global.rate

    def set_global_rate(self, rate: float) -> None:
        """Set the global download speed cap in bytes/s.  0 = unlimited."""
        self._global.set_rate(rate)
        logger.info(
            "Global bandwidth cap: %s",
            f"{rate / 1024:.1f} KB/s" if rate > 0 else "unlimited",
        )

    async def throttle_global(self, byte_count: int) -> None:
        """Block until the global bucket allows *byte_count* bytes."""
        await self._global.acquire(byte_count)

    def create_task_limiter(self, job_id: str, rate: float = 0.0) -> TokenBucket:
        bucket = TokenBucket(rate=rate)
        self._task_limiters[job_id] = bucket
        return bucket

    def set_task_rate(self, job_id: str, rate: float) -> None:
        bucket = self._task_limiters.get(job_id)
        if bucket:
            bucket.set_rate(rate)

    def remove_task_limiter(self, job_id: str) -> None:
        self._task_limiters.pop(job_id, None)

    async def throttle_task(self, job_id: str, byte_count: int) -> None:
        bucket = self._task_limiters.get(job_id)
        if bucket:
            await bucket.acquire(byte_count)

    async def throttle(self, job_id: str, byte_count: int) -> None:
        """Full throttle: blocks on BOTH global and per-task buckets."""
        await self._global.acquire(byte_count)
        bucket = self._task_limiters.get(job_id)
        if bucket:
            await bucket.acquire(byte_count)

    def allocate_for_priority(
        self, job_id: str, available_bw: float,
        weight: float, total_weight: float,
    ) -> None:
        """Assign a per-task rate based on priority weight share."""
        if available_bw <= 0:
            self.set_task_rate(job_id, 0.0)
            return
        share = (weight / total_weight) * available_bw if total_weight > 0 else available_bw
        self.set_task_rate(job_id, share)

    def clear_all_task_limiters(self) -> None:
        self._task_limiters.clear()

    def __repr__(self) -> str:
        return (
            f"<BandwidthManager global={self._global} "
            f"tasks={len(self._task_limiters)}>"
        )

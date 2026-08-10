"""Disk utility helpers — file pre-allocation and space checks.

Pre-allocating the full file size on disk prevents fragmentation
and improves sequential write performance on SSDs/NVMe drives.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def ensure_directory(path: str | Path) -> Path:
    """Create parent directories if they don't exist and return the Path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def preallocate_file(path: str | Path, size: int) -> int:
    """Pre-allocate *size* bytes on disk at *path*.

    - Linux:   uses ``fallocate`` via ``os.posix_fallocate``
    - Windows: uses ``SetFileValidData`` via ctypes
    - Fallback: writes zeros (slower but universal)

    Returns the file descriptor (caller must close).
    """
    if size <= 0:
        raise ValueError(f"Pre-allocation size must be positive, got {size}")

    ensure_directory(path)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o644)

    system = platform.system()
    try:
        if system == "Linux":
            _fallocate_linux(fd, size)
        elif system == "Windows":
            _fallocate_windows(fd, size)
        else:
            _fallocate_fallback(fd, size)
        logger.info("Pre-allocated %s (%s) for %s", _fmt_size(size), system, path)
    except OSError as exc:
        logger.warning("Pre-allocation failed, falling back to zeroing: %s", exc)
        _fallocate_fallback(fd, size)

    return fd


def _fallocate_linux(fd: int, size: int) -> None:
    """Use os.posix_fallocate (available on Linux, macOS ≥10.15)."""
    try:
        os.posix_fallocate(fd, 0, size)
    except AttributeError:
        # macOS without posix_fallocate
        os.ftruncate(fd, size)


def _fallocate_windows(fd: int, size: int) -> None:
    """Use Windows SetFileValidData via ctypes."""
    import ctypes
    import ctypes.wintypes as wt

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = _get_osfhandle(fd)
    current_pos = os.lseek(fd, 0, os.SEEK_CUR)

    class LARGE_INTEGER(ctypes.Structure):
        _fields_ = [("QuadPart", ctypes.c_int64)]

    li = LARGE_INTEGER(size)
    kernel32.SetFileValidData(handle, li)
    os.lseek(fd, current_pos, os.SEEK_SET)


def _get_osfhandle(fd: int) -> int:
    """Get Windows HANDLE from file descriptor."""
    import msvcrt
    return msvcrt.get_osfhandle(fd)


def _fallocate_fallback(fd: int, size: int) -> None:
    """Write zeros to pre-allocate (slowest but works everywhere)."""
    CHUNK = 1024 * 1024  # 1 MB chunks
    zero_block = b"\x00" * CHUNK
    written = 0
    while written < size:
        to_write = min(CHUNK, size - written)
        os.write(fd, zero_block[:to_write])
        written += to_write
    os.lseek(fd, 0, os.SEEK_SET)


def get_available_space(path: str | Path) -> int:
    """Return available disk space in bytes at *path*."""
    stat = shutil.disk_usage(str(Path(path).parent))
    return stat.free


def cleanup_partial_file(path: str | Path) -> None:
    """Remove a partial/failed download file."""
    p = Path(path)
    if p.exists():
        try:
            p.unlink()
            logger.info("Cleaned up partial file: %s", p)
        except OSError as exc:
            logger.warning("Failed to remove %s: %s", p, exc)


def _fmt_size(size: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024  # type: ignore[assignment]
    return f"{size:.1f} PB"

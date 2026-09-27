"""Disk utility helpers — file pre-allocation and space checks.

Pre-allocating the full file size on disk prevents fragmentation
and improves sequential write performance on SSDs/NVMe drives.
"""

from __future__ import annotations

import logging
import os
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

    Pre-allocation keeps the file from fragmenting and lets segmented
    downloads seek freely inside it.

    - Linux:   ``os.posix_fallocate`` (real allocation, no zeroing cost)
    - Anywhere with ftruncate: extend the file sparsely (works on Windows)
    - Last resort: write zeros (slow but universal)

    Returns the file descriptor (caller must close).
    """
    if size <= 0:
        raise ValueError(f"Pre-allocation size must be positive, got {size}")

    ensure_directory(path)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o644)

    try:
        if hasattr(os, "posix_fallocate"):
            os.posix_fallocate(fd, 0, size)
        else:
            os.ftruncate(fd, size)
        os.lseek(fd, 0, os.SEEK_SET)
        logger.info(
            "Pre-allocated %s for %s", _fmt_size(size), path,
        )
    except OSError as exc:
        # posix_fallocate fails on filesystems without support (some
        # network/NTFS mounts) — fall back to extent extension, then zeros.
        logger.warning("Fast pre-allocation failed (%s), trying ftruncate", exc)
        try:
            os.ftruncate(fd, size)
            os.lseek(fd, 0, os.SEEK_SET)
        except OSError as exc2:
            logger.warning("ftruncate failed too (%s), zero-filling", exc2)
            _fallocate_fallback(fd, size)

    return fd


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


def truncate_file(path: str | Path) -> None:
    """Truncate *path* to zero length (used to restart a download cleanly)."""
    with open(path, "wb"):
        pass


#: Sidecar holding the per-segment progress of an interrupted download.
SIDECAR_SUFFIX = ".omnidownloader.json"


def sidecar_path(target: str | Path) -> Path:
    """Path of the resume-state file that belongs to *target*."""
    return Path(str(target) + SIDECAR_SUFFIX)


def get_available_space(path: str | Path) -> int:
    """Return available disk space in bytes at *path*."""
    stat = shutil.disk_usage(str(Path(path).parent))
    return stat.free


def file_digest(path: str | Path, algorithm: str = "sha256",
                chunk_size: int = 1024 * 1024) -> str:
    """Return the hex digest of *path* (streamed; safe for huge files)."""
    import hashlib

    digest = hashlib.new(algorithm)
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_checksum(expected: str, path: str | Path | None = None) -> tuple[str, str]:
    """Split ``"sha256:<hex>"`` (or a bare hex digest) into (algorithm, hex).

    A bare digest is identified by its length, or by the file's size when
    given (``sha256sum`` files list ``<hex>  <filename>``).
    """
    text = (expected or "").strip()
    if not text:
        return "", ""
    if ":" in text:
        algo, _, value = text.partition(":")
        return algo.strip().lower(), value.strip().lower()
    # Bare hex: infer the algorithm from the length.
    value = text.split()[0].strip().lower()
    by_length = {32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}
    return by_length.get(len(value), "sha256"), value


def verify_checksum(path: str | Path, expected: str) -> tuple[bool, str]:
    """Compare *path*'s digest against *expected*.

    Returns ``(ok, message)``; unsupported algorithms or unreadable files come
    back as ``(False, reason)`` rather than raising.
    """
    import hashlib

    algorithm, want = parse_checksum(expected, path)
    if not algorithm or not want:
        return False, "no checksum supplied"
    if algorithm not in hashlib.algorithms_available:
        return False, f"unsupported checksum algorithm: {algorithm}"
    try:
        got = file_digest(path, algorithm)
    except OSError as exc:
        return False, f"could not read {path}: {exc}"
    if got == want:
        return True, f"{algorithm} matches"
    return False, f"{algorithm} mismatch: expected {want}, got {got}"


def cleanup_partial_file(path: str | Path) -> None:
    """Remove a partial/failed download (file *or* directory tree).

    Image batches and torrents use a directory as ``job.file_path``, where
    ``unlink()`` raised IsADirectoryError and the partial tree was never
    cleaned up.  The resume sidecar is removed too, so a deleted job doesn't
    leave a stale state file behind.
    """
    p = Path(path)
    try:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            logger.info("Cleaned up partial directory: %s", p)
        elif p.exists():
            p.unlink()
            logger.info("Cleaned up partial file: %s", p)
    except OSError as exc:
        logger.warning("Failed to remove %s: %s", p, exc)
    sidecar_path(p).unlink(missing_ok=True)


def _fmt_size(size: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024  # type: ignore[assignment]
    return f"{size:.1f} PB"

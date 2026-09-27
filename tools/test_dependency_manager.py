#!/usr/bin/env python3
"""Tests for DependencyManager ffmpeg install/extraction.

Covers the Windows-only bug where the ffmpeg *archive* was saved under the
binary's name, so --ffmpeg-location pointed yt-dlp at a zip.

Usage: .venv/bin/python tools/test_dependency_manager.py
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnidownloader.services.dependency_manager import DependencyManager  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def fake_binary(tag: bytes) -> bytes:
    """A plausible-looking ELF/PE payload that is NOT an archive."""
    return b"\x7fELF" + tag + b"\x00" * 4096


async def test_real_linux_download(tmp: Path) -> None:
    """Real end-to-end: download the Linux tar.xz and extract ffmpeg from it.

    ``shutil.which`` is stubbed to hide the host's system ffmpeg — otherwise
    ensure_ffmpeg() short-circuits and never exercises the download path.
    """
    real_which = shutil.which

    def no_system_ffmpeg(name, *a, **kw):
        return None if name == "ffmpeg" else real_which(name, *a, **kw)

    deps = DependencyManager(deps_dir=str(tmp / "linux"))
    with mock.patch("omnidownloader.services.dependency_manager.shutil.which",
                    side_effect=no_system_ffmpeg):
        check("system ffmpeg is hidden for this test",
              shutil.which("ffmpeg") is None)
        path = await deps.ensure_ffmpeg()
    ok = Path(path).is_file() and Path(path).stat().st_size > 1_000_000
    check("real tar.xz download extracts a real ffmpeg", ok,
          f"{Path(path).name} {Path(path).stat().st_size // 1024 // 1024} MiB")
    if ok:
        rc = subprocess.run([path, "-version"], capture_output=True, text=True)
        first = rc.stdout.splitlines()[0] if rc.stdout else ""
        check("extracted ffmpeg actually runs", rc.returncode == 0, first[:60])
    check("downloaded archive is not left behind",
          not list((tmp / "linux").glob("*.tar.xz")),
          str([p.name for p in (tmp / "linux").glob("*")]))
    check("no bogus non-binary file in deps dir",
          all(deps._is_real_binary(p) or p.is_dir() for p in (tmp / "linux").glob("*")))


def test_windows_zip_path(tmp: Path) -> None:
    """Windows path: nested gyan.dev-style zip must be flattened into deps/."""
    d = tmp / "win"
    d.mkdir(parents=True)
    archive = d / "ffmpeg.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("ffmpeg-7.0-essentials_build/bin/ffmpeg.exe", fake_binary(b"ffmpeg"))
        zf.writestr("ffmpeg-7.0-essentials_build/bin/ffprobe.exe", fake_binary(b"ffprobe"))
        zf.writestr("ffmpeg-7.0-essentials_build/LICENSE", "text")

    deps = DependencyManager(deps_dir=str(d))
    deps._system = "Windows"          # exercise the Windows branch on Linux
    found = str(deps._extract_ffmpeg(archive))
    check("windows zip: ffmpeg.exe extracted and flattened",
          Path(found).name == "ffmpeg.exe" and Path(found).parent == d, found)
    check("windows zip: ffprobe.exe extracted alongside",
          (d / "ffprobe.exe").is_file())
    check("windows zip: found by discovery",
          Path(deps.find_ffmpeg_binary_local()).name == "ffmpeg.exe")


def test_bogus_archive_named_ffmpeg(tmp: Path) -> None:
    """The historical bug's leftovers must be rejected, not trusted."""
    d = tmp / "bogus"
    d.mkdir(parents=True)
    # A tar.xz saved *as* deps/ffmpeg — exactly what the old code produced.
    jam = d / "ffmpeg"
    with tarfile.open(jam, "w:xz") as tf:
        tf.add(__file__, arcname="junk.py")
    deps = DependencyManager(deps_dir=str(d))
    check("archive saved as 'ffmpeg' is not treated as a binary",
          deps.find_ffmpeg_binary_local() == "" and not deps._is_real_binary(jam),
          f"size={jam.stat().st_size}B rejected_by_magic=True")
    check("deps dir exposes no ffmpeg for that name",
          "/usr/bin/ffmpeg" not in deps.find_ffmpeg_binary_local())


def test_missing_binary_in_archive(tmp: Path) -> None:
    """An archive without ffmpeg must raise, not silently return junk."""
    d = tmp / "empty"
    d.mkdir(parents=True)
    archive = d / "ffmpeg.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("readme.txt", "no binaries here")
    deps = DependencyManager(deps_dir=str(d))
    deps._system = "Windows"
    try:
        deps._extract_ffmpeg(archive)
        check("archive without ffmpeg raises RuntimeError", False, "no exception")
    except RuntimeError as exc:
        check("archive without ffmpeg raises RuntimeError", True, str(exc)[:70])


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="depstest-"))
    try:
        await test_real_linux_download(tmp)
        test_windows_zip_path(tmp)
        test_bogus_archive_named_ffmpeg(tmp)
        test_missing_binary_in_archive(tmp)
        failures = [n for n, ok, _ in RESULTS if not ok]
        print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
        if failures:
            print("FAILED: " + ", ".join(failures))
        return 1 if failures else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
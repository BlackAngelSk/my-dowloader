#!/usr/bin/env python3
"""Headless dependency installer: ``--install-deps`` and the aria2c download.

Everything here is offline: the archives are synthesised, the GitHub API is
pointed at a closed port, and the tools' presence is faked in a temp home, so
the whole file runs in about a second.  The real downloads are covered by
``tools/test_dependency_manager.py``.

Usage: .venv/bin/python tools/test_install_deps.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def eq(name: str, got, expected) -> None:
    check(name, got == expected, f"got {got!r}, want {expected!r}")


#: A fake executable: big enough and with no archive magic to pass
#: DependencyManager._is_real_binary().
def fake_binary(path: Path, size: int = 4096) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x7fELF" + b"\x00" * size)
    path.chmod(0o755)
    return path


def test_extract_named(tmp: Path) -> None:
    from omnidownloader.services.dependency_manager import DependencyManager

    deps = DependencyManager(deps_dir=str(tmp / "deps"))
    archive = tmp / "aria2.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("aria2-1.37.0-win-64bit-build1/aria2c.exe", b"MZ" + b"\x00" * 4096)
        zf.writestr("aria2-1.37.0-win-64bit-build1/README.txt", "docs")
        # A hostile member must not escape the deps directory.
        zf.writestr("../../escape.exe", b"MZ" + b"\x00" * 4096)

    extracted = deps._extract_named(archive, ("aria2c.exe",))
    eq("aria2c.exe is extracted", extracted, ["aria2c.exe"])
    check("extracted binary lands in the deps dir",
          (tmp / "deps" / "aria2c.exe").is_file())
    check("path traversal member is not written outside deps",
          not (tmp / "escape.exe").exists() and not (tmp.parent / "escape.exe").exists())

    missing = tmp / "empty.zip"
    with zipfile.ZipFile(missing, "w") as zf:
        zf.writestr("readme.txt", "nothing useful")
    try:
        deps._extract_named(missing, ("aria2c.exe",))
        check("an archive without the binary raises", False, "no error")
    except RuntimeError as exc:
        check("an archive without the binary raises", "aria2c.exe" in str(exc), str(exc))


def test_aria2_discovery(tmp: Path) -> None:
    from omnidownloader.core import platform_utils
    from omnidownloader.services.dependency_manager import DependencyManager

    home = tmp / "home"
    deps_dir = home / "deps"
    deps = DependencyManager(deps_dir=str(deps_dir))

    old = os.environ.get("OMNI_ARIA2C_PATH")
    os.environ.pop("OMNI_ARIA2C_PATH", None)
    real_deps_dir = platform_utils.deps_dir
    real_which = platform_utils.shutil.which
    try:
        fake_binary(deps_dir / "aria2c")
        platform_utils.deps_dir = lambda: deps_dir          # ty: ignore[invalid-assignment]
        platform_utils.shutil.which = lambda name: None     # ty: ignore[invalid-assignment]

        eq("managed aria2c in deps/ is found",
           deps.aria2c_path, str(deps_dir / "aria2c"))
        eq("platform_utils.find_aria2c agrees",
           platform_utils.find_aria2c(), str(deps_dir / "aria2c"))
        check("optional_status reports aria2c present",
              deps.optional_status()["aria2c"] is True)

        (deps_dir / "aria2c").unlink()
        eq("nothing anywhere -> empty path", deps.aria2c_path, "")
        check("optional_status reports aria2c missing",
              deps.optional_status()["aria2c"] is False)
    finally:
        platform_utils.deps_dir = real_deps_dir
        platform_utils.shutil.which = real_which
        if old is not None:
            os.environ["OMNI_ARIA2C_PATH"] = old


def test_system_ytdlp_launcher(tmp: Path) -> None:
    """A pip/pacman yt-dlp is a *script* — use it, never try to delete it."""
    import shutil as shutil_mod

    from omnidownloader.services import dependency_manager as dep_mod

    deps = dep_mod.DependencyManager(deps_dir=str(tmp / "deps_sys"))
    bin_dir = tmp / "system-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = bin_dir / "yt-dlp"
    launcher.write_text("#!/bin/sh\necho 2026.01.01\n")
    launcher.chmod(0o755)

    real_which = shutil_mod.which
    try:
        shutil_mod.which = lambda name: str(launcher) if name == "yt-dlp" else None  # ty: ignore[invalid-assignment]
        got = asyncio.run(deps.ensure_ytdlp())
        eq("a working system yt-dlp is used as-is", got, str(launcher))
        check("the system launcher is left on disk", launcher.exists())
        check("check_all agrees yt-dlp is available", deps.check_all()["yt-dlp"] is True)

        # A launcher that does not run must not lead to the app deleting a file
        # it does not own: the download is attempted instead.
        launcher.write_text("#!/bin/sh\nexit 1\n")

        async def _boom(url, dest):
            raise RuntimeError("offline in tests")

        real_download = deps._download_file
        deps._download_file = _boom  # ty: ignore[invalid-assignment]
        try:
            asyncio.run(deps.ensure_ytdlp())
            check("a broken system yt-dlp triggers a download", False, "no error")
        except RuntimeError as exc:
            check("a broken system yt-dlp triggers a download",
                  "offline in tests" in str(exc), str(exc))
        finally:
            deps._download_file = real_download
        check("a broken system launcher is not deleted", launcher.exists())
        check("check_all reports the broken launcher as unavailable",
              deps.check_all()["yt-dlp"] is False)
    finally:
        shutil_mod.which = real_which


def test_owns(tmp: Path) -> None:
    from omnidownloader.services.dependency_manager import DependencyManager

    deps = DependencyManager(deps_dir=str(tmp / "deps_own"))
    inside = fake_binary(tmp / "deps_own" / "yt-dlp")
    outside = fake_binary(tmp / "elsewhere" / "yt-dlp")
    check("a file in deps/ is owned by the app", deps._owns(inside) is True)
    check("a file outside deps/ is not owned", deps._owns(outside) is False)


def test_asset_url_fallback(tmp: Path) -> None:
    from omnidownloader.services import dependency_manager as dep_mod

    deps = dep_mod.DependencyManager(deps_dir=str(tmp / "deps2"))
    real = dep_mod.ARIA2_RELEASES_API
    try:
        # A closed port: the GitHub API lookup fails fast and must fall back to
        # the pinned release instead of aborting the install.
        dep_mod.ARIA2_RELEASES_API = "http://127.0.0.1:1/releases/latest"
        url = asyncio.run(deps._aria2_asset_url())
        eq("API failure falls back to the pinned aria2c release",
           url, dep_mod.ARIA2_PINNED_URL)
        check("the pinned URL is a win-64bit zip",
              dep_mod.ARIA2_WIN_ASSET_RE.search(dep_mod.ARIA2_PINNED_URL) is not None,
              dep_mod.ARIA2_PINNED_URL)
    finally:
        dep_mod.ARIA2_RELEASES_API = real


def run_cli(args: list[str], home: Path) -> subprocess.CompletedProcess:
    # PATH points nowhere so the tools installed on this machine cannot make
    # the "nothing installed" assertions pass by accident.
    env = dict(os.environ, OMNI_DOWNLOADER_HOME=str(home), PATH="/nonexistent-bin")
    env.pop("OMNI_ARIA2C_PATH", None)
    return subprocess.run(
        [sys.executable, "-m", "omnidownloader", *args],
        capture_output=True, text=True, timeout=180,
        cwd=str(Path(__file__).resolve().parent.parent), env=env,
    )


def test_cli(tmp: Path) -> None:
    home = tmp / "cli-home"
    (home / "deps").mkdir(parents=True, exist_ok=True)

    res = run_cli(["--install-deps", "--check"], home)
    check("--check exits 1 when nothing is installed",
          res.returncode == 1, f"exit {res.returncode}")
    check("--check names the missing tools",
          "missing" in res.stdout and "yt-dlp" in res.stdout, res.stdout.strip()[:120])

    # Fake both tools inside the managed deps dir, with PATH emptied so the
    # system ones cannot stand in.
    for name in ("yt-dlp", "ffmpeg"):
        fake_binary(home / "deps" / name)

    env_home = home
    env = dict(os.environ, OMNI_DOWNLOADER_HOME=str(env_home),
               PATH="/nonexistent-bin")
    res2 = subprocess.run(
        [sys.executable, "-m", "omnidownloader", "--install-deps", "--quick"],
        capture_output=True, text=True, timeout=180,
        cwd=str(Path(__file__).resolve().parent.parent), env=env,
    )
    check("unknown flags are rejected rather than ignored",
          res2.returncode == 2, f"exit {res2.returncode}")

    res3 = run_cli(["--install-deps", "--check"], home)
    check("--check exits 0 once both tools exist",
          res3.returncode == 0, f"exit {res3.returncode}\n{res3.stdout}")

    res4 = run_cli(["--install-deps", "--quiet"], home)
    check("--install-deps on an already-complete install exits 0",
          res4.returncode == 0, f"exit {res4.returncode}\n{res4.stdout[-300:]}")
    check("--quiet prints a single confirmation line",
          res4.stdout.strip().splitlines()[-1] == "All dependencies are ready.",
          res4.stdout.strip().splitlines()[-1] if res4.stdout.strip() else "")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="depscli-"))
    try:
        test_extract_named(tmp)
        test_aria2_discovery(tmp)
        test_system_ytdlp_launcher(tmp)
        test_owns(tmp)
        test_asset_url_fallback(tmp)
        test_cli(tmp)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    failures = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

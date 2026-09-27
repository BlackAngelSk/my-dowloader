#!/usr/bin/env python3
"""Cross-platform tests: Linux, macOS and Windows behaviour.

Nothing here needs the target OS: the platform layer is exercised directly and
the OS-specific bundles are simulated (monkeypatched ``platform.system`` /
``platform.machine``), which is how the Windows and macOS paths get tested from
a Linux box.  A static scan at the end fails the suite if platform-specific
code creeps back into the modules.

Usage: .venv/bin/python tools/test_cross_platform.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnidownloader.core import platform_utils  # noqa: E402
from omnidownloader.services import dependency_manager as depmod  # noqa: E402
from omnidownloader.services.dependency_manager import (  # noqa: E402
    FFMPEG_URLS, DependencyManager,
)

RESULTS: list[tuple[str, bool, str]] = []
ALL_SYSTEMS = (platform_utils.LINUX, platform_utils.MACOS, platform_utils.WINDOWS)
TOOLS = ("ffmpeg", "yt-dlp", "tor", "aria2c", "libtorrent")


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


class fake_platform:
    """Pretend to be another OS/CPU for the duration of a block."""

    def __init__(self, system: str, machine: str) -> None:
        self.system, self.machine = system, machine

    def __enter__(self) -> None:
        self._real = (platform_utils.platform.system, platform_utils.platform.machine)
        platform_utils.platform.system = lambda: self.system
        platform_utils.platform.machine = lambda: self.machine

    def __exit__(self, *exc: object) -> None:
        platform_utils.platform.system, platform_utils.platform.machine = self._real


# ── platform identity ───────────────────────────────────────────────

def test_identity() -> None:
    check("platform is one of the three supported ones",
          platform_utils.system() in ALL_SYSTEMS, platform_utils.describe())
    check("arch is normalised", platform_utils.arch() in ("x86_64", "arm64", "x86"),
          platform_utils.arch())

    with fake_platform(platform_utils.WINDOWS, "AMD64"):
        check("Windows is detected", platform_utils.is_windows() and not platform_utils.is_macos())
        check("Windows spawn kwargs suppress the console window",
              platform_utils.subprocess_kwargs()["creationflags"] & 0x08000000 != 0,
              str(platform_utils.subprocess_kwargs()))
    with fake_platform(platform_utils.MACOS, "arm64"):
        check("macOS is detected", platform_utils.is_macos())
        check("Apple silicon maps to arm64", platform_utils.arch() == "arm64")
        check("macOS spawn kwargs start a new session",
              platform_utils.subprocess_kwargs().get("start_new_session") is True)
    with fake_platform(platform_utils.LINUX, "aarch64"):
        check("Linux arm64 maps to arm64", platform_utils.arch() == "arm64")


# ── file names ─────────────────────────────────────────────────────

def test_safe_filename() -> None:
    check("path traversal is stripped",
          platform_utils.safe_filename("../../etc/passwd") == "passwd",
          platform_utils.safe_filename("../../etc/passwd"))
    check("windows separators are stripped",
          platform_utils.safe_filename(r"..\..\Windows\System32\evil.exe") == "evil.exe")
    check("reserved characters are removed",
          platform_utils.safe_filename('a<b>c:d"e|f?g*h') == "abcdefgh",
          platform_utils.safe_filename('a<b>c:d"e|f?g*h'))
    check("windows reserved device names are defused",
          platform_utils.safe_filename("CON.mp4") == "_CON.mp4",
          platform_utils.safe_filename("CON.mp4"))
    check("trailing dots/spaces are dropped (Windows rejects them)",
          platform_utils.safe_filename("name. ") == "name",
          repr(platform_utils.safe_filename("name. ")))
    check("control characters are dropped",
          platform_utils.safe_filename("bad\x00\x1bname") == "badname")
    long_name = "x" * 400 + ".mp4"
    capped = platform_utils.safe_filename(long_name)
    check("long names are capped for the Windows 260-char path limit",
          len(capped) <= 120 and capped.endswith(".mp4"), f"{len(capped)} chars")
    check("empty input falls back to a usable name",
          platform_utils.safe_filename("") == "download")
    check("unicode names survive",
          platform_utils.safe_filename("Pesnička — časť 2.mp3") == "Pesnička — časť 2.mp3",
          platform_utils.safe_filename("Pesnička — časť 2.mp3"))


# ── data directories ───────────────────────────────────────────────

def test_data_dirs(tmp: Path) -> None:
    target = tmp / "custom-home"
    previous = os.environ.get("OMNI_DOWNLOADER_HOME")
    os.environ["OMNI_DOWNLOADER_HOME"] = str(target)
    try:
        check("OMNI_DOWNLOADER_HOME overrides the data dir",
              platform_utils.user_data_dir() == target, str(platform_utils.user_data_dir()))
        check("subdirs are created under the override",
              platform_utils.logs_dir().is_dir() and platform_utils.deps_dir().is_dir()
              and platform_utils.plugins_dir().is_dir() and platform_utils.tor_dir().is_dir())

        from omnidownloader.services.config_store import ConfigStore

        store = ConfigStore()
        check("ConfigStore follows the platform data dir",
              str(store.path).startswith(str(target)), str(store.path))
    finally:
        if previous is None:
            os.environ.pop("OMNI_DOWNLOADER_HOME", None)
        else:
            os.environ["OMNI_DOWNLOADER_HOME"] = previous


# ── per-platform download sources ──────────────────────────────────

def test_dependency_sources(tmp: Path) -> None:
    with fake_platform(platform_utils.WINDOWS, "x86_64"):
        deps = DependencyManager(deps_dir=str(tmp / "win"))
        check("Windows yt-dlp asset is the .exe",
              deps._ytdlp_url().endswith("yt-dlp.exe"), deps._ytdlp_url())
        check("Windows binary name gets .exe", deps._binary_name("yt-dlp") == "yt-dlp.exe")
        sources = deps._ffmpeg_sources()
        check("Windows ffmpeg source is a zip", sources and sources[0][1] == "zip")
        check("Windows ffmpeg archive provides ffmpeg and ffprobe",
              set(sources[0][2]) == {"ffmpeg", "ffprobe"}, str(sources[0][2]))

    with fake_platform(platform_utils.LINUX, "arm64"):
        deps = DependencyManager(deps_dir=str(tmp / "lin-arm"))
        check("Linux arm64 gets the aarch64 yt-dlp build",
              "aarch64" in deps._ytdlp_url(), deps._ytdlp_url())
        check("Linux arm64 gets the arm64 ffmpeg build",
              "arm64" in deps._ffmpeg_sources()[0][0], deps._ffmpeg_sources()[0][0])
        check("Linux ffmpeg source is a tar.xz",
              deps._ffmpeg_sources()[0][1] == "tar.xz")

    with fake_platform(platform_utils.LINUX, "x86_64"):
        deps = DependencyManager(deps_dir=str(tmp / "lin-x64"))
        check("Linux x86_64 gets the amd64 yt-dlp build",
              deps._ytdlp_url().endswith("yt-dlp_linux"), deps._ytdlp_url())

    with fake_platform(platform_utils.MACOS, "arm64"):
        deps = DependencyManager(deps_dir=str(tmp / "mac"))
        check("macOS gets the universal yt-dlp build",
              deps._ytdlp_url().endswith("yt-dlp_macos"), deps._ytdlp_url())
        check("macOS binary name has no .exe", deps._binary_name("yt-dlp") == "yt-dlp")
        sources = deps._ffmpeg_sources()
        check("macOS downloads ffmpeg and ffprobe separately", len(sources) == 2,
              f"{len(sources)} sources")
        check("macOS sources are both zips", all(s[1] == "zip" for s in sources))
        check("macOS sources are never pinned to a dead version",
              all("getrelease" in s[0] for s in sources),
              "; ".join(s[0] for s in sources))

    check("every platform has at least one ffmpeg source",
          all(bool(FFMPEG_URLS.get(s)) for s in ALL_SYSTEMS),
          str([s for s in ALL_SYSTEMS if not FFMPEG_URLS.get(s)]))
    check("every tool has an install hint on every platform",
          all(_hint_for(tool, s) for tool in TOOLS for s in ALL_SYSTEMS),
          str([(t, s) for t in TOOLS for s in ALL_SYSTEMS if not _hint_for(t, s)]))


def _hint_for(tool: str, system: str) -> str:
    """install_hint() for a simulated platform (it is platform-keyed)."""
    with fake_platform(system, "x86_64"):
        return platform_utils.install_hint(tool)


# ── extraction of the simulated platform archives ──────────────────

def test_extraction(tmp: Path) -> None:
    """The Windows zip and the macOS two-archive layout, on this machine."""
    with fake_platform(platform_utils.WINDOWS, "x86_64"):
        deps = DependencyManager(deps_dir=str(tmp / "win-extract"))
        archive = tmp / "ffmpeg-release-essentials.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("ffmpeg-7.0-essentials_build/bin/ffmpeg.exe",
                        b"MZ" + b"\x00" * 4096)
            zf.writestr("ffmpeg-7.0-essentials_build/bin/ffprobe.exe",
                        b"MZ" + b"\x00" * 4096)
        deps._extract_ffmpeg(archive, ("ffmpeg", "ffprobe"))
        check("Windows zip is flattened into the deps dir",
              (deps._deps_dir / "ffmpeg.exe").is_file()
              and (deps._deps_dir / "ffprobe.exe").is_file(),
              str(sorted(p.name for p in deps._deps_dir.iterdir())))
        check("Windows: extracted binaries are recognised",
              deps._is_real_binary(deps._deps_dir / "ffmpeg.exe"))

    with fake_platform(platform_utils.MACOS, "arm64"):
        deps = DependencyManager(deps_dir=str(tmp / "mac-extract"))
        # macOS download #1: ffmpeg only.
        first = tmp / "ffmpeg.zip"
        with zipfile.ZipFile(first, "w") as zf:
            zf.writestr("ffmpeg", b"\xcf\xfa\xed\xfe" + b"\x00" * 4096)  # Mach-O magic
        deps._extract_ffmpeg(first, ("ffmpeg",))
        # macOS download #2: ffprobe only — must not be treated as a failure
        # just because no ffmpeg came out of it.
        second = tmp / "ffprobe.zip"
        with zipfile.ZipFile(second, "w") as zf:
            zf.writestr("ffprobe", b"\xcf\xfa\xed\xfe" + b"\x00" * 4096)
        try:
            deps._extract_ffmpeg(second, ("ffprobe",))
            ok = (deps._deps_dir / "ffprobe").is_file()
            detail = str(sorted(p.name for p in deps._deps_dir.iterdir()))
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        check("macOS ffprobe-only archive extracts cleanly", ok, detail)

    with fake_platform(platform_utils.LINUX, "x86_64"):
        deps = DependencyManager(deps_dir=str(tmp / "lin-extract"))
        archive = tmp / "ffmpeg-release-amd64-static.tar.xz"
        payload = tmp / "ffmpeg-7.0.2-amd64-static"
        payload.mkdir()
        (payload / "ffmpeg").write_bytes(b"\x7fELF" + b"\x00" * 4096)
        (payload / "ffprobe").write_bytes(b"\x7fELF" + b"\x00" * 4096)
        with tarfile.open(archive, "w:xz") as tf:
            tf.add(payload / "ffmpeg", arcname="ffmpeg-7.0.2-amd64-static/ffmpeg")
            tf.add(payload / "ffprobe", arcname="ffmpeg-7.0.2-amd64-static/ffprobe")
        deps._extract_ffmpeg(archive, ("ffmpeg", "ffprobe"))
        check("Linux tar.xz is flattened into the deps dir",
              (deps._deps_dir / "ffmpeg").is_file()
              and (deps._deps_dir / "ffprobe").is_file())


# ── process control ────────────────────────────────────────────────

def test_process_control() -> None:
    check("suspending an impossible pid fails gracefully",
          platform_utils.suspend_process(999_999_999) is False)
    check("resuming an impossible pid fails gracefully",
          platform_utils.resume_process(999_999_999) is False)
    check("kill_process_tree tolerates a dead pid",
          platform_utils.kill_process_tree(999_999_999) is None)

    # A child in its own session: the whole group must die.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            **platform_utils.subprocess_kwargs())
    platform_utils.kill_process_tree(proc)
    try:
        proc.wait(timeout=10)
        check("a spawned child in its own session is killed", True,
              f"exit={proc.returncode}")
    except subprocess.TimeoutExpired:
        proc.kill()
        check("a spawned child in its own session is killed", False, "still alive")

    if platform_utils.is_posix():
        # Safety: a child that shares *our* process group must not make us
        # signal ourselves.
        shared = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            platform_utils.kill_process_tree(shared)
            shared.wait(timeout=10)
            check("killing a same-group child does not kill the test runner", True)
        except subprocess.TimeoutExpired:
            shared.kill()
            check("killing a same-group child does not kill the test runner", False,
                  "child survived")


# ── tool discovery ─────────────────────────────────────────────────

def test_tool_discovery() -> None:
    tor = platform_utils.find_tor_binary()
    check("find_tor_binary returns an existing file or nothing",
          tor == "" or Path(tor).is_file(), tor or "(not installed)")
    aria = platform_utils.find_aria2c()
    check("find_aria2c returns an existing file or nothing",
          aria == "" or Path(aria).is_file(), aria or "(not installed)")

    with fake_platform(platform_utils.WINDOWS, "x86_64"):
        candidates = platform_utils._windows_program_dirs()
        check("Windows program dirs come from the environment",
              all(isinstance(p, Path) for p in candidates))

    # open_path must never raise, even for a missing target.
    missing = Path(tempfile.gettempdir()) / "definitely-not-here-omni"
    try:
        result = platform_utils.open_path(missing)
        check("open_path on a missing path does not raise", result in (True, False),
              str(result))
    except Exception as exc:  # noqa: BLE001
        check("open_path on a missing path does not raise", False,
              f"{type(exc).__name__}: {exc}")


# ── portability regression scan ────────────────────────────────────

#: Unix-only calls: each may only appear behind a guard.
UNIX_ONLY = ("os.pwrite(", "os.pread(", "os.fork(", "os.posix_fallocate(",
             "signal.SIGSTOP", "signal.SIGCONT", "os.killpg(", "os.getpgid(")

#: Hardcoded launcher paths that belong in platform_utils only.
LAUNCHER_LITERALS = ('"/usr/bin/tor"', '["xdg-open"', '"cmd", "/c", "start"',
                     '"/opt/homebrew/bin"')


def test_portability_scan() -> None:
    root = Path(__file__).resolve().parent.parent / "omnidownloader"
    offenders: list[str] = []
    launcher_offenders: list[str] = []
    platform_branches: list[str] = []

    for path in sorted(root.rglob("*.py")):
        if path.name == "platform_utils.py":
            continue
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for idx, line in enumerate(lines):
            for token in UNIX_ONLY:
                if token in line:
                    # The guard may be on this line or a few lines above.
                    window = "\n".join(lines[max(0, idx - 6):idx + 1])
                    if not any(g in window for g in
                               ("getattr(", "hasattr(", "is_posix()", "is_windows()",
                                "platform_utils.")):
                        offenders.append(f"{path.name}:{idx + 1} {token}")
            for literal in LAUNCHER_LITERALS:
                if literal in line:
                    launcher_offenders.append(f"{path.name}:{idx + 1}")
            if "sys.platform ==" in line or "os.name ==" in line:
                platform_branches.append(f"{path.name}:{idx + 1}")

    check("no unguarded Unix-only API calls", not offenders, "; ".join(offenders[:4]))
    check("launcher paths live only in platform_utils", not launcher_offenders,
          "; ".join(launcher_offenders[:4]))
    check("no ad-hoc platform branches outside the platform layer",
          not platform_branches, "; ".join(platform_branches[:5]))


def test_spawn_kwargs_applied() -> None:
    """Every asyncio subprocess spawn must use the platform kwargs."""
    root = Path(__file__).resolve().parent.parent / "omnidownloader"
    missing: list[str] = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "create_subprocess_exec" not in text:
            continue
        lines = text.splitlines()
        for idx, line in enumerate(lines):
            if "create_subprocess_exec(" in line:
                window = "\n".join(lines[idx:idx + 6])
                if "subprocess_kwargs()" not in window:
                    missing.append(f"{path.name}:{idx + 1}")
    check("every child process uses the platform spawn kwargs", not missing,
          "; ".join(missing))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="xplatform-"))
    try:
        test_identity()
        test_safe_filename()
        test_data_dirs(tmp)
        test_dependency_sources(tmp)
        test_extraction(tmp)
        test_process_control()
        test_tool_discovery()
        test_portability_scan()
        test_spawn_kwargs_applied()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failures = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

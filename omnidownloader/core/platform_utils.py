"""The single place where OmniDownloader talks to the operating system.

Everything platform-specific lives here so the rest of the codebase stays
portable: no scattered ``sys.platform`` branches, no Unix-only calls guarded ad
hoc at each call site, and one answer for "where do files live", "how do I run
a child process without an empty console window", "how do I stop a whole
process tree" and "how do I reveal a finished file".

Supported platforms: Linux, macOS, Windows.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

WINDOWS = "Windows"
MACOS = "Darwin"
LINUX = "Linux"

#: Don't flash an empty console window for every child process on Windows —
#: a GUI app spawning yt-dlp used to pop a black box on screen each time.
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

#: Windows refuses these as file names, with or without an extension.
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

#: Characters no filesystem here accepts (Windows is the strictest).
UNSAFE_FILENAME_CHARS = '<>:"/\\|?*'


# ── platform identity ───────────────────────────────────────────────

def system() -> str:
    """``Linux`` / ``Darwin`` / ``Windows``."""
    return platform.system()


def is_windows() -> bool:
    return system() == WINDOWS


def is_macos() -> bool:
    return system() == MACOS


def is_linux() -> bool:
    return system() == LINUX


def is_posix() -> bool:
    return os.name == "posix"


def arch() -> str:
    """Normalised CPU architecture: ``x86_64`` / ``arm64`` / ``x86``."""
    machine = (platform.machine() or "").lower()
    if machine in ("x86_64", "amd64", "x64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("i386", "i686", "x86"):
        return "x86"
    return machine or "x86_64"


def describe() -> str:
    return f"{system()} {arch()} · Python {platform.python_version()}"


def has_pwrite() -> bool:
    """``os.pwrite`` exists on POSIX only (Windows uses lseek+write per fd)."""
    return hasattr(os, "pwrite")


# ── where things live ───────────────────────────────────────────────

def user_data_dir() -> Path:
    """Root for config, history, logs, plugins, deps and tor state.

    Deliberately one dot-directory in the home folder on every platform: a
    single documented location, no migration surprises between platforms, and
    it keeps working when the app is frozen.  Override with the
    ``OMNI_DOWNLOADER_HOME`` environment variable.
    """
    override = os.environ.get("OMNI_DOWNLOADER_HOME", "").strip()
    root = Path(override).expanduser() if override else Path.home() / ".omnidownloader"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - unwritable home
        logger.warning("Could not create %s: %s", root, exc)
    return root


def _subdir(name: str) -> Path:
    path = user_data_dir() / name
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover
        logger.warning("Could not create %s: %s", path, exc)
    return path


def logs_dir() -> Path:
    return _subdir("logs")


def deps_dir() -> Path:
    return _subdir("deps")


def plugins_dir() -> Path:
    return _subdir("plugins")


def tor_dir() -> Path:
    return _subdir("tor")


def config_path() -> Path:
    return user_data_dir() / "config.json"


def history_path() -> Path:
    return user_data_dir() / "history.json"


# ── child processes ─────────────────────────────────────────────────

def subprocess_kwargs() -> dict:
    """Kwargs that keep child processes well-behaved on each platform.

    * Windows: no console window, and its own process group so it can be
      killed as a tree.
    * POSIX: its own session, so ``kill_process_tree`` can signal everything
      the child spawned (yt-dlp spawns ffmpeg, tor spawns nothing, aria2c may
      spawn nothing — but the tree kill must not hit *us*).
    """
    if is_windows():
        return {"creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_process_tree(proc, timeout: float = 5.0) -> None:
    """Terminate *proc* and everything it spawned.

    Safe by construction: on POSIX the process group is only signalled when it
    is not our own group (otherwise a child that never left our group would
    make us kill ourselves).
    """
    pid = getattr(proc, "pid", proc)
    if not pid:
        return

    if is_windows():
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("taskkill %s failed: %s", pid, exc)
        return

    try:
        group = os.getpgid(pid)
    except OSError:
        group = None

    if group is not None and group != os.getpgid(0):
        try:
            os.killpg(group, signal.SIGTERM)
            return
        except OSError as exc:
            logger.debug("killpg %s failed: %s", group, exc)

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        logger.debug("kill %s failed: %s", pid, exc)


def suspend_process(pid: int) -> bool:
    """Freeze a running process (SIGSTOP, or NtSuspendProcess on Windows)."""
    if not pid:
        return False
    if is_posix():
        try:
            os.kill(pid, signal.SIGSTOP)
            return True
        except OSError as exc:
            logger.debug("SIGSTOP failed for pid %s: %s", pid, exc)
            return False
    return _windows_process_control(pid, suspend=True)


def resume_process(pid: int) -> bool:
    """Thaw a process frozen by :func:`suspend_process`."""
    if not pid:
        return False
    if is_posix():
        try:
            os.kill(pid, signal.SIGCONT)
            return True
        except OSError as exc:
            logger.debug("SIGCONT failed for pid %s: %s", pid, exc)
            return False
    return _windows_process_control(pid, suspend=False)


def _windows_process_control(pid: int, suspend: bool) -> bool:
    """ntdll NtSuspendProcess / NtResumeProcess — Windows has no SIGSTOP."""
    if not is_windows():  # pragma: no cover - only reached on Windows
        return False
    try:
        import ctypes

        PROCESS_SUSPEND_RESUME = 0x0800
        # Library names are kept in a tuple rather than used as literals:
        # PyInstaller's ctypes hook scans for WinDLL("...") strings and would
        # otherwise bundle ntdll.dll into the frozen app — a system DLL that
        # must always come from the OS being run on.
        _DLLS = ("kernel32", "ntdll")
        kernel32 = ctypes.WinDLL(_DLLS[0], use_last_error=True)  # type: ignore[attr-defined]
        ntdll = ctypes.WinDLL(_DLLS[1])  # type: ignore[attr-defined]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, int(pid))
        if not handle:
            get_last_error = getattr(ctypes, "get_last_error", lambda: 0)
            logger.debug("OpenProcess failed for pid %s (error %s)", pid,
                         get_last_error())
            return False
        try:
            fn = ntdll.NtSuspendProcess if suspend else ntdll.NtResumeProcess
            fn.argtypes = [ctypes.c_void_p]
            fn.restype = ctypes.c_long
            return fn(handle) == 0
        finally:
            kernel32.CloseHandle(handle)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Windows process suspend/resume unavailable: %s", exc)
        return False


def make_executable(path: str | Path) -> None:
    """Mark a downloaded binary executable (no-op on Windows)."""
    if not is_posix():
        return
    try:
        p = Path(path)
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    except OSError as exc:
        logger.debug("chmod +x failed for %s: %s", path, exc)


def run_quiet(cmd: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    """Run a helper command without a console window on Windows."""
    kwargs: dict = {"capture_output": True, "text": True, "timeout": timeout}
    if is_windows():
        kwargs["creationflags"] = CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs)


# ── opening things ──────────────────────────────────────────────────

def open_path(path: str | Path, reveal: bool = False) -> bool:
    """Show *path* in the desktop's file manager. Returns True when it worked.

    *reveal=True* selects the file in its folder where the platform supports
    it (macOS ``open -R``, Windows ``explorer /select,``).
    """
    target = Path(path)
    try:
        if is_windows():
            if reveal and target.exists():
                subprocess.Popen(["explorer", "/select,", str(target)],
                                 **subprocess_kwargs())
            else:
                os.startfile(str(target))  # type: ignore[attr-defined]
            return True
        if is_macos():
            cmd = ["open", "-R", str(target)] if reveal else ["open", str(target)]
            subprocess.Popen(cmd)
            return True
        # Linux: xdg-open is the standard, but it is not always installed.
        for opener in ("xdg-open", "gio", "kde-open5", "kde-open", "nautilus"):
            if shutil.which(opener):
                args = [opener, "open", str(target)] if opener == "gio" else [opener, str(target)]
                subprocess.Popen(args)
                return True
        logger.warning("No file manager launcher found (tried xdg-open, gio, …)")
        return False
    except OSError as exc:
        logger.warning("Could not open %s: %s", target, exc)
        return False


def open_url(url: str) -> bool:
    """Open a URL in the default browser."""
    import webbrowser

    try:
        return webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not open %s: %s", url, exc)
        return False


# ── tool discovery ──────────────────────────────────────────────────

def _windows_program_dirs() -> list[Path]:
    dirs: list[Path] = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
        value = os.environ.get(var)
        if value:
            dirs.append(Path(value))
    return dirs


def find_tor_binary() -> str:
    """Locate the tor executable on this platform (empty string if absent)."""
    override = os.environ.get("OMNI_TOR_PATH", "").strip()
    if override and Path(override).is_file():
        return override

    found = shutil.which("tor")
    if found:
        return found

    candidates: list[Path] = []
    if is_windows():
        # Tor Expert Bundle, a plain Tor install, and Tor Browser bundles.
        for base in _windows_program_dirs():
            candidates += [
                base / "Tor Expert Bundle" / "tor" / "tor.exe",
                base / "Tor" / "tor.exe",
                base / "Tor Browser" / "Browser" / "TorBrowser" / "Tor" / "tor.exe",
            ]
        home = Path.home()
        candidates += [
            home / "Desktop" / "Tor Browser" / "Browser" / "TorBrowser" / "Tor" / "tor.exe",
            home / "Downloads" / "Tor Browser" / "Browser" / "TorBrowser" / "Tor" / "tor.exe",
            home / "tor" / "tor.exe",
            home / "tor" / "Tor" / "tor.exe",
        ]
    elif is_macos():
        candidates += [
            Path("/opt/homebrew/bin/tor"),          # Apple-silicon Homebrew
            Path("/usr/local/bin/tor"),             # Intel Homebrew
            Path("/opt/local/bin/tor"),             # MacPorts
            Path("/Applications/Tor Browser.app/Contents/Resources/TorBrowser/Tor/tor"),
        ]
    else:
        candidates += [
            Path("/usr/bin/tor"), Path("/usr/local/bin/tor"), Path("/snap/bin/tor"),
            Path("/usr/sbin/tor"),
        ]

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def find_aria2c() -> str:
    """Locate aria2c (the torrent backend fallback)."""
    override = os.environ.get("OMNI_ARIA2C_PATH", "").strip()
    if override and Path(override).is_file():
        return override
    return shutil.which("aria2c") or ""


def install_hint(tool: str) -> str:
    """One-line, per-platform way to install a missing command line tool."""
    hints = {
        "ffmpeg": {
            WINDOWS: "winget install Gyan.FFmpeg",
            MACOS: "brew install ffmpeg",
            LINUX: "sudo pacman -S ffmpeg  (or: sudo apt install ffmpeg)",
        },
        "yt-dlp": {
            WINDOWS: "winget install yt-dlp.yt-dlp",
            MACOS: "brew install yt-dlp",
            LINUX: "pipx install yt-dlp  (or your package manager)",
        },
        "tor": {
            WINDOWS: "winget install TorProject.TorBrowser, or unpack the Tor Expert Bundle",
            MACOS: "brew install tor",
            LINUX: "sudo pacman -S tor  (or: sudo apt install tor)",
        },
        "aria2c": {
            WINDOWS: "winget install aria2.aria2",
            MACOS: "brew install aria2",
            LINUX: "sudo pacman -S aria2  (or: sudo apt install aria2)",
        },
        "libtorrent": {
            WINDOWS: "pip install libtorrent  (or use aria2c instead)",
            MACOS: "brew install libtorrent-rasterbar && pip install libtorrent",
            LINUX: "sudo pacman -S libtorrent-rasterbar  (or use aria2c)",
        },
    }
    return hints.get(tool, {}).get(system(), "")


# ── file names ──────────────────────────────────────────────────────

def safe_filename(name: str, max_length: int = 120, fallback: str = "download") -> str:
    """Turn arbitrary (remote-controlled) text into a usable file name.

    * strips directory components, so ``../../etc/passwd`` cannot escape;
    * removes characters Windows rejects and control characters;
    * avoids Windows' reserved device names and trailing dots/spaces;
    * caps the length: Windows still limits paths to 260 characters, and a
      playlist entry title can easily blow past that once the directory and
      the ``.f137.mp4``-style suffix are added.
    """
    cleaned = (name or "").replace("\\", "/").split("/")[-1]
    cleaned = "".join(
        ch for ch in cleaned
        if ch not in UNSAFE_FILENAME_CHARS and ch.isprintable()
    ).strip()
    cleaned = cleaned.rstrip(". ")

    stem, dot, suffix = cleaned.rpartition(".")
    if not dot:
        stem, suffix = cleaned, ""
    if stem.upper() in _WINDOWS_RESERVED:
        stem = f"_{stem}"

    if suffix and len(suffix) <= 10:
        keep = max(1, max_length - len(suffix) - 1)
        stem = stem[:keep]
        cleaned = f"{stem}.{suffix}" if stem else ""
    else:
        cleaned = (stem or cleaned)[:max_length]

    return cleaned or fallback


def is_executable_file(path: str | Path) -> bool:
    """Cheap check: exists, is a file, and is not empty."""
    try:
        p = Path(path)
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def python_executable() -> str:
    """The interpreter to re-launch the app with (or the frozen binary)."""
    return sys.executable

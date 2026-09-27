"""Shared PyInstaller settings for the Linux, macOS and Windows builds.

Spec files are exec'd by PyInstaller with their own globals, so the parts that
are genuinely platform-independent (hidden imports, data files, stdlib lookup)
live here and each spec imports them.  Keeping one list means a module added to
the app cannot be bundled on one OS and missing on the other two.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Imported at runtime but invisible to the static import graph — a missing
#: entry here is a crash only on the packaged build, so the list is explicit.
HIDDEN_IMPORTS = [
    # Qt: the app imports these lazily in places.
    "PyQt6.QtWidgets",
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtNetwork",
    # encodings: without them the frozen app dies on the first non-ASCII byte.
    "encodings",
    "encodings.utf_8",
    "encodings.latin_1",
    "encodings.ascii",
    "encodings.cp1252",
    # Dependency auto-install unpacks tar.xz (Linux/macOS) and zip (Windows).
    "tarfile",
    "zipfile",
    "lzma",
    "bz2",
    "zlib",
    # Network stack.
    "aiohttp",
    "aiohttp_socks",
    "aiodns",
    "packaging",
    "packaging.version",
    # Our own packages (imported by name at runtime in places).
    "omnidownloader",
    "omnidownloader.core",
    "omnidownloader.core.platform_utils",
    "omnidownloader.modules",
    "omnidownloader.services",
    "omnidownloader.ui",
    "omnidownloader.ui.pages",
    "omnidownloader.ui.widgets",
    "omnidownloader.diagnostics",
    "omnidownloader.services.update_service",
    "omnidownloader.services.dependency_manager",
    "omnidownloader.services.clipboard_monitor",
    "omnidownloader.services.config_store",
    "omnidownloader.services.ip_checker",
    "omnidownloader.services.plugin_loader",
    "omnidownloader.modules.http_downloader",
    "omnidownloader.modules.media_extractor",
    "omnidownloader.modules.image_scraper",
    "omnidownloader.modules.torrent_downloader",
]

#: Files that must exist inside the bundle.
DATA_FILES = [
    ("omnidownloader/ui", "omnidownloader/ui"),
]


def stdlib_pathex() -> list[str]:
    """Extra ``pathex`` entries so PyInstaller can find the stdlib.

    Derived from ``sys.base_prefix`` rather than ``sys.executable``: when the
    build runs inside a venv, ``sys.executable`` points at ``.venv/...``, which
    holds no stdlib and no ``python3*.dll``.  The layout differs per platform
    (``Lib`` on Windows, ``lib/pythonX.Y`` elsewhere), hence the candidates.
    """
    base = Path(sys.base_prefix)
    major, minor = sys.version_info.major, sys.version_info.minor
    candidates = [
        base / "Lib",                       # Windows
        base / "lib" / f"python{major}.{minor}",   # Linux / macOS
        base / "lib64" / f"python{major}.{minor}",
        Path(sys.executable).parent / ".." / "Lib",
        Path(sys.executable).parent / ".." / "lib" / f"python{major}.{minor}",
    ]
    found: list[str] = []
    for candidate in candidates:
        resolved = os.path.normpath(str(candidate))
        if os.path.isdir(resolved) and resolved not in found:
            found.append(resolved)
    if not found:
        print("WARNING: no stdlib directory found; the bundle may be incomplete")
    return found


def python_dlls() -> list[tuple[str, str]]:
    """Python runtime DLLs to bundle (Windows only; empty elsewhere)."""
    if sys.platform != "win32":
        return []
    import glob

    base = Path(sys.base_prefix)
    seen: set[str] = set()
    dlls: list[tuple[str, str]] = []
    for directory in (base, base / "DLLs", Path(sys.executable).parent):
        for dll in glob.glob(os.path.join(str(directory), "python3*.dll")):
            name = os.path.basename(dll)
            if name not in seen:
                seen.add(name)
                dlls.append((dll, "."))
    return dlls


def project_root() -> str:
    return os.path.abspath(os.getcwd())

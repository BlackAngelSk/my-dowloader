# OmniDownloader — PyInstaller spec for Linux builds.
#
# Produces a single-folder bundle (dist/OmniDownloader/) that the release
# workflow wraps into a .tar.gz together with a .desktop entry.
#
# Build:  pyinstaller linux.spec --noconfirm --clean

import os
import sys

PROJECT_ROOT = os.path.abspath(os.getcwd())
sys.path.insert(0, os.path.join(PROJECT_ROOT, "build"))

from pyi_common import DATA_FILES, HIDDEN_IMPORTS, python_dlls, stdlib_pathex  # noqa: E402

block_cipher = None

a = Analysis(
    ["omnidownloader/__main__.py"],
    pathex=[PROJECT_ROOT] + stdlib_pathex(),
    binaries=python_dlls(),
    datas=DATA_FILES,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Deps the app never imports but which drag in hundreds of MB.
        "tkinter",
        "matplotlib",
        "numpy",
        "pytest",
    ],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OmniDownloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX breaks Qt plugins on some distros
    console=False,      # GUI app: no terminal window
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="OmniDownloader",
)

# OmniDownloader — PyInstaller spec for Windows builds.
#
# Produces a single-folder bundle that Inno Setup wraps into an installer.
# The platform-independent parts (hidden imports, data files, stdlib/DLL
# discovery) come from build/pyi_common.py so Windows cannot silently miss a
# module that the Linux/macOS bundles include.
#
# Build:  pyinstaller windows.spec --noconfirm --clean

import os
import sys

PROJECT_ROOT = os.path.abspath(os.getcwd())
sys.path.insert(0, os.path.join(PROJECT_ROOT, "build"))

from pyi_common import DATA_FILES, HIDDEN_IMPORTS, python_dlls, stdlib_pathex  # noqa: E402

block_cipher = None

# Python runtime DLLs (critical for Python 3.14): derived from the *base*
# interpreter inside python_dlls(), so a venv build still bundles python3*.dll.
a = Analysis(
    ['omnidownloader/__main__.py'],
    pathex=[PROJECT_ROOT] + stdlib_pathex(),
    binaries=python_dlls(),
    datas=DATA_FILES,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'numpy',
        'pytest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='OmniDownloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='OmniDownloader',
)

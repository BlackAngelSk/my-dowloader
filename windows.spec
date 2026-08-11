# OmniDownloader — PyInstaller spec for Windows build
# Produces a single-folder bundle that Inno Setup wraps into an installer.

import sys
import os
import glob
from pathlib import Path

block_cipher = None

# ── Force paths so PyInstaller never collides with our build/ folder
PROJECT_ROOT = Path(__file__).resolve().parent
DISTPATH = str(PROJECT_ROOT / 'dist')
WORKPATH = str(PROJECT_ROOT / 'build' / '_pyinstaller_tmp')

# ── Collect Python runtime DLLs (critical for Python 3.14)
_python_dir = str(Path(sys.executable).parent)
_python_dlls = []
for _dll in glob.glob(os.path.join(_python_dir, 'python3*.dll')):
    _python_dlls.append((_dll, '.'))

a = Analysis(
    ['omnidownloader/__main__.py'],
    pathex=[str(PROJECT_ROOT)],
    binaries=_python_dlls,
    datas=[
        ('omnidownloader/ui', 'omnidownloader/ui'),
    ],
    hiddenimports=[
        'PyQt6.QtWidgets',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'omnidownloader.core',
        'omnidownloader.modules',
        'omnidownloader.services',
        'omnidownloader.ui',
        'omnidownloader.ui.pages',
        'omnidownloader.ui.widgets',
        'omnidownloader.services.update_service',
        'omnidownloader.services.dependency_manager',
        'omnidownloader.services.clipboard_monitor',
        'omnidownloader.services.ip_checker',
        'omnidownloader.services.plugin_loader',
        'omnidownloader.modules.http_downloader',
        'omnidownloader.modules.media_extractor',
        'omnidownloader.modules.image_scraper',
        'omnidownloader.modules.torrent_downloader',
        'aiohttp',
        'aiohttp_socks',
        'aiodns',
        'packaging',
        'packaging.version',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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

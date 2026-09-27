# OmniDownloader — PyInstaller spec for macOS builds.
#
# Produces dist/OmniDownloader.app (a proper bundle, so the app gets a real
# menu bar and focus behaviour instead of being treated as a background
# process) and the release workflow wraps it into a .dmg.
#
# Build:  pyinstaller macos.spec --noconfirm --clean
#
# Note: the bundle is unsigned.  macOS 10.15+ quarantines unsigned apps
# downloaded from the internet — users must right-click → Open the first time,
# or run: xattr -dr com.apple.quarantine /Applications/OmniDownloader.app

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
    upx=False,
    console=False,      # GUI app
    argv_emulation=False,   # we handle dropped files ourselves
    target_arch=None,       # native arch (arm64 on Apple silicon)
    codesign_identity=None,  # unsigned; set in CI when signing is available
    entitlements_file=None,
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

app = BUNDLE(
    coll,
    name="OmniDownloader.app",
    icon=None,
    bundle_identifier="com.omnidownloader.app",
    info_plist={
        "CFBundleName": "OmniDownloader",
        "CFBundleDisplayName": "OmniDownloader",
        "CFBundleShortVersionString": os.environ.get("APP_VERSION", "0.0.0"),
        "CFBundleVersion": os.environ.get("APP_VERSION", "0.0.0"),
        "LSMinimumSystemVersion": "11.0",
        # Retina-crisp Qt rendering; without this Qt draws blurry on HiDPI.
        "NSHighResolutionCapable": True,
        # The app talks to the network for downloads, not for Apple events.
        "LSApplicationCategoryType": "public.app-category.utilities",
        # Torrent/HTTP downloads: the sandbox stays off (we manage files
        # anywhere the user points us).
        "NSRequiresAquaSystemAppearance": False,
    },
)

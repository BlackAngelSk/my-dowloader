#!/usr/bin/env bash
# Build the macOS .app bundle locally (mirrors build/build_windows.bat).
#
#   ./build/build_macos.sh
#
# Result: dist/OmniDownloader.app
# Open it with:  open dist/OmniDownloader.app
#
# The bundle is unsigned.  Downloading it from the internet makes macOS
# quarantine it; clear that with:
#   xattr -dr com.apple.quarantine dist/OmniDownloader.app

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "error: macOS bundles must be built on macOS (found $(uname -s))" >&2
    exit 1
fi

PYTHON="${PYTHON:-.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
    PYTHON="python3"
fi

echo "==> Checking PyInstaller"
"$PYTHON" -m PyInstaller --version >/dev/null 2>&1 || {
    echo "Installing PyInstaller…"
    "$PYTHON" -m pip install --quiet pyinstaller packaging
}

echo "==> Building"
APP_VERSION="${APP_VERSION:-$(git describe --tags --abbrev=0 2>/dev/null || echo 0.0.0)}" \
    "$PYTHON" -m PyInstaller macos.spec --noconfirm --clean

echo "==> Smoke test (headless)"
QT_QPA_PLATFORM=offscreen ./dist/OmniDownloader.app/Contents/MacOS/OmniDownloader --diagnose \
    | tee /tmp/omni-diagnose.txt

if grep -q "no download modules loaded" /tmp/omni-diagnose.txt; then
    echo "ERROR: the bundle loaded no download modules — nothing would work." >&2
    exit 1
fi

echo
echo "Done: dist/OmniDownloader.app"
echo "Optional: hdiutil create -volname OmniDownloader -srcfolder dist/OmniDownloader.app -ov -format UDZO OmniDownloader.dmg"

#!/usr/bin/env bash
# Build the Linux bundle locally (mirrors build/build_windows.bat).
#
#   ./build/build_linux.sh
#
# Result: dist/OmniDownloader/OmniDownloader  (run it directly)

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

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
"$PYTHON" -m PyInstaller linux.spec --noconfirm --clean

echo "==> Smoke test (headless)"
QT_QPA_PLATFORM=offscreen ./dist/OmniDownloader/OmniDownloader --diagnose | tee /tmp/omni-diagnose.txt

if grep -q "no download modules loaded" /tmp/omni-diagnose.txt; then
    echo "ERROR: the bundle loaded no download modules — nothing would work." >&2
    exit 1
fi

echo
echo "Done: dist/OmniDownloader/OmniDownloader"

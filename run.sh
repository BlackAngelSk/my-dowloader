#!/usr/bin/env bash
# OmniDownloader — one-click venv setup & launch
set -euo pipefail
cd "$(dirname "$0")"

VENV_DIR=".venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --upgrade pip
    "$VENV_DIR/bin/pip" install -e ".[torrent]"
    echo "Dependencies installed."
fi

exec "$VENV_DIR/bin/python" -m omnidownloader "$@"

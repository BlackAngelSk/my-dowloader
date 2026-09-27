#!/usr/bin/env bash
# Install OmniDownloader from a release tarball into the user's home.
#
#   tar -xzf OmniDownloader-Linux-x86_64-*.tar.gz
#   ./install_linux.sh
#
# Nothing is installed system-wide: the app goes to ~/.local/share/OmniDownloader,
# a launcher to ~/.local/bin and a desktop entry to ~/.local/share/applications.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/OmniDownloader"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

if [[ ! -d "$HERE/OmniDownloader" ]]; then
    echo "error: run this script from the extracted tarball (OmniDownloader/ not found)" >&2
    exit 1
fi

echo "Installing OmniDownloader to $APP_DIR"
mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR"
rm -rf "${APP_DIR:?}/OmniDownloader"
cp -r "$HERE/OmniDownloader" "$APP_DIR/"
chmod +x "$APP_DIR/OmniDownloader/OmniDownloader"

ln -sf "$APP_DIR/OmniDownloader/OmniDownloader" "$BIN_DIR/omnidownloader"

if [[ -f "$HERE/OmniDownloader.desktop" ]]; then
    sed "s|^Exec=OmniDownloader|Exec=$APP_DIR/OmniDownloader/OmniDownloader|" \
        "$HERE/OmniDownloader.desktop" > "$DESKTOP_DIR/OmniDownloader.desktop"
    command -v update-desktop-database >/dev/null 2>&1 \
        && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
fi

echo
echo "Done."
echo "  Launch:  omnidownloader        (or from your application menu)"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "  Note:    $BIN_DIR is not on your PATH — add it to use the 'omnidownloader' command" ;;
esac
echo "  Config:  ~/.omnidownloader/    (config, history, logs, plugins, deps)"
echo
echo "Optional extras:"
echo "  torrents : sudo pacman -S aria2    (or apt install aria2 / dnf install aria2)"
echo "  anonymity: sudo pacman -S tor      (or apt install tor)"
for tool in ffmpeg yt-dlp aria2 tor; do
    command -v "$tool" >/dev/null 2>&1 || echo "  missing  : $tool (the app can download ffmpeg/yt-dlp itself)"
done

# ⚡ OmniDownloader

**Ultra-fast, modular, cross-platform download manager** with multi-protocol support.

## Features

- 🌐 **HTTP/HTTPS Multi-Segment Downloader** — Dynamic range-request splitting with RAM buffering for maximum throughput
- ⏯️ **Resume Anywhere** — Interrupted downloads continue from where they stopped (per-segment offsets, kept across restarts)
- 🔐 **Checksum Verification** — Verify a finished download against `sha256:`/`md5:` (paste it after the URL)
- 📹 **Video & Social Media Extractor** — yt-dlp integration for YouTube, Twitter/X, TikTok, Instagram, Reddit, Twitch, and more
- 📃 **Playlists** — A playlist URL becomes one job per entry, all inheriting the quality you pick once
- 🔄 **Self-Updating Extractor** — yt-dlp is refreshed automatically (weekly, or from Settings) so broken extractors self-heal
- 💾 **Persistent Settings** — Download directory, limits, theme, proxy, scheduler rules and history survive restarts
- 🧲 **Torrent Downloader** — libtorrent integration with DHT and sequential download
- 🖼️ **Image Scraper** — Batch image gallery downloader with deduplication
- 🎨 **Dark & Light Mode** — System-aware theme switching with polished QSS stylesheets
- 📋 **Clipboard Monitor** — Auto-detects copied URLs (including magnets) and prompts to download
- 📂 **Drag & Drop** — Drop .torrent files, or text files containing a list of links
- 🩺 **Diagnostics** — `python -m omnidownloader --diagnose [--download]` measures a real end-to-end download and reports what is broken
- 🖥️ **Linux, macOS and Windows** — one code path, with the OS differences confined to `core/platform_utils.py`
- ⚡ **Performance** — RAM ring buffer (16–64 MB), disk pre-allocation, async I/O, dynamic thread allocation

## Architecture

```
omnidownloader/
├── main.py                    # Entry point (+ --diagnose)
├── diagnostics.py             # Self-check report
├── core/
│   ├── base_module.py         # Abstract BaseDownloaderModule
│   ├── models.py              # DownloadJob, DownloadState, SegmentProgress
│   ├── download_manager.py    # Queue scheduler, speed limiter, signals
│   ├── platform_utils.py      # The only place that talks to the OS
│   ├── ram_buffer.py          # Ring buffer + positional disk flush
│   ├── disk_utils.py          # Pre-allocation, checksums, resume sidecar
│   └── scheduler.py           # Time-of-day rules
├── modules/
│   ├── http_downloader.py     # Segmented HTTP downloader with resume
│   ├── media_extractor.py     # yt-dlp + ffmpeg wrapper (playlists, clients)
│   ├── torrent_downloader.py  # libtorrent wrapper
│   └── image_scraper.py       # Batch image scraper
├── ui/
│   ├── themes.py              # Dark/Light QSS stylesheet engine
│   ├── main_window.py         # Main application window
│   ├── drag_drop_overlay.py   # Drag-and-drop overlay (URL lists)
│   ├── widgets/
│   │   ├── url_input_bar.py   # Smart URL input with paste + checksum
│   │   ├── download_card.py   # Progress card (speed, ETA, bar)
│   │   ├── speed_graph.py     # Real-time speed graph (QPainter)
│   │   ├── settings_panel.py  # Settings form (incl. Update yt-dlp)
│   │   └── toast_notification.py  # Clipboard-detected toast
│   └── pages/
│       ├── dashboard_page.py  # Active downloads + queue
│       ├── history_page.py    # Completed downloads
│       └── settings_page.py   # Global settings
└── services/
    ├── clipboard_monitor.py   # Clipboard URL detection
    ├── config_store.py        # Settings + history persistence
    ├── dependency_manager.py  # Auto-download/update yt-dlp, ffmpeg
    └── plugin_loader.py       # Dynamic module discovery
```

## Requirements

- Python 3.11+
- PyQt6
- aiohttp
- yt-dlp (system or auto-downloaded)
- ffmpeg (system or auto-downloaded)
- aria2c or python-libtorrent (optional, torrent support)
- tor (optional, anonymity features)

## Installation

### Prebuilt releases

| Platform | Artifact | Install |
|---|---|---|
| Windows 10/11 | `OmniDownloader-Setup-<ver>.exe` | run the installer |
| macOS 11+ (Apple silicon) | `OmniDownloader-macOS-arm64-<ver>.dmg` | open, drag to Applications |
| Linux x86_64 | `OmniDownloader-Linux-x86_64-<ver>.tar.gz` | `tar -xzf … && ./install_linux.sh` |

The macOS build is **unsigned**: the first launch needs right-click → Open, or
`xattr -dr com.apple.quarantine /Applications/OmniDownloader.app`.

### From source (any platform)

```bash
python -m venv .venv
.venv/bin/pip install -e .          # Windows: .venv\Scripts\pip install -e .
python -m omnidownloader            # run the app
python -m omnidownloader --diagnose # verify this machine can download
```

Optional tools per platform:

```
ffmpeg    Linux: sudo pacman -S ffmpeg   macOS: brew install ffmpeg   Windows: winget install Gyan.FFmpeg
yt-dlp    Linux: pipx install yt-dlp     macOS: brew install yt-dlp   Windows: winget install yt-dlp.yt-dlp
torrents  Linux: sudo pacman -S aria2    macOS: brew install aria2    Windows: winget install aria2.aria2
tor       Linux: sudo pacman -S tor      macOS: brew install tor      Windows: Tor Expert Bundle
```

The app downloads and unpacks yt-dlp and ffmpeg itself if they are missing
(including the right CPU architecture — arm64 builds on Apple silicon and
Raspberry Pi), and keeps yt-dlp up to date.

## Usage

```bash
python -m omnidownloader              # run the app
python -m omnidownloader --diagnose    # check dependencies + generation
```

Where things are stored: `~/.omnidownloader/` holds `config.json`,
`history.json`, `plugins/`, `deps/` (managed yt-dlp + ffmpeg), `tor/` and
`logs/`.  One location on every platform; override it with the
`OMNI_DOWNLOADER_HOME` environment variable.  `OMNI_TOR_PATH` and
`OMNI_ARIA2C_PATH` point the app at non-standard binaries.

Interrupted downloads leave a `<file>.omnidownloader.json` sidecar next to the
partial file; the next attempt for the same URL resumes from it, and removing
the job (✕) deletes both. Cancelling a job deliberately discards it.

```bash
# verify a download against a published checksum
# paste into the URL bar:   https://example.com/big.iso sha256:<hex>
```

## Adding Custom Modules

Drop a `.py` file into `~/.omnidownloader/plugins/` (the directory is created on
first run and loaded at startup):

```python
from omnidownloader.core.base_module import BaseDownloaderModule

class MyCustomDownloader(BaseDownloaderModule):
    MODULE_NAME = "mycustom"

    def can_handle(self, url: str) -> bool:
        return "mywebsite.com" in url

    async def extract_metadata(self, url: str) -> dict:
        return {"title": "My File", "file_size": -1}

    async def start_download(self, job, progress_callback=None):
        # Implement download logic
        pass
```

Built-in modules live in `omnidownloader/modules/`; `MODULE_NAME` decides routing.
Note that built-ins are *imported*, so packaging them into a frozen bundle (see
`build/pyi_common.py`) is all that is needed.

## Development

```bash
.venv/bin/python tools/test_routing.py            # URL → module routing
.venv/bin/python tools/test_http_downloader.py    # segmented engine, resume, checksums
.venv/bin/python tools/test_media_extractor.py    # live YouTube download
.venv/bin/python tools/test_playlists.py          # playlist → child jobs
.venv/bin/python tools/test_persistence.py        # config + history
.venv/bin/python tools/test_cross_platform.py     # Linux/macOS/Windows behaviour
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/test_ui_wiring.py
```

All platform-specific code belongs in `omnidownloader/core/platform_utils.py` —
`tools/test_cross_platform.py` fails the build if OS branches, unguarded
Unix-only calls or launcher paths reappear elsewhere.

Building a bundle locally:

```bash
./build/build_linux.sh     # → dist/OmniDownloader/OmniDownloader
./build/build_macos.sh     # → dist/OmniDownloader.app   (macOS only)
build\build_windows.bat    # → dist\OmniDownloader\ + Inno Setup installer
```

Each build script smoke-tests the frozen app and refuses to finish if it loaded
no download modules.  CI (`.github/workflows/release.yml`) builds all three
platforms and attaches one artifact per OS to the release.

## License

MIT

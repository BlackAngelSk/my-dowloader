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
- 🧲 **Torrents & Magnet Links** — magnet links resolve to a real name, size and file
  list (libtorrent, or aria2c plus the bundled metadata reader), with live progress
- 🖼️ **Image Scraper** — Batch image gallery downloader with deduplication
- 🕵️ **Automatic Proxy** — Finds a working public proxy for you and routes every
  download through it (each candidate is proven by pushing a real request
  through it; transparent proxies are rejected), with health re-checks and
  automatic replacement when one dies
- 🎨 **Dark & Light Mode** — System-aware theme switching with polished QSS stylesheets
- 📋 **Clipboard Monitor** — Auto-detects copied URLs (including magnets) and prompts to download
- 📂 **Drag & Drop** — Drop .torrent files, or text files containing a list of links
- 🩺 **Diagnostics** — `python -m omnidownloader --diagnose [--download]` measures a real end-to-end download and reports what is broken
- 🖥️ **Linux, macOS and Windows** — one code path, with the OS differences confined to `core/platform_utils.py`
- ⚡ **Performance** — a pool of connections that share the work (a free one takes the
  largest remaining piece), RAM ring buffer (16–64 MB), disk pre-allocation, async I/O

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
│   ├── segment_planner.py     # Work-stealing piece planning for the pool
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

On Windows you can skip all of the manual steps: `start.bat` creates `.venv`,
installs `requirements.txt`, re-installs it whenever that file changes (or `pip`
is missing), installs the managed tools (`yt-dlp`, `ffmpeg`, and `aria2c` for
torrents) and then launches the app — a plain clone plus a double click is
enough. `run.sh` does the same on Linux/macOS.

The tool installer is also available on its own:

```bash
python -m omnidownloader --install-deps          # install yt-dlp/ffmpeg (+ aria2c on Windows)
python -m omnidownloader --install-deps --check  # report only, download nothing
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

## Download engine

The HTTP engine splits a file into **four times as many pieces as there are
connections** and lets each free connection take the largest piece still going
(the idea comes from [Mister Download Manager](https://github.com/anefzaoui/misterdownloadmanager)'s
planner).  Three things follow from that:

- **The tail is shared.**  The last piece is never left to one connection while
  the others idle — a per-connection cap is spread over the whole file instead
  of deciding who finishes last.
- **A stalled connection loses its piece.**  If a piece moves no bytes for 15 s
  while the rest of the job is still transferring, its connection is dropped,
  the bytes already on disk are kept, and the remainder is re-split for the
  pool.  If *nothing* moves for 30 s the job fails with a message instead of
  hanging forever.
- **Every piece retries from where it stopped.**  A reset connection, a 429/503
  (honouring `Retry-After`) or a body cut short is retried — four attempts with
  exponential backoff — and the resume point is the last byte stored, never the
  start of the piece.

Resume state is protected against a file that changed on the server: the ETag /
Last-Modified seen at probe time goes into the sidecar and back out as
`If-Range`, so a replaced file is downloaded again rather than mixed with the
half you already had.

Measured with `tools/bench_segmented.py` (local server, 2 MB/s cap per
connection, 25 MB file): **1 connection 12.4 s, pool of 8 1.5 s — 8.1×**, same
bytes.

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

On every start the app asks GitHub for the latest release (Settings → Updates →
"Update automatically on start", on by default). How it installs depends on how
it runs:

* **git checkout** (what `start.bat`/`run.sh` use): the checkout is
  fast-forwarded with `git fetch` + `git pull --ff-only`, then the app restarts
  through the launcher so changed requirements are installed. A dirty tree, a
  detached HEAD, a non-tracking branch or diverged history are *not* touched —
  the update is skipped and the app keeps running.
* **packaged build** (installer/.dmg/tarball): the release asset for this
  platform is downloaded and installed; on Windows the Inno installer runs
  silently and the app exits so it can replace the binary.

Turn it off in Settings → Updates; "Check for Updates" there still asks before
installing anything.

Pasted links are tidied before routing: the `https://` part is optional
(`youtube.com/watch?v=…` works, which is what the address bar and the Windows
clipboard usually contain), zero-width characters from a paste are dropped, and
a link that genuinely cannot be downloaded now says *why* (unsupported scheme,
not a link at all, or yt-dlp still being installed) instead of one blanket
message.

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
.venv/bin/python tools/test_segment_planner.py    # piece grid + work stealing (pure)
.venv/bin/python tools/test_segmented_engine.py   # stall re-split, retries, changed file
.venv/bin/python tools/bench_segmented.py         # pool vs single connection, byte-exact
.venv/bin/python tools/test_media_extractor.py    # live YouTube download
.venv/bin/python tools/test_playlists.py          # playlist → child jobs
.venv/bin/python tools/test_persistence.py        # config + history
.venv/bin/python tools/test_proxy_finder.py       # proxy discovery + validation (offline, local proxies)
.venv/bin/python tools/test_proxy_finder.py --live # + a real search against public lists
.venv/bin/python tools/test_magnet.py             # magnet/torrent handling (offline)
.venv/bin/python tools/test_magnet.py --live "magnet:?xt=urn:btih:..."   # + a real magnet
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

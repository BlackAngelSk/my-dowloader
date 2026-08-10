# ⚡ OmniDownloader

**Ultra-fast, modular, cross-platform download manager** with multi-protocol support.

## Features

- 🌐 **HTTP/HTTPS/FTP Multi-Segment Downloader** — Dynamic range-request splitting with RAM buffering for maximum throughput
- 📹 **Video & Social Media Extractor** — yt-dlp integration for YouTube, Twitter/X, TikTok, Instagram, Reddit, Twitch, and more
- 🧲 **Torrent Downloader** — libtorrent integration with DHT, sequential download, selective file priorities
- 🖼️ **Image Scraper** — Batch image gallery downloader with deduplication
- 🎨 **Dark & Light Mode** — System-aware theme switching with polished QSS stylesheets
- 📋 **Clipboard Monitor** — Auto-detects copied URLs and prompts to download
- 📂 **Drag & Drop** — Drop files, .torrents, or URL lists directly onto the app
- ⚡ **Performance** — RAM ring buffer (16–64 MB), disk pre-allocation, async I/O, dynamic thread allocation

## Architecture

```
omnidownloader/
├── main.py                    # Entry point
├── core/
│   ├── base_module.py         # Abstract BaseDownloaderModule
│   ├── models.py              # DownloadJob, DownloadState, SegmentProgress
│   ├── download_manager.py    # Queue scheduler, speed limiter, signals
│   ├── ram_buffer.py          # Ring buffer + disk flush logic
│   └── disk_utils.py          # Pre-allocation (fallocate), space checks
├── modules/
│   ├── http_downloader.py     # Multi-threaded HTTP chunked downloader
│   ├── media_extractor.py     # yt-dlp + ffmpeg wrapper
│   ├── torrent_downloader.py  # libtorrent wrapper
│   └── image_scraper.py       # Batch image scraper
├── ui/
│   ├── themes.py              # Dark/Light QSS stylesheet engine
│   ├── main_window.py         # Main application window
│   ├── drag_drop_overlay.py   # Drag-and-drop overlay
│   ├── widgets/
│   │   ├── url_input_bar.py   # Smart URL input with paste
│   │   ├── download_card.py   # Progress card (speed, ETA, bar)
│   │   ├── speed_graph.py     # Real-time speed graph (QPainter)
│   │   ├── settings_panel.py  # Settings form
│   │   └── toast_notification.py  # Clipboard-detected toast
│   └── pages/
│       ├── dashboard_page.py  # Active downloads + queue
│       ├── history_page.py    # Completed downloads
│       └── settings_page.py   # Global settings
└── services/
    ├── clipboard_monitor.py   # Clipboard URL detection
    ├── dependency_manager.py  # Auto-download yt-dlp, ffmpeg
    └── plugin_loader.py       # Dynamic module discovery
```

## Requirements

- Python 3.11+
- PyQt6
- aiohttp
- yt-dlp (system or auto-downloaded)
- ffmpeg (system or auto-downloaded)
- python-libtorrent (optional, for torrent support)

## Installation

```bash
cd omnidownloader
pip install -r requirements.txt

# Optional: for torrent support
pip install python-libtorrent
```

## Usage

```bash
python -m omnidownloader.main
```

## Adding Custom Modules

Create a new file in `omnidownloader/modules/` or an external plugin directory:

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

Register it in `services/plugin_loader.py` or use the external plugin directory.

## License

MIT

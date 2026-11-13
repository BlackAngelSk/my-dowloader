#!/usr/bin/env python3
"""Render the real UI to PNG files offscreen (design work + visual regression).

The window is the app's own MainWindow with a stubbed DownloadManager, so the
pictures show the widgets as they actually lay out — no mock-up drift.

Usage: QT_QPA_PLATFORM=offscreen .venv/bin/python tools/shots_ui.py \
           --out /tmp/shots --theme both [--page dashboard,history,...]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from omnidownloader.core.download_manager import DownloadManager  # noqa: E402
from omnidownloader.core.models import DownloadJob, DownloadState, Priority  # noqa: E402
from omnidownloader.ui.main_window import MainWindow  # noqa: E402

PAGES = ("dashboard", "history", "anonymity", "scheduler", "settings")


def sample_jobs() -> list[DownloadJob]:
    """A believable mix of states, so every part of a card gets exercised."""
    busy = DownloadJob(
        url="https://cdn.example.com/ubuntu-24.04.1-desktop-amd64.iso",
        state=DownloadState.DOWNLOADING,
        file_name="ubuntu-24.04.1-desktop-amd64.iso",
        file_path="/home/black/Downloads/ubuntu-24.04.1-desktop-amd64.iso",
        file_size=5_400_000_000, downloaded_bytes=2_380_000_000,
        speed_bps=11.4 * 1024 * 1024, thread_count=8,
    )
    busy.started_at = 0.0
    video = DownloadJob(
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        state=DownloadState.DOWNLOADING,
        file_name="Rick Astley - Never Gonna Give You Up (4K Remaster).mp4",
        file_path="/home/black/Downloads/Rick Astley.mp4",
        file_size=248_000_000, downloaded_bytes=61_000_000,
        speed_bps=3.2 * 1024 * 1024, thread_count=4,
        priority=Priority.HIGH,
    )
    video.metadata.update({"quality_label": "1080p", "format": "137+140"})
    paused = DownloadJob(
        url="https://mirror.example.org/debian/debian-12.7.0-amd64-DVD-1.iso",
        state=DownloadState.PAUSED,
        file_name="debian-12.7.0-amd64-DVD-1.iso",
        file_path="/home/black/Downloads/debian-12.7.0-amd64-DVD-1.iso",
        file_size=3_900_000_000, downloaded_bytes=470_000_000,
    )
    torrent = DownloadJob(
        url="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
        state=DownloadState.EXTRACTING,
        file_name="fedora-workstation-41.iso",
        file_path="/home/black/Downloads/fedora-workstation-41.iso",
        file_size=2_100_000_000, downloaded_bytes=2_100_000_000,
        speed_bps=1.1 * 1024 * 1024,
    )
    torrent.metadata["status_note"] = "Checking pieces"
    queued = DownloadJob(
        url="https://files.example.net/archive/photos-2024.zip",
        state=DownloadState.PENDING,
        file_name="photos-2024.zip",
        file_path="/home/black/Downloads/photos-2024.zip",
        file_size=1_240_000_000,
    )
    failed = DownloadJob(
        url="https://broken.example.com/missing.tar.gz",
        state=DownloadState.FAILED,
        file_name="missing.tar.gz",
        file_path="/home/black/Downloads/missing.tar.gz",
        error_message="HTTP 404 — the file is no longer on the server.",
    )
    finished = DownloadJob(
        url="https://cdn.example.com/obs-studio-31.0.1-x86_64.tar.gz",
        state=DownloadState.COMPLETED,
        file_name="obs-studio-31.0.1-x86_64.tar.gz",
        file_path="/home/black/Downloads/obs-studio-31.0.1-x86_64.tar.gz",
        file_size=310_000_000, downloaded_bytes=310_000_000,
    )
    return [busy, video, paused, torrent, queued, failed, finished]


def render(out: Path, theme: str, pages: list[str]) -> list[Path]:
    app = QApplication.instance() or QApplication(sys.argv)
    dm = DownloadManager()
    window = MainWindow(dm)
    window._set_theme(theme)

    jobs = sample_jobs()
    for job in jobs:
        window._dashboard.add_job(job)
    import math
    for i in range(70):
        wobble = 0.55 + 0.45 * math.sin(i / 5.0) * math.cos(i / 13.0)
        window._dashboard.update_global_speed(14.6 * 1024 * 1024 * wobble)
    for job in jobs:
        window._dashboard.update_job(job)
    for job in (jobs[6], jobs[5]):
        window._history.add_job(job)

    window.resize(1280, 820)
    window.show()
    app.processEvents()

    # The clipboard toast is its own top-level window: grab it separately so a
    # theme change to it can be reviewed like any other surface.
    window._toast.show_for_url("https://example.com/ubuntu-24.04.1-live-server-amd64.iso")
    app.processEvents()
    toast_png = out / f"toast-{theme}.png"
    window._toast.grab().save(str(toast_png))
    window._toast.hide()

    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for index, name in enumerate(PAGES):
        if name not in pages:
            continue
        window._switch_page(index)
        app.processEvents()
        # Let layout settle (queued resize/layout events need a second pass).
        for _ in range(3):
            app.processEvents()
        path = out / f"{name}-{theme}.png"
        window.grab().save(str(path))
        written.append(path)
    window.close()
    return written


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/tmp/omnidownloader-shots")
    parser.add_argument("--theme", default="both", choices=("dark", "light", "both"))
    parser.add_argument("--page", default=",".join(PAGES))
    args = parser.parse_args()

    out = Path(args.out)
    themes = ["dark", "light"] if args.theme == "both" else [args.theme]
    pages = [p.strip() for p in args.page.split(",") if p.strip() in PAGES]
    written: list[Path] = []
    for theme in themes:
        written.extend(render(out, theme, pages))
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

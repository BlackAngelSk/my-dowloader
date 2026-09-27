"""Embedded Media Player widget for in-app preview.

Uses QMediaPlayer + QVideoWidget for video/audio.
Falls back to QLabel + QPixmap for images.
Reads from StreamingBuffer of partially-downloaded files.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

from omnidownloader.core import platform_utils

logger = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".webm", ".mov"}
AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".wav"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}


def _is_video(name: str) -> bool:
    return Path(name).suffix.lower() in VIDEO_EXTS

def _is_audio(name: str) -> bool:
    return Path(name).suffix.lower() in AUDIO_EXTS

def _is_image(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_EXTS


class MediaPreviewWidget(QFrame):
    """Dockable media player panel for previewing downloads in progress."""

    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("mediaPreview")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumHeight(200)
        self._streaming_buffer = None
        self._job = None
        self._timer: Optional[QTimer] = None
        self._last_read_pos = 0
        self._preview_source_bytes = -1
        self._cached_pixmap = None
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(4)

        header = QHBoxLayout()
        self._title_lbl = QLabel("Media Preview")
        self._title_lbl.setStyleSheet("font-size: 14px; font-weight: bold;")
        header.addWidget(self._title_lbl, 1)
        self._status_lbl = QLabel("")
        self._status_lbl.setObjectName("muted")
        header.addWidget(self._status_lbl)
        close_btn = QPushButton("\u2715")
        close_btn.setObjectName("iconButton")
        close_btn.setFixedSize(28, 28)
        close_btn.clicked.connect(self._on_close)
        header.addWidget(close_btn)
        root.addLayout(header)

        self._player_area = QLabel("Select a download to preview")
        self._player_area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._player_area.setMinimumHeight(160)
        self._player_area.setStyleSheet(
            "background-color: #111827; border-radius: 8px; color: #94A3B8;"
        )
        root.addWidget(self._player_area, 1)

        controls = QHBoxLayout()
        self._play_btn = QPushButton("\u25b6 Open in Player")
        self._play_btn.setObjectName("primaryButton")
        self._play_btn.clicked.connect(self._open_external)
        self._play_btn.setEnabled(False)
        controls.addWidget(self._play_btn)
        self._progress_lbl = QLabel("0% downloaded")
        self._progress_lbl.setObjectName("muted")
        controls.addWidget(self._progress_lbl)
        controls.addStretch()
        root.addLayout(controls)

    def attach_job(self, job) -> None:
        # Detach first: re-attaching used to create a second QTimer while the
        # old one kept polling, so N previews meant N timers running.
        self.detach()
        self._job = job
        self._streaming_buffer = getattr(job, "streaming_buffer", None)
        self._last_read_pos = 0
        self._preview_source_bytes = -1
        self._cached_pixmap = None
        self._title_lbl.setText(job.file_name or "Media Preview")
        fname = job.file_name or job.url
        if _is_image(fname):
            self._status_lbl.setText("Image preview")
        elif _is_audio(fname):
            self._status_lbl.setText("Audio player")
        elif _is_video(fname):
            self._status_lbl.setText("Video player")
        else:
            self._status_lbl.setText("Preview")
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_buffer)
        self._timer.start(1000)
        self._poll_buffer()

    def detach(self) -> None:
        if self._timer:
            self._timer.stop()
            self._timer = None
        self._streaming_buffer = None
        self._job = None

    def _poll_buffer(self) -> None:
        if not self._streaming_buffer or not self._job:
            return
        avail = self._streaming_buffer.available_bytes()
        total = self._streaming_buffer.file_size
        pct = (avail / total * 100) if total > 0 else 0
        self._progress_lbl.setText(f"{pct:.1f}% downloaded ({avail:,} bytes)")
        if self._job.file_name and _is_image(self._job.file_name):
            if avail > 0:
                self._show_image_preview()
        min_preview = max(1024 * 1024, total // 20)
        if avail >= min_preview:
            self._play_btn.setEnabled(True)

    def _show_image_preview(self) -> None:
        if not self._streaming_buffer:
            return
        try:
            avail = self._streaming_buffer.available_bytes()
            # Re-read and rescale only when the available prefix actually
            # changed — this used to pull up to 5 MB and run a smooth rescale
            # on every 1 Hz tick while a large image was downloading.
            if avail == self._preview_source_bytes and self._cached_pixmap is not None:
                return
            data = self._streaming_buffer.read(0, min(5 * 1024 * 1024, avail))
            if not data:
                return
            fresh = QPixmap()
            fresh.loadFromData(data)
            if fresh.isNull():
                return
            self._cached_pixmap = fresh
            self._preview_source_bytes = avail
            scaled = self._cached_pixmap.scaled(
                self._player_area.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._player_area.setPixmap(scaled)
        except Exception as exc:
            logger.debug("Image preview failed: %s", exc)

    def _open_external(self) -> None:
        if not self._job or not self._job.file_path:
            return
        # Same helper as the "open folder" action: `cmd /c start` broke on
        # paths with spaces and flashed a console window on Windows.
        platform_utils.open_path(self._job.file_path, reveal=True)

    def _on_close(self) -> None:
        self.detach()
        self.closed.emit()

    def closeEvent(self, a0):
        self.detach()
        super().closeEvent(a0)

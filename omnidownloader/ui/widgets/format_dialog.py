"""Format Selection Dialog — lets users pick video/audio quality before download.

Displays all available formats from yt-dlp metadata in a tabbed view
(Video / Audio), showing resolution, codec, filesize, and container.
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

logger = logging.getLogger(__name__)


def _fmt_size(b: int) -> str:
    if b <= 0:
        return "?"
    for u in ("B", "KB", "MB", "GB"):
        if abs(b) < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"


def _fmt_duration(secs: float) -> str:
    if secs <= 0:
        return ""
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"


def _parse_height(res: str) -> int:
    try:
        if "x" in str(res):
            return int(str(res).split("x")[1])
        return int(str(res).replace("p", "").strip())
    except (ValueError, IndexError):
        return 0


class FormatEntry:
    """Structured representation of a single yt-dlp format."""

    def __init__(self, fmt: dict):
        self.format_id: str = fmt.get("format_id", "")
        self.ext: str = fmt.get("ext", "")
        self.resolution: str = fmt.get("resolution", "audio only")
        self.filesize: int = fmt.get("filesize") or fmt.get("filesize_approx", 0)
        self.vcodec: str = fmt.get("vcodec", "none")
        self.acodec: str = fmt.get("acodec", "none")
        self.fps: float = fmt.get("fps") or 0
        # Use native height from yt-dlp, fallback to parsing resolution string
        self.height: int = fmt.get("height", 0) or _parse_height(fmt.get("resolution", ""))
        self.abr: float = fmt.get("abr") or 0
        # A format is audio-only if it has no video codec
        self.audio_only: bool = self.vcodec in ("none", "")

    @property
    def is_video(self) -> bool:
        # Video if it has a video codec OR a known height
        return (self.vcodec not in ("none", "")) or self.height > 0

    @property
    def label(self) -> str:
        parts = []
        if self.is_video:
            parts.append(f"{self.height}p")
            if self.fps > 0 and self.fps != 30:
                parts.append(f"{int(self.fps)}fps")
        elif self.audio_only:
            if self.abr > 0:
                parts.append(f"{int(self.abr)}kbps")
            parts.append("audio")
        codec_parts = []
        if self.vcodec and self.vcodec != "none":
            codec_parts.append(self.vcodec.split(".")[0])
        if self.acodec and self.acodec != "none":
            codec_parts.append(self.acodec.split(".")[0])
        if codec_parts:
            parts.append(" / ".join(codec_parts))
        return " — ".join(parts) if parts else self.format_id

    @property
    def detail_line(self) -> str:
        sz = _fmt_size(self.filesize) if self.filesize > 0 else ""
        parts = [self.ext.upper()]
        if sz:
            parts.append(sz)
        return " · ".join(parts)


class FormatItemWidget(QFrame):
    """Custom widget for a single format row in the list."""

    selected = pyqtSignal(str)

    def __init__(self, entry: FormatEntry, is_default: bool = False,
                 emit_id: str = "", parent=None):
        super().__init__(parent)
        self._entry = entry
        self._emit_id = emit_id or entry.format_id
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)

        self._indicator = QLabel("●" if is_default else "○")
        self._indicator.setFixedWidth(20)
        self._indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._indicator.setStyleSheet(
            "font-size: 16px; color: #3B82F6;" if is_default else "font-size: 16px;"
        )
        layout.addWidget(self._indicator)

        lbl = QLabel(f"<b>{entry.label}</b>")
        lbl.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(lbl, 1)

        detail = QLabel(entry.detail_line)
        detail.setObjectName("muted")
        detail.setFixedWidth(120)
        detail.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(detail)

    def mousePressEvent(self, a0):
        self.selected.emit(self._emit_id)
        super().mousePressEvent(a0)


class FormatSelectionDialog(QDialog):
    """Modal dialog for selecting download quality from yt-dlp formats."""

    def __init__(self, metadata: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Download Quality")
        self.setMinimumSize(520, 520)
        self.setModal(True)
        self._metadata = metadata
        self._formats = metadata.get("formats", [])
        self._result_data: dict = {}
        self._selected_id: Optional[str] = None
        self._entries: dict[str, FormatEntry] = {}
        self._build_ui()
        self._populate_formats()

    @property
    def result_data(self) -> dict:
        return self._result_data

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # Header
        header = QHBoxLayout()
        header.setSpacing(12)
        thumb = QLabel("🎬")
        thumb.setFixedSize(120, 68)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setStyleSheet("font-size: 28px; background-color: #111827; border-radius: 6px;")
        if self._metadata.get("thumbnail"):
            try:
                import urllib.request
                data = urllib.request.urlopen(self._metadata["thumbnail"], timeout=5).read()
                pixmap = QPixmap()
                pixmap.loadFromData(data)
                if not pixmap.isNull():
                    thumb.setPixmap(pixmap.scaled(
                        120, 68, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation))
            except Exception:
                pass
        header.addWidget(thumb)

        info = QVBoxLayout()
        title_lbl = QLabel(self._metadata.get("title", "Unknown"))
        title_lbl.setWordWrap(True)
        title_lbl.setStyleSheet("font-size: 14px; font-weight: bold;")
        info.addWidget(title_lbl)
        meta_parts = []
        if self._metadata.get("uploader"):
            meta_parts.append(self._metadata["uploader"])
        if self._metadata.get("duration"):
            meta_parts.append(_fmt_duration(self._metadata["duration"]))
        if meta_parts:
            info_lbl = QLabel(" · ".join(meta_parts))
            info_lbl.setObjectName("muted")
            info.addWidget(info_lbl)
        header.addLayout(info, 1)
        root.addLayout(header)

        # Tabs
        self._tabs = QTabWidget()
        self._video_tab = QWidget()
        self._audio_tab = QWidget()
        self._tabs.addTab(self._video_tab, "🎬 Video")
        self._tabs.addTab(self._audio_tab, "🎵 Audio")
        self._video_list = QListWidget()
        self._video_list.setFrameShape(QListWidget.Shape.NoFrame)
        vlay = QVBoxLayout(self._video_tab)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.addWidget(self._video_list)
        self._audio_list = QListWidget()
        self._audio_list.setFrameShape(QListWidget.Shape.NoFrame)
        alay = QVBoxLayout(self._audio_tab)
        alay.setContentsMargins(0, 0, 0, 0)
        alay.addWidget(self._audio_list)
        root.addWidget(self._tabs, 1)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("iconButton")
        cancel_btn.clicked.connect(self.reject)
        self._start_btn = QPushButton("⬇ Start Download")
        self._start_btn.setObjectName("primaryButton")
        self._start_btn.clicked.connect(self._on_accept)
        self._start_btn.setEnabled(False)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self._start_btn)
        root.addLayout(btn_row)

    def _populate_formats(self) -> None:
        """Parse formats into video and audio tabs."""
        video_formats: list[FormatEntry] = []
        audio_formats: list[FormatEntry] = []

        seen_heights: set[int] = set()
        for fmt in self._formats:
            entry = FormatEntry(fmt)
            self._entries[entry.format_id] = entry
            if entry.is_video:
                if entry.height not in seen_heights:
                    seen_heights.add(entry.height)
                    video_formats.append(entry)
            elif entry.audio_only:
                audio_formats.append(entry)

        # Sort video by height desc, audio by bitrate desc
        video_formats.sort(key=lambda e: e.height, reverse=True)
        audio_formats.sort(key=lambda e: e.abr, reverse=True)

        # Populate video tab — "Best" option first, uses universal format string
        if video_formats:
            best = video_formats[0]
            self._add_format_item(self._video_list, best, is_default=True,
                                  format_id="bv*+ba/b",
                                  label_override=f"⚡ Best Quality ({best.height}p)")
            for entry in video_formats:
                fmt_str = f"{entry.format_id}+bestaudio/best"
                self._add_format_item(self._video_list, entry, format_id=fmt_str)

        # Populate audio tab
        if audio_formats:
            best_audio = audio_formats[0]
            self._add_format_item(self._audio_list, best_audio, is_default=True,
                                  format_id="bestaudio/best",
                                  label_override="⚡ Best Audio",
                                  audio_only=True)
            for entry in audio_formats:
                self._add_format_item(self._audio_list, entry,
                                      format_id=f"{entry.format_id}/best",
                                      audio_only=True)

    def _add_format_item(self, lst: QListWidget, entry: FormatEntry,
                         is_default: bool = False, format_id: str = "",
                         label_override: str = "", audio_only: bool = False) -> None:
        item = QListWidgetItem(lst)
        fid = format_id or entry.format_id
        widget = FormatItemWidget(entry, is_default, emit_id=fid)
        if label_override:
            for child in widget.findChildren(QLabel):
                if child.text().startswith("<b>"):
                    child.setText(f"<b>{label_override}</b>")
                    break
        ao = audio_only
        widget.selected.connect(lambda chosen_id: self._select_format(chosen_id, ao))
        item.setSizeHint(widget.sizeHint())
        lst.addItem(item)
        lst.setItemWidget(item, widget)
        if is_default and not self._selected_id:
            self._select_format(fid, audio_only)

    def _select_format(self, format_id: str, audio_only: bool = False) -> None:
        self._selected_id = format_id
        self._start_btn.setEnabled(True)
        self._result_data = {
            "format": format_id,
            "audio_only": audio_only,
            "audio_format": "bestaudio",
            "audio_format_ext": "mp3",
        }
        # Try to get quality label from the entry
        for entry in self._entries.values():
            if entry.format_id in format_id and entry.is_video:
                self._result_data["quality_label"] = f"{entry.height}p"
                break
        if audio_only:
            self._result_data["quality_label"] = "Audio"
        elif "quality_label" not in self._result_data:
            self._result_data["quality_label"] = "Best"

    def _on_accept(self) -> None:
        if self._selected_id:
            self.accept()

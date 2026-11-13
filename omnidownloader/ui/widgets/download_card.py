"""Download card — one row per transfer, with the state legible at a glance."""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from omnidownloader.core.models import DownloadJob, DownloadState, Priority
from omnidownloader.ui import icons, themes


def _fmt_speed(bps):
    if bps <= 0:
        return "—"
    for u in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024:
            return f"{bps:.1f} {u}"
        bps /= 1024
    return f"{bps:.1f} TB/s"


def _fmt_eta(s):
    if s is None or s <= 0:
        return "—"
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s // 60)}m {int(s % 60)}s"
    return f"{int(s // 3600)}h {int((s % 3600) // 60)}m"


def _fmt_size(b):
    if b <= 0:
        return "?"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"


#: state → (label, pill tone, tile colour role)
_STATE_STYLE = {
    DownloadState.PENDING: ("Queued", "idle", "text_3"),
    DownloadState.EXTRACTING: ("Preparing", "warn", "warning"),
    DownloadState.DOWNLOADING: ("Downloading", "active", "accent"),
    DownloadState.PAUSED: ("Paused", "warn", "warning"),
    DownloadState.MERGING: ("Merging", "warn", "accent"),
    DownloadState.COMPLETED: ("Done", "done", "success"),
    DownloadState.FAILED: ("Failed", "error", "danger"),
    DownloadState.CANCELLED: ("Cancelled", "idle", "text_3"),
}


def _tile_text(job: DownloadJob) -> str:
    """A four-character file-type badge, the way a file manager shows it."""
    name = job.file_name or Path(job.url.split("?")[0]).name
    suffix = Path(name).suffix.lstrip(".").upper()
    if job.url.startswith("magnet:"):
        return "TOR"
    if not suffix:
        return "FILE"
    return suffix[:4]


class DownloadCard(QFrame):
    pause_clicked = pyqtSignal(str)
    resume_clicked = pyqtSignal(str)
    cancel_clicked = pyqtSignal(str)
    open_folder_clicked = pyqtSignal(str)
    preview_clicked = pyqtSignal(str)
    priority_changed = pyqtSignal(str, str)  # job_id, priority_value
    sequential_toggled = pyqtSignal(str, bool)  # job_id, checked

    def __init__(self, job, parent=None):
        super().__init__(parent)
        self._job = job
        self._full_title = job.file_name or job.url[:80]
        self.setObjectName("downloadCard")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumHeight(104)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._build_ui()
        self.apply_theme()
        self.update_progress()

    @property
    def job(self):
        return self._job

    # ── construction ────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(9)

        # ── row 1: tile · title/subtitle · state · actions ──────
        row1 = QHBoxLayout()
        row1.setSpacing(12)

        self._tile = QLabel(_tile_text(self._job))
        self._tile.setObjectName("fileTile")
        self._tile.setFixedSize(42, 42)
        self._tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row1.addWidget(self._tile, 0, Qt.AlignmentFlag.AlignTop)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.setContentsMargins(0, 1, 0, 0)
        self._title = QLabel(self._full_title)
        self._title.setObjectName("cardTitle")
        self._title.setTextFormat(Qt.TextFormat.PlainText)
        self._sub = QLabel(self._subtitle_text())
        self._sub.setObjectName("cardSub")
        text_col.addWidget(self._title)
        text_col.addWidget(self._sub)
        row1.addLayout(text_col, 1)

        quality = str(self._job.metadata.get("quality_label", "") or "")
        self._quality_badge = None
        if quality:
            self._quality_badge = QLabel(quality)
            self._quality_badge.setObjectName("qualityPill")
            self._quality_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row1.addWidget(self._quality_badge, 0, Qt.AlignmentFlag.AlignVCenter)

        self._badge = QLabel("Queued")
        self._badge.setObjectName("pill")
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row1.addWidget(self._badge, 0, Qt.AlignmentFlag.AlignVCenter)

        self._pause_btn = self._action_button("pause", "Pause / resume")
        self._pause_btn.clicked.connect(self._on_pause_or_resume)
        self._cancel_btn = self._action_button("close", "Remove the download")
        self._cancel_btn.clicked.connect(
            lambda: self.cancel_clicked.emit(self._job.id))
        self._open_btn = self._action_button("folder", "Show in folder")
        self._open_btn.clicked.connect(
            lambda: self.open_folder_clicked.emit(self._job.id))
        self._preview_btn = self._action_button("film", "Preview while downloading")
        self._preview_btn.clicked.connect(
            lambda: self.preview_clicked.emit(self._job.id))
        for btn in (self._pause_btn, self._preview_btn,
                    self._open_btn, self._cancel_btn):
            row1.addWidget(btn, 0, Qt.AlignmentFlag.AlignVCenter)

        root.addLayout(row1)

        # ── row 2: progress · percent ───────────────────────────
        row2 = QHBoxLayout()
        row2.setSpacing(10)
        row2.setContentsMargins(0, 0, 0, 0)
        self._bar = QProgressBar()
        self._bar.setObjectName("cardProgress")
        self._bar.setRange(0, 1000)
        self._bar.setValue(0)
        self._bar.setFixedHeight(6)
        self._bar.setTextVisible(False)
        self._bar.setSizePolicy(QSizePolicy.Policy.Expanding,
                                QSizePolicy.Policy.Fixed)
        self._percent = QLabel("0%")
        self._percent.setObjectName("cardPercent")
        self._percent.setFixedWidth(40)
        self._percent.setAlignment(Qt.AlignmentFlag.AlignRight
                                   | Qt.AlignmentFlag.AlignVCenter)
        row2.addWidget(self._bar, 1)
        row2.addWidget(self._percent, 0)
        root.addLayout(row2)

        # ── row 3: meta · priority · sequential ─────────────────
        row3 = QHBoxLayout()
        row3.setSpacing(8)
        self._speed_lbl = QLabel("—")
        self._speed_lbl.setObjectName("cardMeta")
        self._size_lbl = QLabel("0 / ?")
        self._size_lbl.setObjectName("cardMeta")
        self._eta_lbl = QLabel("ETA —")
        self._eta_lbl.setObjectName("cardMeta")
        for label in (self._speed_lbl, self._size_lbl, self._eta_lbl):
            label.setMinimumWidth(0)
        row3.addWidget(self._speed_lbl)
        row3.addWidget(self._dot_separator())
        row3.addWidget(self._size_lbl)
        row3.addWidget(self._dot_separator())
        row3.addWidget(self._eta_lbl)
        row3.addStretch()

        self._priority_combo = QComboBox()
        self._priority_combo.addItems(["High", "Normal", "Low"])
        self._priority_combo.setCurrentText(self._job.priority.value.capitalize())
        self._priority_combo.setFixedWidth(88)
        self._priority_combo.setFixedHeight(26)
        self._priority_combo.setToolTip("Priority in the queue")
        self._priority_combo.currentTextChanged.connect(self._on_priority_changed)
        row3.addWidget(self._priority_combo)

        self._seq_btn = QPushButton("Sequential")
        self._seq_btn.setObjectName("filterChip")
        self._seq_btn.setCheckable(True)
        self._seq_btn.setChecked(self._job.sequential)
        self._seq_btn.setFixedHeight(26)
        self._seq_btn.setToolTip("Fetch the pieces in file order (streaming)")
        self._seq_btn.clicked.connect(self._on_sequential_toggled)
        row3.addWidget(self._seq_btn)
        root.addLayout(row3)

    @staticmethod
    def _dot_separator() -> QLabel:
        label = QLabel("·")
        label.setObjectName("muted")
        label.setFixedWidth(8)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return label

    def _action_button(self, glyph: str, tooltip: str) -> QPushButton:
        btn = QPushButton()
        btn.setObjectName("cardAction")
        btn.setFixedSize(QSize(30, 30))
        btn.setToolTip(tooltip)
        btn.setProperty("glyph", glyph)
        btn.setIconSize(QSize(16, 16))
        return btn

    # ── theming ─────────────────────────────────────────────────
    def apply_theme(self) -> None:
        """Re-tint the painted icons and the file tile for the current theme."""
        c = themes.current_palette()
        state_style = _STATE_STYLE.get(self._job.state, ("", "idle", "text_3"))
        tile_color = getattr(c, state_style[2], c.text_2)
        soft = getattr(c, f"{state_style[2]}_soft", None)
        if soft is None:
            soft = c.surface_3
        self._tile.setStyleSheet(
            f"background-color: {soft}; color: {tile_color};"
            " border-radius: 10px; font-size: 9px; font-weight: 700;"
        )
        paused = self._job.state == DownloadState.PAUSED
        glyphs = {
            self._pause_btn: ("play" if paused else "pause", c.text_2),
            self._cancel_btn: ("close", c.text_3),
            self._open_btn: ("folder", c.text_2),
            self._preview_btn: ("film", c.text_2),
        }
        for btn, (name, color) in glyphs.items():
            enabled = btn.isEnabled()
            btn.setIcon(icons.icon(name, color if enabled else c.text_3, 16))

    # ── text helpers ────────────────────────────────────────────
    def _subtitle_text(self) -> str:
        j = self._job
        if j.state == DownloadState.FAILED and j.error_message:
            return j.error_message[:120]
        host = ""
        try:
            from urllib.parse import urlparse
            host = urlparse(j.url).hostname or ""
        except Exception:  # noqa: BLE001
            host = ""
        if not host and j.url.startswith("magnet:"):
            host = "torrent"
        parts = [p for p in (host, Path(j.file_path).parent.name if j.file_path else "") if p]
        return " · ".join(parts) or j.url[:80]

    def _size_text(self, j) -> str:
        """``done / total`` line, using the size the quality dialog estimated."""
        if j.file_size > 0:
            return f"{_fmt_size(j.downloaded_bytes)} of {_fmt_size(j.file_size)}"
        est = 0
        if isinstance(j.metadata, dict):
            try:
                est = int(j.metadata.get("estimated_size") or 0)
            except (TypeError, ValueError):
                est = 0
        if est > 0 and j.state in (DownloadState.PENDING, DownloadState.EXTRACTING,
                                   DownloadState.DOWNLOADING, DownloadState.PAUSED):
            done = _fmt_size(j.downloaded_bytes) if j.downloaded_bytes > 0 else "0 B"
            return f"{done} of ≈{_fmt_size(est)}"
        return f"{_fmt_size(j.downloaded_bytes)} of {_fmt_size(j.file_size)}"

    def _elide_title(self) -> None:
        full = self._job.file_name or self._job.url[:80]
        budget = max(180, self.width() - 330)
        self._title.setText(self._title.fontMetrics().elidedText(
            full, Qt.TextElideMode.ElideMiddle, budget))

    # ── state ───────────────────────────────────────────────────
    def update_progress(self):
        j = self._job
        self._full_title = j.file_name or j.url[:80]
        self._elide_title()
        self._sub.setText(self._subtitle_text())
        c = themes.current_palette()
        self._sub.setStyleSheet(
            f"color: {c.danger};" if j.state == DownloadState.FAILED else "")

        label, tone, _tile_role = _STATE_STYLE.get(
            j.state, (j.state.value.title(), "idle", "text_3"))
        self._badge.setText(label)
        # A dynamic property drives the QSS pill colour, so it must be
        # re-polished to take effect.
        self._badge.setProperty("tone", tone)
        style = self._badge.style()
        if style is not None:
            style.unpolish(self._badge)
            style.polish(self._badge)

        self._bar.setValue(int(j.progress_percent * 10))
        self._percent.setText(f"{j.progress_percent:.0f}%")
        self._speed_lbl.setText(_fmt_speed(j.speed_bps))
        self._size_lbl.setText(self._size_text(j))

        # A torrent that is waiting for peers has no ETA and no speed: the
        # module's status_note says what it is doing instead.
        note = j.metadata.get("status_note") if isinstance(j.metadata, dict) else None
        if note and j.state in (DownloadState.DOWNLOADING, DownloadState.EXTRACTING):
            self._eta_lbl.setText(str(note)[:60])
        elif j.state == DownloadState.COMPLETED:
            self._eta_lbl.setText("complete")
        elif j.state == DownloadState.PENDING:
            self._eta_lbl.setText("waiting")
        else:
            self._eta_lbl.setText(f"ETA {_fmt_eta(j.eta_seconds)}")

        active = j.state in (DownloadState.DOWNLOADING, DownloadState.EXTRACTING,
                             DownloadState.MERGING)
        paused = j.state == DownloadState.PAUSED
        self._pause_btn.setEnabled(active or paused)
        self._cancel_btn.setEnabled(active or paused or j.state == DownloadState.PENDING)
        self._open_btn.setEnabled(j.state == DownloadState.COMPLETED)
        self._preview_btn.setEnabled(
            j.state in (DownloadState.DOWNLOADING, DownloadState.COMPLETED))
        self._pause_btn.setToolTip("Resume" if paused else "Pause")
        self.apply_theme()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._elide_title()

    # ── slots ───────────────────────────────────────────────────
    def _on_pause_or_resume(self) -> None:
        if self._job.state == DownloadState.PAUSED:
            self.resume_clicked.emit(self._job.id)
        else:
            self.pause_clicked.emit(self._job.id)

    def _on_priority_changed(self, text: str) -> None:
        self.priority_changed.emit(self._job.id, text.lower())

    def _on_sequential_toggled(self, checked: bool) -> None:
        self._job.sequential = checked
        self.sequential_toggled.emit(self._job.id, checked)

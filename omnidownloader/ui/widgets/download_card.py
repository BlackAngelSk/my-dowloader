"""Download progress card widget."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)
from omnidownloader.core.models import DownloadJob, DownloadState, Priority


def _fmt_speed(bps):
    if bps <= 0:
        return "\u2014"
    for u in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024:
            return f"{bps:.1f} {u}"
        bps /= 1024
    return f"{bps:.1f} TB/s"


def _fmt_eta(s):
    if s is None or s <= 0:
        return "\u2014"
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


_STATE_COLORS = {
    DownloadState.PENDING: "#64748B", DownloadState.EXTRACTING: "#F59E0B",
    DownloadState.DOWNLOADING: "#3B82F6", DownloadState.PAUSED: "#F59E0B",
    DownloadState.MERGING: "#8B5CF6", DownloadState.COMPLETED: "#14B8A6",
    DownloadState.FAILED: "#EF4444", DownloadState.CANCELLED: "#64748B",
}


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
        self.setObjectName("downloadCard")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumHeight(90)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._build_ui()

    @property
    def job(self):
        return self._job

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(6)
        row1 = QHBoxLayout()
        row1.setSpacing(10)
        self._dot = QLabel()
        self._dot.setFixedSize(8, 8)
        self._update_dot()
        self._title = QLabel(self._job.file_name or self._job.url[:60])
        self._title.setObjectName("title")
        self._title.setWordWrap(True)
        self._badge = QLabel(self._job.state.value.upper())
        self._badge.setObjectName("muted")
        self._badge.setFixedWidth(80)
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pause_btn = QPushButton("\u23f8")
        self._pause_btn.setObjectName("iconButton")
        self._pause_btn.setFixedSize(32, 32)
        self._pause_btn.clicked.connect(lambda: self.pause_clicked.emit(self._job.id))
        self._cancel_btn = QPushButton("\u23f9")
        self._cancel_btn.setObjectName("iconButton")
        self._cancel_btn.setFixedSize(32, 32)
        self._cancel_btn.clicked.connect(lambda: self.cancel_clicked.emit(self._job.id))
        self._open_btn = QPushButton("\U0001f4c2")
        self._open_btn.setObjectName("iconButton")
        self._open_btn.setFixedSize(32, 32)
        self._open_btn.clicked.connect(lambda: self.open_folder_clicked.emit(self._job.id))
        self._preview_btn = QPushButton("\U0001f3ac")
        self._preview_btn.setObjectName("iconButton")
        self._preview_btn.setFixedSize(32, 32)
        self._preview_btn.setToolTip("Preview media")
        self._preview_btn.clicked.connect(lambda: self.preview_clicked.emit(self._job.id))
        row1.addWidget(self._dot)
        row1.addWidget(self._title, 1)
        # Quality badge for media downloads
        quality = self._job.metadata.get("quality_label", "")
        if quality:
            self._quality_badge = QLabel(quality)
            self._quality_badge.setStyleSheet(
                "background-color: #3B82F6; color: white; border-radius: 4px; "
                "padding: 2px 8px; font-size: 11px; font-weight: bold;"
            )
            self._quality_badge.setFixedWidth(50)
            self._quality_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row1.addWidget(self._quality_badge)
        row1.addWidget(self._badge)
        row1.addWidget(self._pause_btn)
        row1.addWidget(self._cancel_btn)
        row1.addWidget(self._open_btn)
        row1.addWidget(self._preview_btn)
        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)
        self._bar.setValue(0)
        self._bar.setFixedHeight(8)
        self._bar.setTextVisible(False)
        row3 = QHBoxLayout()
        self._speed_lbl = QLabel("\u2014")
        self._speed_lbl.setObjectName("muted")
        self._size_lbl = QLabel("0 / ?")
        self._size_lbl.setObjectName("muted")
        self._eta_lbl = QLabel("ETA: \u2014")
        self._eta_lbl.setObjectName("muted")
        row3.addWidget(self._speed_lbl)
        row3.addWidget(self._size_lbl)
        row3.addStretch()
        row3.addWidget(self._eta_lbl)
        root.addLayout(row1)
        root.addWidget(self._bar)

        # Priority & sequential row
        opt_row = QHBoxLayout()
        self._priority_combo = QComboBox()
        self._priority_combo.addItems(["High", "Normal", "Low"])
        self._priority_combo.setCurrentText(self._job.priority.value.capitalize())
        self._priority_combo.setFixedWidth(100)
        self._priority_combo.currentTextChanged.connect(self._on_priority_changed)
        opt_row.addWidget(QLabel("Priority:"))
        opt_row.addWidget(self._priority_combo)
        self._seq_btn = QPushButton("Sequential")
        self._seq_btn.setObjectName("iconButton")
        self._seq_btn.setCheckable(True)
        self._seq_btn.setChecked(self._job.sequential)
        self._seq_btn.clicked.connect(self._on_sequential_toggled)
        opt_row.addWidget(self._seq_btn)
        opt_row.addStretch()
        root.addLayout(opt_row)

        root.addLayout(row3)

    def _update_dot(self):
        c = _STATE_COLORS.get(self._job.state, "#64748B")
        self._dot.setStyleSheet(f"background-color: {c}; border-radius: 4px;")

    def update_progress(self):
        j = self._job
        self._update_dot()
        self._badge.setText(j.state.value.upper())
        # Show error message on failed jobs
        if j.state == DownloadState.FAILED and j.error_message:
            self._title.setText(f"❌ {j.error_message[:80]}")
        else:
            self._title.setText(j.file_name or j.url[:60])
        self._bar.setValue(int(j.progress_percent * 10))
        self._speed_lbl.setText(_fmt_speed(j.speed_bps))
        self._size_lbl.setText(f"{_fmt_size(j.downloaded_bytes)} / {_fmt_size(j.file_size)}")
        self._eta_lbl.setText(f"ETA: {_fmt_eta(j.eta_seconds)}")
        active = j.state in (DownloadState.DOWNLOADING, DownloadState.EXTRACTING)
        paused = j.state == DownloadState.PAUSED
        self._pause_btn.setText("\u25b6" if paused else "\u23f8")
        self._pause_btn.setEnabled(active or paused)
        self._cancel_btn.setEnabled(active or paused or j.state == DownloadState.PENDING)
        self._open_btn.setEnabled(j.state == DownloadState.COMPLETED)
        self._preview_btn.setEnabled(
            j.state in (DownloadState.DOWNLOADING, DownloadState.COMPLETED)
            and j.streaming_buffer is not None
        )

    def _on_priority_changed(self, text: str) -> None:
        val = text.lower()
        self.priority_changed.emit(self._job.id, val)

    def _on_sequential_toggled(self, checked: bool) -> None:
        self._job.sequential = checked
        self.sequential_toggled.emit(self._job.id, checked)


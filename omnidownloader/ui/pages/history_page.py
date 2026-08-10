"""History page — shows completed/failed downloads."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)
from PyQt6.QtCore import Qt
from omnidownloader.core.models import DownloadJob, DownloadState


def _fmt_size(b: float) -> str:
    if b <= 0:
        return "?"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"


class HistoryCard(QFrame):
    open_folder_clicked = pyqtSignal(str)
    remove_clicked = pyqtSignal(str)

    def __init__(self, job: DownloadJob, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._job = job
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumHeight(60)
        self.setStyleSheet(
            "background-color: {bg}; border: 1px solid {border}; border-radius: 8px; padding: 10px;".format(
                bg="#1E293B", border="#334155"
            )
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)

        icon = "✅" if job.state == DownloadState.COMPLETED else "❌"
        state_color = "#14B8A6" if job.state == DownloadState.COMPLETED else "#EF4444"

        lbl = QLabel(f"<b>{icon} {job.file_name or job.url[:50]}</b>")
        lbl.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(lbl, 1)

        size = QLabel(_fmt_size(job.file_size))
        size.setObjectName("muted")
        size.setFixedWidth(80)
        layout.addWidget(size)

        badge = QLabel(job.state.value.upper())
        badge.setStyleSheet(f"color: {state_color}; font-size: 11px; font-weight: bold;")
        badge.setFixedWidth(80)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(badge)

        open_btn = QPushButton("📂")
        open_btn.setObjectName("iconButton")
        open_btn.setFixedSize(28, 28)
        open_btn.clicked.connect(lambda: self.open_folder_clicked.emit(job.id))
        layout.addWidget(open_btn)

        rm_btn = QPushButton("✕")
        rm_btn.setObjectName("iconButton")
        rm_btn.setFixedSize(28, 28)
        rm_btn.clicked.connect(lambda: self.remove_clicked.emit(job.id))
        layout.addWidget(rm_btn)


class HistoryPage(QWidget):
    open_folder_clicked = pyqtSignal(str)
    remove_clicked = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cards: dict[str, HistoryCard] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        header = QLabel("Download History")
        header.setObjectName("title")
        header.setContentsMargins(24, 16, 24, 8)
        root.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(16, 8, 16, 8)
        self._layout.setSpacing(6)
        self._layout.addStretch()
        self._empty = QLabel("No completed downloads yet.")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setObjectName("muted")
        self._layout.insertWidget(0, self._empty)
        scroll.setWidget(self._container)
        root.addWidget(scroll, 1)

    def add_job(self, job: DownloadJob) -> None:
        card = HistoryCard(job)
        card.open_folder_clicked.connect(self.open_folder_clicked.emit)
        card.remove_clicked.connect(self.remove_clicked.emit)
        self._cards[job.id] = card
        self._layout.insertWidget(self._layout.count() - 1, card)
        self._empty.hide()

    def remove_job(self, job_id: str) -> None:
        card = self._cards.pop(job_id, None)
        if card:
            self._layout.removeWidget(card)
            card.deleteLater()
            if not self._cards:
                self._empty.show()

    def clear(self) -> None:
        for card in list(self._cards.values()):
            self._layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()
        self._empty.show()

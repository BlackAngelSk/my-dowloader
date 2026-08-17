"""Dashboard page — active downloads, queue, speed graph, input bar."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from omnidownloader.core.models import DownloadJob
from omnidownloader.ui.widgets.download_card import DownloadCard
from omnidownloader.ui.widgets.speed_graph import SpeedGraph
from omnidownloader.ui.widgets.url_input_bar import URLInputBar


class DashboardPage(QWidget):
    """Main dashboard with URL input, speed graph, and download cards."""

    url_submitted = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cards: dict[str, DownloadCard] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── URL Input Bar ────────────────────────────────────
        self._input_bar = URLInputBar()
        self._input_bar.url_submitted.connect(self.url_submitted.emit)
        root.addWidget(self._input_bar)

        # ── Speed Graph ──────────────────────────────────────
        graph_container = QWidget()
        graph_layout = QVBoxLayout(graph_container)
        graph_layout.setContentsMargins(16, 8, 16, 8)

        self._global_speed_label = QLabel("Global Speed: —")
        self._global_speed_label.setObjectName("subtitle")
        graph_layout.addWidget(self._global_speed_label)

        self._speed_graph = SpeedGraph()
        graph_layout.addWidget(self._speed_graph)
        root.addWidget(graph_container)

        # ── Scrollable download cards ────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        self._cards_container = QWidget()
        self._cards_layout = QVBoxLayout(self._cards_container)
        self._cards_layout.setContentsMargins(16, 8, 16, 8)
        self._cards_layout.setSpacing(8)
        self._cards_layout.addStretch()

        # Empty state
        self._empty_label = QLabel("No active downloads.\nPaste a URL or drop a file above.")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setObjectName("muted")
        self._cards_layout.insertWidget(0, self._empty_label)

        scroll.setWidget(self._cards_container)
        root.addWidget(scroll, 1)

    # ── Public API ───────────────────────────────────────────

    def add_job(self, job: DownloadJob) -> None:
        card = DownloadCard(job)
        card.pause_clicked.connect(self._on_pause)
        card.resume_clicked.connect(self._on_resume)
        card.cancel_clicked.connect(self._on_cancel)
        card.open_folder_clicked.connect(self._on_open_folder)
        card.preview_clicked.connect(self.preview_clicked.emit)
        card.priority_changed.connect(self.priority_changed.emit)
        card.sequential_toggled.connect(self.sequential_toggled.emit)
        self._cards[job.id] = card
        self._cards_layout.insertWidget(self._cards_layout.count() - 1, card)
        self._empty_label.hide()

    def update_job(self, job: DownloadJob) -> None:
        card = self._cards.get(job.id)
        if card:
            card.update_progress()

    def remove_job(self, job_id: str) -> None:
        card = self._cards.pop(job_id, None)
        if card:
            self._cards_layout.removeWidget(card)
            card.deleteLater()
            if not self._cards:
                self._empty_label.show()

    def update_global_speed(self, speed_bps: float) -> None:
        display_bps = speed_bps
        for u in ("B/s", "KB/s", "MB/s", "GB/s"):
            if display_bps < 1024:
                self._global_speed_label.setText(f"Global Speed: {display_bps:.1f} {u}")
                break
            display_bps /= 1024
        self._speed_graph.add_sample(speed_bps)

    # ── Internal slots — emit signals for MainWindow ──────────

    def _on_pause(self, job_id: str) -> None:
        self.pause_requested.emit(job_id)

    def _on_resume(self, job_id: str) -> None:
        self.resume_requested.emit(job_id)

    def _on_cancel(self, job_id: str) -> None:
        self.cancel_requested.emit(job_id)

    def _on_open_folder(self, job_id: str) -> None:
        self.open_folder_requested.emit(job_id)

    # Signals
    pause_requested = pyqtSignal(str)
    resume_requested = pyqtSignal(str)
    cancel_requested = pyqtSignal(str)
    open_folder_requested = pyqtSignal(str)
    preview_clicked = pyqtSignal(str)
    priority_changed = pyqtSignal(str, str)
    sequential_toggled = pyqtSignal(str, bool)

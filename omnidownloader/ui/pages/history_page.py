"""History — finished, failed and cancelled transfers."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from omnidownloader.core.models import DownloadJob, DownloadState
from omnidownloader.ui import icons, themes


def _fmt_size(b: float) -> str:
    if b <= 0:
        return "unknown size"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


_STATE_PILL = {
    DownloadState.COMPLETED: ("Completed", "done"),
    DownloadState.FAILED: ("Failed", "error"),
    DownloadState.CANCELLED: ("Cancelled", "idle"),
}


class HistoryCard(QFrame):
    """One finished transfer: what it was, how big, what happened to it."""

    open_folder_clicked = pyqtSignal(str)
    remove_clicked = pyqtSignal(str)

    def __init__(self, job: DownloadJob, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._job = job
        self.setObjectName("downloadCard")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(12)

        suffix = Path(job.file_name or job.url).suffix.lstrip(".").upper() or "FILE"
        self._tile = QLabel(suffix[:4])
        self._tile.setObjectName("fileTile")
        self._tile.setFixedSize(34, 34)
        self._tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._tile)

        text = QVBoxLayout()
        text.setSpacing(2)
        self._title = QLabel(job.file_name or job.url[:70])
        self._title.setObjectName("cardTitle")
        self._meta = QLabel(self._meta_text())
        self._meta.setObjectName("cardSub")
        text.addWidget(self._title)
        text.addWidget(self._meta)
        layout.addLayout(text, 1)

        label, tone = _STATE_PILL.get(job.state, (job.state.value.title(), "idle"))
        self._badge = QLabel(label)
        self._badge.setObjectName("pill")
        self._badge.setProperty("tone", tone)
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._badge, 0, Qt.AlignmentFlag.AlignVCenter)

        self._open_btn = QPushButton()
        self._open_btn.setObjectName("cardAction")
        self._open_btn.setFixedSize(QSize(30, 30))
        self._open_btn.setToolTip("Show in folder")
        self._open_btn.setEnabled(bool(job.file_path) and job.state == DownloadState.COMPLETED)
        self._open_btn.clicked.connect(lambda: self.open_folder_clicked.emit(job.id))
        layout.addWidget(self._open_btn)

        self._remove_btn = QPushButton()
        self._remove_btn.setObjectName("cardAction")
        self._remove_btn.setFixedSize(QSize(30, 30))
        self._remove_btn.setToolTip("Remove from history")
        self._remove_btn.clicked.connect(lambda: self.remove_clicked.emit(job.id))
        layout.addWidget(self._remove_btn)

        self.apply_theme()

    @property
    def job(self) -> DownloadJob:
        return self._job

    def _meta_text(self) -> str:
        parts = [_fmt_size(self._job.file_size)]
        if self._job.state == DownloadState.FAILED and self._job.error_message:
            parts.append(self._job.error_message[:90])
        elif self._job.file_path:
            parts.append(str(Path(self._job.file_path).parent))
        return " · ".join(parts)

    def apply_theme(self) -> None:
        c = themes.current_palette()
        role = {DownloadState.COMPLETED: "success",
                DownloadState.FAILED: "danger"}.get(self._job.state, "text_3")
        soft = getattr(c, f"{role}_soft", c.surface_3)
        self._tile.setStyleSheet(
            f"background-color: {soft}; color: {getattr(c, role, c.text_2)};"
            " border-radius: 10px; font-size: 9px; font-weight: 700;")
        self._open_btn.setIcon(icons.icon(
            "folder", c.text_2 if self._open_btn.isEnabled() else c.text_3, 16))
        self._remove_btn.setIcon(icons.icon("close", c.text_3, 16))


class HistoryPage(QWidget):
    open_folder_clicked = pyqtSignal(str)
    remove_clicked = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cards: dict[str, HistoryCard] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 20, 26, 16)
        root.setSpacing(14)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("History")
        title.setObjectName("pageTitle")
        self._subtitle = QLabel("Finished and failed transfers")
        self._subtitle.setObjectName("pageSubtitle")
        titles.addWidget(title)
        titles.addWidget(self._subtitle)
        header.addLayout(titles)
        header.addStretch()
        self._clear_btn = QPushButton("Clear history")
        self._clear_btn.setObjectName("ghostButton")
        self._clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_btn.clicked.connect(self.clear)
        header.addWidget(self._clear_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(0, 0, 2, 0)
        self._layout.setSpacing(10)
        self._layout.addStretch()

        self._empty = QWidget()
        empty_layout = QVBoxLayout(self._empty)
        empty_layout.setContentsMargins(0, 46, 0, 46)
        empty_layout.setSpacing(8)
        icon_label = QLabel()
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setPixmap(icons.pixmap("history", themes.current_palette().text_3, 32))
        icon_label.setMinimumHeight(38)
        empty_title = QLabel("Nothing here yet")
        empty_title.setObjectName("emptyTitle")
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = QLabel("Finished and failed downloads are listed here.")
        hint.setObjectName("emptyHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(icon_label)
        empty_layout.addWidget(empty_title)
        empty_layout.addWidget(hint)
        self._empty._icon_label = icon_label  # type: ignore[attr-defined]

        self._layout.insertWidget(0, self._empty)
        scroll.setWidget(self._container)
        root.addWidget(scroll, 1)
        self._refresh_header()

    def add_job(self, job: DownloadJob) -> None:
        # A cancelled job emits "cancelled" twice (once from cancel_job, once
        # from the cancelled task), which produced two cards for one job and
        # orphaned the first one in the layout forever.
        if job.id in self._cards:
            return
        card = HistoryCard(job)
        card.open_folder_clicked.connect(self.open_folder_clicked.emit)
        card.remove_clicked.connect(self.remove_clicked.emit)
        self._cards[job.id] = card
        self._layout.insertWidget(self._layout.count() - 1, card)
        self._refresh_header()

    def remove_job(self, job_id: str) -> None:
        card = self._cards.pop(job_id, None)
        if card:
            self._layout.removeWidget(card)
            card.deleteLater()
        self._refresh_header()

    def clear(self) -> None:
        for card in list(self._cards.values()):
            self._layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()
        self._refresh_header()

    def restyle(self) -> None:
        label = getattr(self._empty, "_icon_label", None)
        if label is not None:
            label.setPixmap(icons.pixmap("history", themes.current_palette().text_3, 32))
        for card in self._cards.values():
            card.apply_theme()

    def _refresh_header(self) -> None:
        count = len(self._cards)
        self._empty.setVisible(count == 0)
        self._clear_btn.setEnabled(count > 0)
        self._subtitle.setText(
            "Finished and failed transfers" if count == 0
            else f"{count} transfer{'s' if count != 1 else ''}")

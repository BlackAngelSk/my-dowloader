"""Dashboard — header, command bar, throughput, filters and the download list."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from omnidownloader.core.models import DownloadJob, DownloadState
from omnidownloader.ui import icons, themes
from omnidownloader.ui.widgets.download_card import DownloadCard
from omnidownloader.ui.widgets.speed_graph import SpeedGraph
from omnidownloader.ui.widgets.url_input_bar import URLInputBar

#: Which states each filter chip keeps visible.
_FILTER_STATES = {
    "all": None,
    "active": {DownloadState.DOWNLOADING, DownloadState.EXTRACTING,
               DownloadState.MERGING, DownloadState.PENDING},
    "paused": {DownloadState.PAUSED},
    "finished": {DownloadState.COMPLETED, DownloadState.FAILED,
                 DownloadState.CANCELLED},
}


def _fmt_speed(bps: float) -> str:
    if bps <= 0:
        return "0 B/s"
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024:
            return f"{bps:.1f} {unit}"
        bps /= 1024
    return f"{bps:.1f} TB/s"


class StatChip(QWidget):
    """A small metric tile: big value over a quiet caption."""

    def __init__(self, caption: str, value: str = "—",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statChip")
        # A plain QWidget ignores a stylesheet background/border without this.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(104)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 7, 12, 7)
        layout.setSpacing(0)
        self._value = QLabel(value)
        self._value.setObjectName("statValue")
        self._caption = QLabel(caption.upper())
        self._caption.setObjectName("statLabel")
        layout.addWidget(self._value)
        layout.addWidget(self._caption)

    def set_value(self, text: str) -> None:
        self._value.setText(text)


class DashboardPage(QWidget):
    """Main dashboard with URL input, throughput graph, filters and cards."""

    url_submitted = pyqtSignal(str)
    #: Forwarded from the input bar when the user supplied a checksum.
    url_submitted_with_checksum = pyqtSignal(str, str)
    pause_requested = pyqtSignal(str)
    resume_requested = pyqtSignal(str)
    cancel_requested = pyqtSignal(str)
    open_folder_requested = pyqtSignal(str)
    preview_clicked = pyqtSignal(str)
    priority_changed = pyqtSignal(str, str)
    sequential_toggled = pyqtSignal(str, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cards: dict[str, DownloadCard] = {}
        self._filter = "all"
        self._speed_bps = 0.0
        self._build_ui()

    # ── layout ──────────────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 20, 26, 16)
        root.setSpacing(14)

        root.addLayout(self._build_header())
        root.addWidget(self._build_command_bar())
        root.addWidget(self._build_throughput())
        root.addLayout(self._build_filter_row())

        # ── download list ───────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._cards_container = QWidget()
        self._cards_layout = QVBoxLayout(self._cards_container)
        self._cards_layout.setContentsMargins(0, 0, 2, 0)
        self._cards_layout.setSpacing(10)
        self._cards_layout.addStretch()

        self._empty = self._build_empty_state()
        self._cards_layout.insertWidget(0, self._empty)
        self._empty_label = self._empty  # older name kept for callers

        scroll.setWidget(self._cards_container)
        root.addWidget(scroll, 1)

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("Downloads")
        title.setObjectName("pageTitle")
        self._subtitle = QLabel("No transfers yet")
        self._subtitle.setObjectName("pageSubtitle")
        titles.addWidget(title)
        titles.addWidget(self._subtitle)
        row.addLayout(titles)
        row.addStretch()

        self._speed_chip = StatChip("Throughput", "0 B/s")
        self._active_chip = StatChip("Active", "0")
        self._queue_chip = StatChip("Queued", "0")
        for chip in (self._speed_chip, self._active_chip, self._queue_chip):
            row.addWidget(chip, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    def _build_command_bar(self) -> QWidget:
        self._input_bar = URLInputBar()
        self._input_bar.url_submitted.connect(self.url_submitted.emit)
        self._input_bar.url_with_checksum.connect(self.url_submitted_with_checksum.emit)
        return self._input_bar

    def _build_throughput(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("cardPanel")
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        panel.setFixedHeight(86)
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(18)

        labels = QVBoxLayout()
        labels.setSpacing(2)
        caption = QLabel("LIVE THROUGHPUT")
        caption.setObjectName("sectionLabel")
        self._global_speed_label = QLabel("0 B/s")
        self._global_speed_label.setObjectName("statValue")
        self._global_speed_label.setStyleSheet("font-size: 20px; font-weight: 600;")
        hint = QLabel("across every active transfer")
        hint.setObjectName("muted")
        labels.addWidget(caption)
        labels.addWidget(self._global_speed_label)
        labels.addWidget(hint)
        layout.addLayout(labels)

        self._speed_graph = SpeedGraph()
        self._speed_graph.setMinimumHeight(56)
        self._speed_graph.setSizePolicy(QSizePolicy.Policy.Expanding,
                                        QSizePolicy.Policy.Fixed)
        layout.addWidget(self._speed_graph, 1)
        return panel

    def _build_filter_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        self._filter_buttons: dict[str, QPushButton] = {}
        for key, label in (("all", "All"), ("active", "Active"),
                           ("paused", "Paused"), ("finished", "Finished")):
            btn = QPushButton(label)
            btn.setObjectName("filterChip")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setChecked(key == "all")
            btn.clicked.connect(lambda _checked, k=key: self._set_filter(k))
            self._filter_group.addButton(btn)
            self._filter_buttons[key] = btn
            row.addWidget(btn, 0, Qt.AlignmentFlag.AlignLeft)
        row.addStretch()
        self._count_label = QLabel("")
        self._count_label.setObjectName("muted")
        row.addWidget(self._count_label)
        return row

    def _build_empty_state(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 46, 0, 46)
        layout.setSpacing(8)
        icon_label = QLabel()
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setPixmap(icons.pixmap("inbox", themes.current_palette().text_3, 34))
        icon_label.setMinimumHeight(40)
        title = QLabel("Nothing downloading")
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = QLabel("Paste a link above, drop a .torrent, or drag a list of URLs here.")
        hint.setObjectName("emptyHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon_label)
        layout.addWidget(title)
        layout.addWidget(hint)
        panel._icon_label = icon_label  # type: ignore[attr-defined]
        return panel

    # ── public API ──────────────────────────────────────────────
    def add_job(self, job: DownloadJob) -> None:
        card = DownloadCard(job)
        card.pause_clicked.connect(self.pause_requested.emit)
        card.resume_clicked.connect(self.resume_requested.emit)
        card.cancel_clicked.connect(self.cancel_requested.emit)
        card.open_folder_clicked.connect(self.open_folder_requested.emit)
        card.preview_clicked.connect(self.preview_clicked.emit)
        card.priority_changed.connect(self.priority_changed.emit)
        card.sequential_toggled.connect(self.sequential_toggled.emit)
        self._cards[job.id] = card
        self._cards_layout.insertWidget(self._cards_layout.count() - 1, card)
        self._empty.setVisible(not self._cards or self._filter != "all")
        self._apply_filter()
        self._refresh_summary()

    def update_job(self, job: DownloadJob) -> None:
        card = self._cards.get(job.id)
        if card:
            card.update_progress()
            self._apply_filter()
            self._refresh_summary()

    def remove_job(self, job_id: str) -> None:
        card = self._cards.pop(job_id, None)
        if card:
            self._cards_layout.removeWidget(card)
            card.deleteLater()
        self._apply_filter()
        self._refresh_summary()

    def update_global_speed(self, speed_bps: float) -> None:
        self._speed_bps = max(0.0, float(speed_bps))
        self._speed_chip.set_value(_fmt_speed(self._speed_bps))
        self._global_speed_label.setText(_fmt_speed(self._speed_bps))
        self._speed_graph.add_sample(self._speed_bps)
        self._refresh_summary()

    def restyle(self) -> None:
        """Re-tint painted icons after a theme switch."""
        self._input_bar.apply_theme()
        self._speed_graph.set_accent_color(themes.current_palette().accent)
        label = getattr(self._empty, "_icon_label", None)
        if label is not None:
            label.setPixmap(icons.pixmap("inbox", themes.current_palette().text_3, 34))
        for card in self._cards.values():
            card.apply_theme()

    # ── internals ───────────────────────────────────────────────
    def _set_filter(self, key: str) -> None:
        self._filter = key if key in _FILTER_STATES else "all"
        self._apply_filter()

    def _apply_filter(self) -> None:
        allowed = _FILTER_STATES.get(self._filter)
        visible = 0
        for card in self._cards.values():
            show = allowed is None or card.job.state in allowed
            card.setVisible(show)
            visible += 1 if show else 0
        self._empty.setVisible(visible == 0)

    def _refresh_summary(self) -> None:
        states = [card.job.state for card in self._cards.values()]
        downloading = sum(1 for s in states if s in (
            DownloadState.DOWNLOADING, DownloadState.EXTRACTING, DownloadState.MERGING))
        queued = sum(1 for s in states if s == DownloadState.PENDING)
        paused = sum(1 for s in states if s == DownloadState.PAUSED)
        done = sum(1 for s in states if s == DownloadState.COMPLETED)

        self._active_chip.set_value(str(downloading))
        self._queue_chip.set_value(str(queued))

        if not states:
            self._subtitle.setText("No transfers yet")
        else:
            parts = []
            if downloading:
                parts.append(f"{downloading} downloading")
            if queued:
                parts.append(f"{queued} queued")
            if paused:
                parts.append(f"{paused} paused")
            if done:
                parts.append(f"{done} finished")
            self._subtitle.setText(" · ".join(parts) or "Idle")

        total = len(states)
        allowed = _FILTER_STATES.get(self._filter)
        shown = sum(1 for card in self._cards.values()
                    if allowed is None or card.job.state in allowed)
        self._count_label.setText(
            f"{shown} of {total} shown" if shown != total else f"{total} transfers")

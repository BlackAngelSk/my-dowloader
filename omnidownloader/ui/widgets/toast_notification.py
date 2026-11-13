"""Toast notification overlay for clipboard-detected URLs."""

from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from omnidownloader.ui import icons, themes


class ToastNotification(QWidget):
    """A floating card that appears when a downloadable URL is detected."""

    download_clicked = pyqtSignal(str)
    dismissed = pyqtSignal()

    #: Inset of the painted card inside the translucent window (room for the
    #: rounded corner + border anti-aliasing).
    INSET = 1.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(430)
        self.setFixedHeight(78)
        self._url = ""
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._auto_dismiss)

    # ── painting ────────────────────────────────────────────────
    def paintEvent(self, a0) -> None:
        """Draw the card.

        A translucent frameless window does not get a stylesheet background, so
        the rounded card is painted here from the active palette — that is what
        makes the toast readable in both themes.
        """
        palette = themes.current_palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.INSET, self.INSET,
                      self.width() - 2 * self.INSET,
                      self.height() - 2 * self.INSET)
        path = QPainterPath()
        path.addRoundedRect(rect, 14, 14)
        painter.fillPath(path, QColor(palette.surface_3))
        painter.setPen(QPen(QColor(palette.border_strong), 1))
        painter.drawPath(path)
        painter.end()

    # ── construction ────────────────────────────────────────────
    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 14, 12)
        layout.setSpacing(12)

        self._icon = QLabel()
        self._icon.setFixedSize(28, 28)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(1)
        self._title = QLabel("Download detected")
        self._title.setObjectName("cardTitle")
        self._desc = QLabel("")
        self._desc.setObjectName("cardSub")
        text_layout.addWidget(self._title)
        text_layout.addWidget(self._desc)

        self._download_btn = QPushButton("Download")
        self._download_btn.setObjectName("primaryButton")
        self._download_btn.setMinimumWidth(104)
        self._download_btn.setFixedHeight(32)
        self._download_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._download_btn.clicked.connect(self._on_download)

        self._dismiss_btn = QPushButton()
        self._dismiss_btn.setObjectName("cardAction")
        self._dismiss_btn.setFixedSize(28, 28)
        self._dismiss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dismiss_btn.clicked.connect(self._auto_dismiss)

        layout.addWidget(self._icon)
        layout.addLayout(text_layout, 1)
        layout.addWidget(self._download_btn)
        layout.addWidget(self._dismiss_btn)
        self.apply_theme()

    def apply_theme(self) -> None:
        """Re-tint the toast for the active theme (called on theme switches)."""
        palette = themes.current_palette()
        self._icon.setPixmap(icons.pixmap("inbox", palette.accent, 20))
        self._dismiss_btn.setIcon(icons.icon("close", palette.text_3, 14))
        self.update()

    # ── behaviour ───────────────────────────────────────────────
    def show_for_url(self, url: str, auto_dismiss_ms: int = 5000) -> None:
        self._url = url
        display = url[:52] + "…" if len(url) > 52 else url
        self._desc.setText(display)
        self._title.setText("Download detected")
        # A toast is also used for plain notices ("up to date ✓"); offering a
        # Download button for those enqueued invisible non-URLs as real jobs.
        downloadable = self._looks_downloadable(url)
        self._download_btn.setEnabled(downloadable)
        if not downloadable:
            self._title.setText("OmniDownloader")

        # Position at the top-right of the parent — a Qt.Tool window lives in
        # screen coordinates, so the parent's width is not a position.
        parent = self.parent()
        if parent is not None:
            from PyQt6.QtCore import QPoint
            if isinstance(parent, QWidget):
                corner = parent.mapToGlobal(
                    QPoint(max(20, parent.width() - self.width() - 24), 24)
                )
                self.move(corner)
            else:
                self.move(20, 20)

        self.show()
        self.raise_()
        self._timer.start(auto_dismiss_ms)

    @staticmethod
    def _looks_downloadable(text: str) -> bool:
        """True for things worth offering a Download button for."""
        lowered = text.strip().lower()
        return lowered.startswith(("http://", "https://", "magnet:"))

    def _on_download(self) -> None:
        self.download_clicked.emit(self._url)
        self._auto_dismiss()

    def _auto_dismiss(self) -> None:
        self._timer.stop()
        self.hide()
        self.dismissed.emit()

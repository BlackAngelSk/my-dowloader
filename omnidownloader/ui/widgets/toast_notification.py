"""Toast notification overlay for clipboard-detected URLs."""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)


class ToastNotification(QWidget):
    """A floating toast that appears when a downloadable URL is detected."""

    download_clicked = pyqtSignal(str)
    dismissed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(400)
        self.setFixedHeight(70)
        self._url = ""
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._auto_dismiss)

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)

        self._icon = QLabel("🔗")
        self._icon.setFixedSize(30, 30)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)

        text_layout = QVBoxLayout()
        self._title = QLabel("Download Detected")
        self._title.setStyleSheet("font-weight: bold; font-size: 13px;")
        self._desc = QLabel("")
        self._desc.setObjectName("muted")
        self._desc.setStyleSheet("font-size: 11px; color: #94A3B8;")
        self._desc.setMaximumWidth(240)
        text_layout.addWidget(self._title)
        text_layout.addWidget(self._desc)

        self._download_btn = QPushButton("Download")
        self._download_btn.setObjectName("primaryButton")
        self._download_btn.setFixedWidth(80)
        self._download_btn.clicked.connect(self._on_download)

        self._dismiss_btn = QPushButton("✕")
        self._dismiss_btn.setObjectName("iconButton")
        self._dismiss_btn.setFixedSize(28, 28)
        self._dismiss_btn.clicked.connect(self._auto_dismiss)

        layout.addWidget(self._icon)
        layout.addLayout(text_layout, 1)
        layout.addWidget(self._download_btn)
        layout.addWidget(self._dismiss_btn)

        self.setStyleSheet(
            "background-color: #1E293B; border: 1px solid #334155;"
            "border-radius: 10px;"
        )

    def show_for_url(self, url: str, auto_dismiss_ms: int = 5000) -> None:
        self._url = url
        display = url[:50] + "…" if len(url) > 50 else url
        self._desc.setText(display)

        # Position at top-right of parent
        parent = self.parent()
        if parent is not None:
            from PyQt6.QtWidgets import QWidget
            pw = parent.width() if isinstance(parent, QWidget) else self.width()
            self.move(pw - self.width() - 20, 20)

        self.show()
        self.raise_()
        self._timer.start(auto_dismiss_ms)

    def _on_download(self) -> None:
        self.download_clicked.emit(self._url)
        self._auto_dismiss()

    def _auto_dismiss(self) -> None:
        self._timer.stop()
        self.hide()
        self.dismissed.emit()

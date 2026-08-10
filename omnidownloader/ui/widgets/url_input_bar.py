"""Smart URL input bar with auto-detect, paste, and add button."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QWidget


class URLInputBar(QWidget):
    """Top-bar URL input with paste and download trigger."""

    url_submitted = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(8)

        self._input = QLineEdit()
        self._input.setObjectName("urlInput")
        self._input.setPlaceholderText(
            "Paste a URL — or prefix with 'scrape:' to extract images from any page"
        )
        self._input.setMinimumHeight(44)
        self._input.returnPressed.connect(self._on_submit)

        self._paste_btn = QPushButton("📋 Paste")
        self._paste_btn.setObjectName("iconButton")
        self._paste_btn.setFixedWidth(80)
        self._paste_btn.setFixedHeight(44)
        self._paste_btn.setToolTip("Paste from clipboard")
        self._paste_btn.clicked.connect(self._on_paste)

        self._add_btn = QPushButton("＋ Add")
        self._add_btn.setObjectName("primaryButton")
        self._add_btn.setFixedWidth(100)
        self._add_btn.setFixedHeight(44)
        self._add_btn.clicked.connect(self._on_submit)

        layout.addWidget(self._input, 1)
        layout.addWidget(self._paste_btn)
        layout.addWidget(self._add_btn)

    def set_url(self, url: str) -> None:
        self._input.setText(url)

    def clear(self) -> None:
        self._input.clear()

    def focus_input(self) -> None:
        self._input.setFocus()

    def _on_paste(self) -> None:
        from PyQt6.QtWidgets import QApplication
        clipboard = QApplication.clipboard()
        if clipboard:
            text = clipboard.text()
            if text:
                self._input.setText(text.strip())

    def _on_submit(self) -> None:
        url = self._input.text().strip()
        if url:
            self.url_submitted.emit(url)
            self._input.clear()

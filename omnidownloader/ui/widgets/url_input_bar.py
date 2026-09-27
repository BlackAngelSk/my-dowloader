"""Smart URL input bar with auto-detect, paste, and add button."""

from __future__ import annotations

import re

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QWidget

#: "https://… sha256:abc…" — an expected digest typed after the URL.
CHECKSUM_RE = re.compile(r"\b(sha256|sha512|sha1|md5):([0-9a-fA-F]{32,128})\b")


class URLInputBar(QWidget):
    """Top-bar URL input with paste and download trigger."""

    url_submitted = pyqtSignal(str)
    #: URL plus an expected checksum, when one was supplied.
    url_with_checksum = pyqtSignal(str, str)

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
        url, checksum = self.parse_input(self._input.text())
        if not url:
            return
        if checksum:
            self.url_with_checksum.emit(url, checksum)
        else:
            self.url_submitted.emit(url)
        self._input.clear()

    @staticmethod
    def parse_input(text: str) -> tuple[str, str]:
        """Split ``"<url> sha256:<hex>"`` into (url, checksum).

        Checksums are optional: paste ``https://host/file.iso sha256:abc…``
        and the digest is verified once the download finishes.
        """
        raw = (text or "").strip()
        if not raw:
            return "", ""
        checksum = ""
        match = CHECKSUM_RE.search(raw)
        if match:
            checksum = f"{match.group(1).lower()}:{match.group(2).lower()}"
            raw = (raw[:match.start()] + " " + raw[match.end():]).strip()
        url = raw.split()[0] if raw.split() else ""
        return url, checksum

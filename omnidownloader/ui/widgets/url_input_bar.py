"""Command bar — paste a link, optionally with a checksum, and start it."""

from __future__ import annotations

import re

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QWidget

from omnidownloader.core.url_utils import normalize_url
from omnidownloader.ui import icons, themes

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
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self._input = QLineEdit()
        self._input.setObjectName("urlInput")
        self._input.setPlaceholderText(
            "Paste a link — https:// is optional, or prefix with 'scrape:'"
        )
        self._input.setMinimumHeight(46)
        self._input.setClearButtonEnabled(True)
        self._input.returnPressed.connect(self._on_submit)
        # Leading action = an icon inside the field, no extra widget/geometry.
        self._lead_action = self._input.addAction(
            icons.icon("download", themes.current_palette().text_3, 18),
            QLineEdit.ActionPosition.LeadingPosition,
        )

        self._paste_btn = QPushButton("Paste")
        self._paste_btn.setObjectName("ghostButton")
        self._paste_btn.setMinimumHeight(46)
        self._paste_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._paste_btn.setToolTip("Paste from the clipboard")
        self._paste_btn.clicked.connect(self._on_paste)

        self._add_btn = QPushButton("Download")
        self._add_btn.setObjectName("primaryButton")
        self._add_btn.setMinimumHeight(46)
        self._add_btn.setMinimumWidth(126)
        self._add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_btn.setIconSize(QSize(16, 16))
        self._add_btn.clicked.connect(self._on_submit)

        layout.addWidget(self._input, 1)
        layout.addWidget(self._paste_btn)
        layout.addWidget(self._add_btn)
        self.apply_theme()

    def apply_theme(self) -> None:
        c = themes.current_palette()
        if self._lead_action is not None:
            self._lead_action.setIcon(icons.icon("download", c.text_3, 18))
        self._paste_btn.setIcon(icons.icon("paste", c.text_2, 16))
        self._add_btn.setIcon(icons.icon("plus", c.on_accent, 16))

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
        # Browsers hide the scheme ("youtube.com/watch?v=…") and paste can bring
        # zero-width characters along; repair both before the router sees them.
        url = normalize_url(raw.split()[0]) if raw.split() else ""
        return url, checksum

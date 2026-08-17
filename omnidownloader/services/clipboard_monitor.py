"""Clipboard Monitor — polls the system clipboard for downloadable URLs."""

from __future__ import annotations

import re
from typing import Optional

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication, QWidget

# Regex for common URLs
URL_RE = re.compile(
    r'https?://[^\s<>"\')\]]+',
    re.IGNORECASE,
)

# Domains we recognize as downloadable
SUPPORTED_DOMAINS = {
    "youtube.com", "youtu.be", "m.youtube.com",
    "twitter.com", "x.com",
    "tiktok.com", "vm.tiktok.com",
    "instagram.com",
    "facebook.com", "fb.watch",
    "reddit.com", "v.redd.it",
    "twitch.tv", "clips.twitch.tv",
    "imgur.com", "flickr.com",
    "soundcloud.com",
    "rutube.ru", "www.rutube.ru",
    # Popular platforms
    "kick.com",
    "ok.ru",
    "dzen.ru",
    "nicovideo.jp", "nico.ms",
    "odysee.com", "odys.ly",
    "archive.org",
}


class ClipboardMonitor(QWidget):
    """Polls clipboard at regular intervals and emits detected URLs."""

    url_detected = pyqtSignal(str)

    def __init__(
        self,
        poll_interval_ms: int = 1000,
        auto_add: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._auto_add = auto_add
        self._last_clipboard = ""
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(poll_interval_ms)

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _poll(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return
        text = clipboard.text()
        if not text or text == self._last_clipboard:
            return
        self._last_clipboard = text

        for match in URL_RE.finditer(text):
            url = match.group(0)
            if self._is_downloadable(url):
                self.url_detected.emit(url)
                break  # one at a time

    @staticmethod
    def _is_downloadable(url: str) -> bool:
        from urllib.parse import urlparse
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").removeprefix("www.")
            if host in SUPPORTED_DOMAINS:
                return True
            path = parsed.path.lower()
            if path.endswith(".torrent"):
                return True
            if path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                return True
            if url.startswith("magnet:"):
                return True
            return False
        except Exception:
            return False

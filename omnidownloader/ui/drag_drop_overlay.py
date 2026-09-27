"""Drag-and-drop overlay widget for dropping .torrent files, URL lists and URLs."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

logger = logging.getLogger(__name__)

#: URLs we can actually hand to a module.
URL_RE = re.compile(r'(https?://[^\s<>"\']+|magnet:\?[^\s<>"\']+)', re.IGNORECASE)

#: Dropped text files we treat as link lists.
LIST_SUFFIXES = (".txt", ".list", ".urls", ".csv", ".md")
#: Dropped files that a download module can consume directly.
FILE_SUFFIXES = (".torrent",)


class DragDropOverlay(QWidget):
    """Semi-transparent overlay shown when files/URLs are dragged over the window."""

    files_dropped = pyqtSignal(list)
    urls_dropped = pyqtSignal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.hide()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        layout = QVBoxLayout(self)
        label = QLabel("\U0001f4c2\nDrop files, .torrent, URL lists, or URLs here")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet(
            "font-size: 20px; font-weight: bold; color: #F1F5F9;"
            "background: rgba(15, 23, 42, 0.85); border: 3px dashed #3B82F6;"
            "border-radius: 20px; padding: 60px;"
        )
        layout.addWidget(label)

    def activate(self) -> None:
        self.show()
        self.raise_()

    def deactivate(self) -> None:
        self.hide()

    def dragEnterEvent(self, a0: QDragEnterEvent | None) -> None:
        if a0 is None:
            return
        mime = a0.mimeData()
        if mime is not None and (mime.hasUrls() or mime.hasText()):
            a0.acceptProposedAction()
            self.activate()

    def dragLeaveEvent(self, a0) -> None:
        self.deactivate()

    def dropEvent(self, a0: QDropEvent | None) -> None:
        self.deactivate()
        if a0 is None:
            return
        mime = a0.mimeData()
        if mime is None:
            return

        files: list[str] = []
        urls: list[str] = []
        list_files: list[Path] = []

        if mime.hasUrls():
            for url in mime.urls():
                path = url.toLocalFile()
                if not path:
                    urls.append(url.toString())
                    continue
                lowered = path.lower()
                if lowered.endswith(FILE_SUFFIXES):
                    # A module (torrent) handles a bare filesystem path.
                    files.append(path)
                elif lowered.endswith(LIST_SUFFIXES):
                    list_files.append(Path(path))
                else:
                    files.append(path)

        if mime.hasText():
            urls.extend(self.extract_urls(mime.text()))

        for list_file in list_files:
            try:
                content = list_file.read_text(errors="replace")
            except OSError as exc:
                logger.warning("Could not read dropped file %s: %s", list_file, exc)
                continue
            found = self.extract_urls(content)
            logger.info("Read %d URL(s) from dropped %s", len(found), list_file.name)
            urls.extend(found)

        # Deduplicate while preserving order.
        seen: set[str] = set()
        urls = [u for u in urls if not (u in seen or seen.add(u))]

        if files:
            self.files_dropped.emit(files)
        if urls:
            self.urls_dropped.emit(urls)
        a0.acceptProposedAction()

    @staticmethod
    def extract_urls(text: str) -> list[str]:
        """Pull every downloadable URL out of *text*.

        A dropped text file used to be handed to the queue whole, so a pasted
        list of twenty links became one nonsense job.
        """
        return [match.group(0).rstrip('.,;:)"\'') for match in URL_RE.finditer(text or "")]

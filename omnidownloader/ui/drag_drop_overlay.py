"""Drag-and-drop overlay widget for dropping .torrent files and URLs."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget


class DragDropOverlay(QWidget):
    """Semi-transparent overlay shown when files/URLs are dragged over the window."""

    files_dropped = pyqtSignal(list)
    urls_dropped = pyqtSignal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.hide()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        layout = QVBoxLayout(self)
        label = QLabel("\U0001f4c2\nDrop files, .torrent, or URLs here")
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
        if mime.hasUrls():
            files = []
            urls = []
            for url in mime.urls():
                path = url.toLocalFile()
                if path:
                    files.append(path)
                else:
                    urls.append(url.toString())
            if files:
                self.files_dropped.emit(files)
            if urls:
                self.urls_dropped.emit(urls)
        elif mime.hasText():
            text = mime.text().strip()
            if text:
                self.urls_dropped.emit([text])
        a0.acceptProposedAction()


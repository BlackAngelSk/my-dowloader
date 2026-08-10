"""Update Dialog — shows available update with changelog, download progress, and restart."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QTextEdit, QVBoxLayout, QWidget,
)

if TYPE_CHECKING:
    from omnidownloader.services.update_service import UpdateInfo, UpdateService

logger = logging.getLogger(__name__)


class UpdateDialog(QDialog):
    """Modal dialog that presents an available update to the user."""

    download_requested = pyqtSignal(object)  # UpdateInfo
    install_requested = pyqtSignal(str)      # file path
    restart_requested = pyqtSignal()

    def __init__(self, info: "UpdateInfo", update_service: "UpdateService",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._info = info
        self._svc = update_service
        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self) -> None:
        self.setWindowTitle("Update Available")
        self.setMinimumWidth(480)
        self.setMaximumWidth(560)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 24, 24, 24)

        # ── Header ─────────────────────────────────────────────
        header = QLabel(
            f"🚀  A new version of OmniDownloader is available!"
        )
        header.setObjectName("title")
        header.setWordWrap(True)
        layout.addWidget(header)

        version_info = QLabel(
            f"<b>Current version:</b> 0.1.0 &nbsp;&nbsp;→&nbsp;&nbsp; "
            f"<b>New version:</b> {self._info.version}"
        )
        version_info.setWordWrap(True)
        layout.addWidget(version_info)

        # ── Changelog ──────────────────────────────────────────
        if self._info.body:
            cl_label = QLabel("<b>Changelog:</b>")
            layout.addWidget(cl_label)
            changelog = QTextEdit()
            changelog.setPlainText(self._info.body)
            changelog.setReadOnly(True)
            changelog.setMaximumHeight(160)
            layout.addWidget(changelog)

        # ── Progress bar (hidden until download starts) ────────
        self._progress_bar = QProgressBar()
        self._progress_bar.setValue(0)
        self._progress_bar.setVisible(False)
        layout.addWidget(self._progress_bar)

        self._progress_label = QLabel("")
        self._progress_label.setVisible(False)
        layout.addWidget(self._progress_label)

        # ── Buttons ────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self._skip_btn = QPushButton("Skip This Version")
        self._skip_btn.setObjectName("iconButton")
        self._skip_btn.clicked.connect(self.reject)

        self._download_btn = QPushButton("Download & Install")
        self._download_btn.setObjectName("primaryButton")
        self._download_btn.clicked.connect(self._on_download)

        self._restart_btn = QPushButton("Restart Now")
        self._restart_btn.setObjectName("primaryButton")
        self._restart_btn.setVisible(False)
        self._restart_btn.clicked.connect(self._on_restart)

        btn_row.addWidget(self._skip_btn)
        btn_row.addWidget(self._download_btn)
        btn_row.addWidget(self._restart_btn)
        layout.addLayout(btn_row)

    def _connect_signals(self) -> None:
        self._svc.download_progress.connect(self._on_progress)
        self._svc.download_finished.connect(self._on_download_finished)

    def _on_download(self) -> None:
        self._download_btn.setEnabled(False)
        self._download_btn.setText("Downloading…")
        self._progress_bar.setVisible(True)
        self._progress_label.setVisible(True)
        self._progress_bar.setValue(0)
        self.download_requested.emit(self._info)

    def _on_progress(self, pct: int, speed: str) -> None:
        self._progress_bar.setValue(pct)
        self._progress_label.setText(speed)

    def _on_download_finished(self, file_path: str) -> None:
        self._progress_bar.setValue(100)
        self._progress_label.setText("Download complete — ready to install")
        self._download_btn.setVisible(False)
        self._skip_btn.setVisible(False)
        self._restart_btn.setVisible(True)
        self._restart_btn.setFocus()
        self.install_requested.emit(file_path)

    def _on_restart(self) -> None:
        self.restart_requested.emit()
        self.accept()

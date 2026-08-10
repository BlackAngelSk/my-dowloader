"""Settings panel widget — download directory, concurrent connections, etc."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)


class SettingsPanel(QWidget):
    """Global settings form with save/reset signals."""

    settings_changed = pyqtSignal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        title = QLabel("Settings")
        title.setObjectName("title")
        root.addWidget(title)

        # ── Downloads group ──────────────────────────────────
        dl_group = QGroupBox("Downloads")
        dl_form = QFormLayout()

        self._dir_layout = QHBoxLayout()
        self._dir_input = QLineEdit()
        self._dir_input.setPlaceholderText("~/Downloads/OmniDownloader")
        self._browse_btn = QPushButton("Browse…")
        self._browse_btn.clicked.connect(self._browse_dir)
        self._dir_layout.addWidget(self._dir_input, 1)
        self._dir_layout.addWidget(self._browse_btn)
        dl_form.addRow("Download Directory:", self._dir_layout)

        self._concurrent_spin = QSpinBox()
        self._concurrent_spin.setRange(1, 32)
        self._concurrent_spin.setValue(4)
        dl_form.addRow("Max Concurrent Downloads:", self._concurrent_spin)

        self._speed_limit = QLineEdit()
        self._speed_limit.setPlaceholderText("0 = unlimited")
        dl_form.addRow("Global Speed Limit (KB/s):", self._speed_limit)

        self._per_task_limit = QLineEdit()
        self._per_task_limit.setPlaceholderText("0 = unlimited")
        dl_form.addRow("Per-Task Speed Limit (KB/s):", self._per_task_limit)

        dl_group.setLayout(dl_form)
        root.addWidget(dl_group)

        # ── Theme group ──────────────────────────────────────
        theme_group = QGroupBox("Appearance")
        theme_form = QFormLayout()
        self._theme_combo = QComboBox()
        self._theme_combo.addItems(["Auto", "Dark", "Light"])
        theme_form.addRow("Theme:", self._theme_combo)
        theme_group.setLayout(theme_form)
        root.addWidget(theme_group)

        # ── Updates group ──────────────────────────────────────
        update_group = QGroupBox("Updates")
        update_layout = QHBoxLayout()

        from omnidownloader import __version__
        self._version_label = QLabel(f"OmniDownloader v{__version__}")
        self._version_label.setStyleSheet("font-weight: bold;")
        update_layout.addWidget(self._version_label)
        update_layout.addStretch()

        self._check_update_btn = QPushButton("Check for Updates")
        self._check_update_btn.setObjectName("primaryButton")
        update_layout.addWidget(self._check_update_btn)

        update_group.setLayout(update_layout)
        root.addWidget(update_group)

        # ── Buttons ──────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._save_btn = QPushButton("Save Settings")
        self._save_btn.setObjectName("primaryButton")
        self._save_btn.clicked.connect(self._on_save)
        self._reset_btn = QPushButton("Reset Defaults")
        self._reset_btn.setObjectName("iconButton")
        self._reset_btn.clicked.connect(self._on_reset)
        btn_row.addWidget(self._reset_btn)
        btn_row.addWidget(self._save_btn)
        root.addLayout(btn_row)

        root.addStretch()

    def _browse_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select Download Directory")
        if d:
            self._dir_input.setText(d)

    def get_settings(self) -> dict:
        return {
            "download_dir": self._dir_input.text(),
            "max_concurrent": self._concurrent_spin.value(),
            "speed_limit_kbs": self._speed_limit.text(),
            "per_task_limit_kbs": self._per_task_limit.text(),
            "theme": self._theme_combo.currentText().lower(),
        }

    def _on_save(self) -> None:
        self.settings_changed.emit(self.get_settings())

    def _on_reset(self) -> None:
        self._dir_input.clear()
        self._concurrent_spin.setValue(4)
        self._speed_limit.clear()
        self._per_task_limit.clear()
        self._theme_combo.setCurrentIndex(0)

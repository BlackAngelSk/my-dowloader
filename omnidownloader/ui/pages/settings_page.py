"""Settings page — wraps SettingsPanel and adds about section."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from omnidownloader.ui.widgets.settings_panel import SettingsPanel


class SettingsPage(QWidget):
    settings_changed = pyqtSignal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._panel = SettingsPanel()
        self._panel.settings_changed.connect(self.settings_changed.emit)
        layout.addWidget(self._panel)
        layout.addStretch()

    def set_defaults(self, settings: dict) -> None:
        if "download_dir" in settings:
            self._panel._dir_input.setText(settings["download_dir"])
        if "max_concurrent" in settings:
            self._panel._concurrent_spin.setValue(settings["max_concurrent"])
        if "theme" in settings:
            idx = {"auto": 0, "dark": 1, "light": 2}.get(settings["theme"], 0)
            self._panel._theme_combo.setCurrentIndex(idx)

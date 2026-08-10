"""Scheduler page — wraps SchedulerPanel for the sidebar navigation."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from omnidownloader.ui.widgets.scheduler_panel import SchedulerPanel


class SchedulerPage(QWidget):
    rules_changed = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 16)
        self._panel = SchedulerPanel()
        self._panel.rules_changed.connect(self.rules_changed.emit)
        layout.addWidget(self._panel)
        layout.addStretch()

    def set_rules(self, rules) -> None:
        self._panel.set_rules(rules)

    def update_active_rule(self, name: str) -> None:
        self._panel.update_active_rule(name)

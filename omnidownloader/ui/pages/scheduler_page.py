"""Scheduler page — wraps SchedulerPanel for the sidebar navigation."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from omnidownloader.ui.widgets.scheduler_panel import SchedulerPanel


class SchedulerPage(QWidget):
    rules_changed = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 20, 26, 16)
        layout.setSpacing(14)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("Scheduler")
        title.setObjectName("pageTitle")
        subtitle = QLabel(
            "Time-based rules that change the speed limit — for downloads you leave overnight.")
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch()
        layout.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._panel = SchedulerPanel()
        self._panel.rules_changed.connect(self.rules_changed.emit)
        scroll.setWidget(self._panel)
        layout.addWidget(scroll, 1)

    def set_rules(self, rules) -> None:
        self._panel.set_rules(rules)

    def update_active_rule(self, name: str) -> None:
        self._panel.update_active_rule(name)

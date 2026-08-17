"""Scheduler panel widget — manage time-based bandwidth rules."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from omnidownloader.core.scheduler import SchedulerRule


def _speed_to_display(bps: float) -> str:
    if bps <= 0:
        return "0"
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f}"
    return f"{bps / 1024:.0f}"


def _display_to_speed(text: str, unit: str) -> float:
    try:
        val = float(text)
    except ValueError:
        return 0.0
    if unit == "MB/s":
        return val * 1024 * 1024
    elif unit == "KB/s":
        return val * 1024
    return val


class SchedulerPanel(QWidget):
    """Form for managing scheduler rules."""

    rules_changed = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rules: list[SchedulerRule] = []
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        title = QLabel("Bandwidth Scheduler Rules")
        title.setObjectName("title")
        root.addWidget(title)

        desc = QLabel(
            "Set time-based rules to automatically adjust download speed."
        )
        desc.setObjectName("muted")
        desc.setWordWrap(True)
        root.addWidget(desc)

        # Add new rule form
        add_group = QGroupBox("Add New Rule")
        form = QFormLayout()
        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("e.g. Night Mode")
        form.addRow("Rule Name:", self._name_input)
        add_group.setLayout(form)
        root.addWidget(add_group)

        # Time row
        time_form = QFormLayout()
        time_row = QHBoxLayout()
        self._start_h = QSpinBox(); self._start_h.setRange(0, 23); self._start_h.setValue(2)
        self._start_m = QSpinBox(); self._start_m.setRange(0, 59)
        time_row.addWidget(QLabel("H:")); time_row.addWidget(self._start_h)
        time_row.addWidget(QLabel("M:")); time_row.addWidget(self._start_m)
        time_form.addRow("Start Time:", time_row)
        time_row2 = QHBoxLayout()
        self._end_h = QSpinBox(); self._end_h.setRange(0, 23); self._end_h.setValue(8)
        self._end_m = QSpinBox(); self._end_m.setRange(0, 59)
        time_row2.addWidget(QLabel("H:")); time_row2.addWidget(self._end_h)
        time_row2.addWidget(QLabel("M:")); time_row2.addWidget(self._end_m)
        time_form.addRow("End Time:", time_row2)
        speed_row = QHBoxLayout()
        self._speed_input = QLineEdit()
        self._speed_input.setPlaceholderText("0 = unlimited")
        speed_row.addWidget(self._speed_input, 1)
        self._speed_unit = QComboBox()
        self._speed_unit.addItems(["MB/s", "KB/s", "B/s"])
        self._speed_unit.setCurrentText("MB/s")
        speed_row.addWidget(self._speed_unit)
        time_form.addRow("Speed Limit:", speed_row)
        add_group.setLayout(time_form)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._add_btn = QPushButton("+ Add Rule")
        self._add_btn.setObjectName("primaryButton")
        self._add_btn.clicked.connect(self._add_rule)
        self._preset_btn = QPushButton("Load Night/Day Preset")
        self._preset_btn.setObjectName("iconButton")
        self._preset_btn.clicked.connect(self._load_preset)
        btn_row.addWidget(self._preset_btn)
        btn_row.addWidget(self._add_btn)
        root.addLayout(btn_row)

        # Active rules list
        self._rules_group = QGroupBox("Active Rules")
        self._rules_layout = QVBoxLayout()
        self._rules_group.setLayout(self._rules_layout)
        self._empty_label = QLabel("No rules. Add one above or load preset.")
        self._empty_label.setObjectName("muted")
        self._rules_layout.addWidget(self._empty_label)
        root.addWidget(self._rules_group)

        self._active_lbl = QLabel("Active rule: None")
        self._active_lbl.setStyleSheet("font-weight: bold; color: #14B8A6;")
        root.addWidget(self._active_lbl)
        root.addStretch()

    def set_rules(self, rules: list) -> None:
        self._rules = list(rules)
        self._refresh_rules_list()

    def update_active_rule(self, name: str) -> None:
        if name:
            self._active_lbl.setText(f"Active rule: {name}")
        else:
            self._active_lbl.setText("Active rule: None (unlimited)")

    def _add_rule(self) -> None:
        name = self._name_input.text().strip() or f"Rule {len(self._rules) + 1}"
        speed = _display_to_speed(
            self._speed_input.text().strip() or "0",
            self._speed_unit.currentText(),
        )
        rule = SchedulerRule(
            name=name,
            start_hour=self._start_h.value(), start_minute=self._start_m.value(),
            end_hour=self._end_h.value(), end_minute=self._end_m.value(),
            global_speed_limit=speed,
        )
        self._rules.append(rule)
        self._refresh_rules_list()
        self.rules_changed.emit([r.to_dict() for r in self._rules])
        self._name_input.clear()
        self._speed_input.clear()

    def _remove_rule(self, idx: int) -> None:
        if 0 <= idx < len(self._rules):
            self._rules.pop(idx)
            self._refresh_rules_list()
            self.rules_changed.emit([r.to_dict() for r in self._rules])

    def _load_preset(self) -> None:
        self._rules = [
            SchedulerRule(name="Night Mode (Unlimited)",
                          start_hour=2, start_minute=0, end_hour=8, end_minute=0,
                          global_speed_limit=0.0),
            SchedulerRule(name="Day Mode (2 MB/s cap)",
                          start_hour=8, start_minute=0, end_hour=2, end_minute=0,
                          global_speed_limit=2*1024*1024),
        ]
        self._refresh_rules_list()
        self.rules_changed.emit([r.to_dict() for r in self._rules])

    def _refresh_rules_list(self) -> None:
        while self._rules_layout.count():
            item = self._rules_layout.takeAt(0)
            if item is not None:
                w = item.widget()
                if w:
                    w.deleteLater()
        if not self._rules:
            lbl = QLabel("No rules configured.")
            lbl.setObjectName("muted")
            self._rules_layout.addWidget(lbl)
            return
        for i, rule in enumerate(self._rules):
            row = QHBoxLayout()
            info = (
                f"<b>{rule.name}</b>  "
                f"{rule.start_hour:02d}:{rule.start_minute:02d}-"
                f"{rule.end_hour:02d}:{rule.end_minute:02d}  "
                f"{'unlimited' if rule.global_speed_limit <= 0 else _speed_to_display(rule.global_speed_limit) + ' MB/s'}"
            )
            lbl = QLabel(info)
            lbl.setTextFormat(Qt.TextFormat.RichText)
            row.addWidget(lbl, 1)
            rm = QPushButton("\u2715")
            rm.setObjectName("iconButton")
            rm.setFixedSize(28, 28)
            idx = i
            rm.clicked.connect(lambda _, idx=idx: self._remove_rule(idx))
            row.addWidget(rm)
            container = QWidget()
            container.setLayout(row)
            self._rules_layout.addWidget(container)
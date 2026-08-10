"""Anonymity & Privacy page — proxy config, Tor, kill switch, IP check."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)


class AnonymityPage(QWidget):
    proxy_config_changed = pyqtSignal(dict)
    tor_toggle = pyqtSignal(bool)
    kill_switch_toggle = pyqtSignal(bool)
    ip_check_requested = pyqtSignal()
    tor_rotate_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # Status Card
        sg = QGroupBox("Connection Status")
        sl = QVBoxLayout()
        self._ip_label = QLabel("⚪ Not checked yet")
        self._ip_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        sl.addWidget(self._ip_label)
        self._country_label = QLabel("")
        self._country_label.setObjectName("subtitle")
        sl.addWidget(self._country_label)
        self._latency_label = QLabel("")
        self._latency_label.setObjectName("muted")

        # Proxy Configuration
        pg = QGroupBox("Proxy Configuration")
        pf = QFormLayout()
        self._proxy_enabled = QPushButton("OFF")
        self._proxy_enabled.setCheckable(True)
        self._proxy_enabled.setFixedWidth(60)
        self._proxy_enabled.clicked.connect(self._on_proxy_toggled)
        self._proxy_type = QComboBox()
        self._proxy_type.addItems(["SOCKS5", "SOCKS4", "HTTP", "HTTPS"])
        self._proxy_host = QLineEdit()
        self._proxy_host.setPlaceholderText("127.0.0.1")
        self._proxy_port = QSpinBox()
        self._proxy_port.setRange(1, 65535)
        self._proxy_port.setValue(1080)
        self._proxy_user = QLineEdit()
        self._proxy_user.setPlaceholderText("optional")
        self._proxy_pass = QLineEdit()
        self._proxy_pass.setPlaceholderText("optional")
        self._proxy_pass.setEchoMode(QLineEdit.EchoMode.Password)
        pf.addRow("Enable:", self._proxy_enabled)
        pf.addRow("Type:", self._proxy_type)
        pf.addRow("Host:", self._proxy_host)
        pf.addRow("Port:", self._proxy_port)
        pf.addRow("Username:", self._proxy_user)
        pf.addRow("Password:", self._proxy_pass)
        pg.setLayout(pf)
        root.addWidget(pg)

        # Tor
        tg = QGroupBox("Tor Network")
        tf = QFormLayout()
        self._tor_btn = QPushButton("OFF")
        self._tor_btn.setCheckable(True)
        self._tor_btn.setFixedWidth(60)
        self._tor_btn.clicked.connect(self._on_tor_toggled)
        self._tor_status = QLabel("⚪ Not running")
        self._tor_status.setObjectName("subtitle")
        tf.addRow("Use Tor:", self._tor_btn)
        tf.addRow("Status:", self._tor_status)
        tg.setLayout(tf)
        root.addWidget(tg)

        # Kill Switch
        kg = QGroupBox("Kill Switch")
        kf = QFormLayout()
        self._kill_btn = QPushButton("OFF")
        self._kill_btn.setCheckable(True)
        self._kill_btn.setFixedWidth(60)
        self._kill_btn.clicked.connect(self._on_kill_toggled)
        self._kill_status = QLabel("⚪ Inactive")
        self._kill_status.setObjectName("subtitle")
        self._kill_interval = QSpinBox()
        self._kill_interval.setRange(5, 300)
        self._kill_interval.setValue(15)

    def _on_proxy_toggled(self):
        enabled = self._proxy_enabled.isChecked()
        self._proxy_enabled.setText("ON" if enabled else "OFF")
        type_map = {"SOCKS5": "socks5", "SOCKS4": "socks4", "HTTP": "http", "HTTPS": "https"}
        self.proxy_config_changed.emit({
            "enabled": enabled,
            "proxy_type": type_map.get(self._proxy_type.currentText(), "socks5"),
            "host": self._proxy_host.text(),
            "port": self._proxy_port.value(),
            "username": self._proxy_user.text(),
            "password": self._proxy_pass.text(),
        })

    def _on_tor_toggled(self):
        enabled = self._tor_btn.isChecked()
        self._tor_btn.setText("ON" if enabled else "OFF")
        self.tor_toggle.emit(enabled)

    def _on_kill_toggled(self):
        enabled = self._kill_btn.isChecked()
        self._kill_btn.setText("ON" if enabled else "OFF")
        self.kill_switch_toggle.emit(enabled)

    def update_ip_info(self, ip, country, isp):
        if ip:
            flag = self._country_flag(country)
            self._ip_label.setText(f"🟢 Masked IP: {ip}  {flag}")
            self._country_label.setText(f"{country}  •  {isp}")
        else:
            self._ip_label.setText("🔴 Could not determine IP")

    def update_tor_status(self, running, bootstrapped):
        if bootstrapped:
            self._tor_status.setText("✅ Connected (3 hops)")
        elif running:
            self._tor_status.setText("⏳ Starting...")
        else:
            self._tor_status.setText("⚪ Not running")

    def update_kill_status(self, active):
        self._kill_status.setText("✅ Active — monitoring" if active else "⚪ Inactive")

    @staticmethod
    def _country_flag(code):
        if not code or len(code) != 2:
            return ""
        return chr(0x1F1E6 + ord(code[0]) - ord("A")) + chr(0x1F1E6 + ord(code[1]) - ord("A"))

        self._kill_interval.setSuffix(" seconds")
        kf.addRow("Enable:", self._kill_btn)
        kf.addRow("Status:", self._kill_status)
        kf.addRow("Check Interval:", self._kill_interval)
        kg.setLayout(kf)
        root.addWidget(kg)

        # DNS
        dg = QGroupBox("DNS Leak Protection")
        df = QFormLayout()
        self._dns_label = QLabel("Remote DNS via SOCKS5 (when proxy active)")
        self._dns_label.setObjectName("subtitle")
        df.addRow("Method:", self._dns_label)
        dg.setLayout(df)
        root.addWidget(dg)
        root.addStretch()

        sl.addWidget(self._latency_label)
        br = QHBoxLayout()
        self._check_btn = QPushButton("🔄 Check IP Now")
        self._check_btn.setObjectName("primaryButton")
        self._check_btn.clicked.connect(self.ip_check_requested.emit)
        br.addWidget(self._check_btn)
        self._rotate_btn = QPushButton("🔄 New Tor Circuit")
        self._rotate_btn.setObjectName("iconButton")
        self._rotate_btn.clicked.connect(self.tor_rotate_requested.emit)
        br.addWidget(self._rotate_btn)
        br.addStretch()
        sl.addLayout(br)
        sg.setLayout(sl)
        root.addWidget(sg)

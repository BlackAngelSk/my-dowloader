"""Main application window — sidebar navigation + page stack + theming."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QMainWindow, QPushButton, QSizePolicy,
    QStackedWidget, QVBoxLayout, QWidget,
)

from omnidownloader.core.download_manager import DownloadManager
from omnidownloader.core.models import DownloadJob, DownloadState, DownloadModule
from omnidownloader.ui.themes import (
    DARK_COLORS, LIGHT_COLORS, generate_stylesheet, get_system_theme,
)
from omnidownloader.ui.pages.dashboard_page import DashboardPage
from omnidownloader.ui.pages.history_page import HistoryPage
from omnidownloader.ui.pages.settings_page import SettingsPage
from omnidownloader.ui.pages.anonymity_page import AnonymityPage
from omnidownloader.ui.pages.scheduler_page import SchedulerPage
from omnidownloader.ui.widgets.toast_notification import ToastNotification
from omnidownloader.ui.widgets.media_player import MediaPreviewWidget
from omnidownloader.ui.widgets.format_dialog import FormatSelectionDialog
from omnidownloader.ui.drag_drop_overlay import DragDropOverlay


class MainWindow(QMainWindow):

    def __init__(self, download_manager: DownloadManager) -> None:
        super().__init__()
        self._dm = download_manager
        self._theme = get_system_theme()
        self.setWindowTitle("OmniDownloader")
        self.setMinimumSize(960, 640)
        self.resize(1100, 720)
        self.setAcceptDrops(True)
        self._build_ui()
        self._connect_signals()
        self._apply_theme()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Sidebar
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(12, 16, 12, 16)
        sb_layout.setSpacing(4)

        logo = QPushButton("\u26a1 OmniDownloader")
        logo.setObjectName("title")
        logo.setStyleSheet("font-size: 16px; font-weight: bold; border: none; text-align: left;")
        logo.setEnabled(False)
        sb_layout.addWidget(logo)
        sb_layout.addSpacing(16)

        self._nav_buttons: list[QPushButton] = []
        btn_dashboard = QPushButton("\U0001f4e5  Dashboard")
        btn_history = QPushButton("\U0001f4cb  History")
        btn_anonymity = QPushButton("\U0001f6e1\ufe0f  Anonymity")
        btn_scheduler = QPushButton("\U0001f552  Scheduler")
        btn_settings = QPushButton("\u2699\ufe0f  Settings")
        theme_toggle = QPushButton("\U0001f319  Dark Mode" if self._theme == "dark" else "\u2600\ufe0f  Light Mode")
        theme_toggle.setObjectName("themeToggle")

        for btn in (btn_dashboard, btn_history, btn_anonymity, btn_scheduler, btn_settings):
            btn.setObjectName("navBtn")
            self._nav_buttons.append(btn)
            sb_layout.addWidget(btn)

        sb_layout.addStretch()
        sb_layout.addWidget(theme_toggle)
        self._theme_btn = theme_toggle

        main_layout.addWidget(sidebar)

        # Page Stack
        self._pages = QStackedWidget()
        self._dashboard = DashboardPage()
        self._history = HistoryPage()
        self._anonymity = AnonymityPage()
        self._scheduler_page = SchedulerPage()
        self._settings = SettingsPage()

        self._pages.addWidget(self._dashboard)
        self._pages.addWidget(self._history)
        self._pages.addWidget(self._anonymity)
        self._pages.addWidget(self._scheduler_page)
        self._pages.addWidget(self._settings)
        main_layout.addWidget(self._pages, 1)

        # Drag-drop overlay
        self._overlay = DragDropOverlay(self)

        # Toast notification
        self._toast = ToastNotification(self)

        # Media preview player
        self._media_player = MediaPreviewWidget(self)
        self._media_player.hide()

        # Nav button wiring
        btn_dashboard.clicked.connect(lambda: self._switch_page(0))
        btn_history.clicked.connect(lambda: self._switch_page(1))
        btn_anonymity.clicked.connect(lambda: self._switch_page(2))
        btn_scheduler.clicked.connect(lambda: self._switch_page(3))
        btn_settings.clicked.connect(lambda: self._switch_page(4))
        theme_toggle.clicked.connect(self._toggle_theme)

        self._switch_page(0)


    def _connect_signals(self) -> None:
        self._dm.job_added.connect(self._on_job_added)
        self._dm.job_removed.connect(self._on_job_removed)
        self._dm.job_state_changed.connect(self._on_job_state_changed)
        self._dm.job_progress.connect(self._on_job_progress)
        self._dm.global_speed_update.connect(self._dashboard.update_global_speed)
        self._dashboard.url_submitted.connect(self._on_url_submitted)
        self._dashboard.open_folder_requested.connect(self._on_open_folder)
        self._dashboard.pause_requested.connect(self._dm.pause_job)
        self._dashboard.resume_requested.connect(self._dm.resume_job)
        self._dashboard.cancel_requested.connect(self._dm.cancel_job)
        self._dashboard.preview_clicked.connect(self._on_preview_clicked)
        self._dashboard.priority_changed.connect(self._on_priority_changed)
        self._overlay.files_dropped.connect(self._on_files_dropped)
        self._overlay.urls_dropped.connect(self._on_urls_dropped)
        self._toast.download_clicked.connect(self._on_url_submitted)
        self._settings.settings_changed.connect(self._on_settings_changed)
        self._history.open_folder_clicked.connect(self._on_open_folder)
        self._history.remove_clicked.connect(self._on_remove_history)
        # Scheduler
        self._scheduler_page.rules_changed.connect(self._on_scheduler_rules_changed)
        # Media player
        self._media_player.closed.connect(self._on_media_player_closed)
        # Format selection
        self._dm.format_selection_needed.connect(self._on_format_selection_needed)

    def _switch_page(self, index: int) -> None:
        self._pages.setCurrentIndex(index)
        for i, btn in enumerate(self._nav_buttons):
            btn.setProperty("active", i == index)
            style = btn.style()
            if style:
                style.unpolish(btn)
                style.polish(btn)

    def _toggle_theme(self) -> None:
        self._theme = "light" if self._theme == "dark" else "dark"
        self._apply_theme()
        self._theme_btn.setText(
            "\U0001f319  Dark Mode" if self._theme == "dark" else "\u2600\ufe0f  Light Mode"
        )

    def _apply_theme(self) -> None:
        self.setStyleSheet(generate_stylesheet(self._theme))
        color = DARK_COLORS.accent if self._theme == "dark" else LIGHT_COLORS.accent
        self._dashboard._speed_graph.set_accent_color(color)

    def _on_url_submitted(self, url: str) -> None:
        self._dm.enqueue(url)

    def _on_files_dropped(self, files: list[str]) -> None:
        for f in files:
            self._dm.enqueue(f)

    def _on_urls_dropped(self, urls: list[str]) -> None:
        for url in urls:
            self._dm.enqueue(url)

    def _on_job_added(self, job_id: str) -> None:
        job = self._dm.get_job(job_id)
        if job:
            self._dashboard.add_job(job)

    def _on_job_removed(self, job_id: str) -> None:
        self._dashboard.remove_job(job_id)

    def _on_job_state_changed(self, job_id: str, state: str) -> None:
        job = self._dm.get_job(job_id)
        if job:
            self._dashboard.update_job(job)
            if state in ("completed", "failed", "cancelled"):
                self._history.add_job(job)

    def _on_job_progress(self, job_id: str, pct: float, speed: float) -> None:
        job = self._dm.get_job(job_id)
        if job:
            self._dashboard.update_job(job)

    def _on_settings_changed(self, settings: dict) -> None:
        if "download_dir" in settings and settings["download_dir"]:
            self._dm.download_dir = settings["download_dir"]
        if "max_concurrent" in settings:
            self._dm._max_concurrent = settings["max_concurrent"]
        if "speed_limit_kbs" in settings:
            try:
                val = float(settings["speed_limit_kbs"] or "0") * 1024
                self._dm.bandwidth_manager.set_global_rate(val)
            except ValueError:
                pass

    def _on_open_folder(self, job_id: str) -> None:
        job = self._dm.get_job(job_id)
        if job and job.file_path:
            path = Path(job.file_path).parent
            if sys.platform == "linux":
                os.system(f'xdg-open "{path}"')
            elif sys.platform == "darwin":
                os.system(f'open "{path}"')
            else:
                os.system(f'explorer "{path}"')

    def _on_remove_history(self, job_id: str) -> None:
        self._history.remove_job(job_id)
        self._dm.remove_job(job_id)

    # Drag & drop
    def dragEnterEvent(self, a0) -> None:
        self._overlay.dragEnterEvent(a0)

    def dragLeaveEvent(self, a0) -> None:
        self._overlay.deactivate()

    def dropEvent(self, a0) -> None:
        self._overlay.dropEvent(a0)

    def show_toast(self, url: str) -> None:
        self._toast.show_for_url(url)

    def _on_scheduler_rules_changed(self, rules: list) -> None:
        """Apply scheduler rules to the DownloadManager's scheduler."""
        from omnidownloader.core.scheduler import SchedulerRule
        if self._dm._scheduler:
            self._dm._scheduler.clear_rules()
            for r in rules:
                self._dm._scheduler.add_rule(SchedulerRule.from_dict(r))

    def _on_media_player_closed(self) -> None:
        self._media_player.hide()

    def _on_preview_clicked(self, job_id: str) -> None:
        job = self._dm.get_job(job_id)
        if job:
            self._media_player.attach_job(job)
            self._media_player.show()
            self._media_player.raise_()

    def _on_priority_changed(self, job_id: str, priority_val: str) -> None:
        from omnidownloader.core.models import Priority
        try:
            p = Priority(priority_val)
            self._dm.set_job_priority(job_id, p)
        except ValueError:
            pass

    def _on_format_selection_needed(self, job_id: str, metadata: dict) -> None:
        """Show the format selection dialog for a media download."""
        dialog = FormatSelectionDialog(metadata, parent=self)
        result = dialog.exec()
        if result == FormatSelectionDialog.DialogCode.Accepted and dialog.result_data:
            self._dm.resolve_format(job_id, dialog.result_data)
        else:
            # User cancelled — resolve with defaults so download proceeds
            self._dm.resolve_format(job_id, {
                "format": "bv*+ba/b",
                "quality_label": "Best",
                "audio_only": False,
            })


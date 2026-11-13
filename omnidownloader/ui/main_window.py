"""Main application window — navigation rail, page stack and theming."""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton,
    QStackedWidget, QVBoxLayout, QWidget,
)

from omnidownloader.core import platform_utils
from omnidownloader.core.download_manager import DownloadManager
from omnidownloader.ui import icons, themes
from omnidownloader.ui.pages.anonymity_page import AnonymityPage
from omnidownloader.ui.pages.dashboard_page import DashboardPage
from omnidownloader.ui.pages.history_page import HistoryPage
from omnidownloader.ui.pages.scheduler_page import SchedulerPage
from omnidownloader.ui.pages.settings_page import SettingsPage
from omnidownloader.ui.widgets.format_dialog import FormatSelectionDialog
from omnidownloader.ui.widgets.media_player import MediaPreviewWidget
from omnidownloader.ui.widgets.toast_notification import ToastNotification
from omnidownloader.ui.drag_drop_overlay import DragDropOverlay

logger = logging.getLogger(__name__)

#: (icon, label) per page, in stack order.
NAV_ITEMS = (
    ("download", "Downloads"),
    ("history", "History"),
    ("shield", "Anonymity"),
    ("clock", "Scheduler"),
    ("sliders", "Settings"),
)


class MainWindow(QMainWindow):

    def __init__(self, download_manager: DownloadManager) -> None:
        super().__init__()
        self._dm = download_manager
        self._theme = themes.get_system_theme()
        themes.set_current_mode(self._theme)
        self.setWindowTitle("OmniDownloader")
        self.setMinimumSize(1000, 660)
        self.resize(1180, 760)
        self.setAcceptDrops(True)
        self._build_ui()
        self._connect_signals()
        self._apply_theme()

    # ── construction ────────────────────────────────────────────
    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("appShell")
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        main_layout.addWidget(self._build_sidebar())

        self._pages = QStackedWidget()
        self._dashboard = DashboardPage()
        self._history = HistoryPage()
        self._anonymity = AnonymityPage()
        self._scheduler_page = SchedulerPage()
        self._settings = SettingsPage()
        for page in (self._dashboard, self._history, self._anonymity,
                     self._scheduler_page, self._settings):
            self._pages.addWidget(page)
        main_layout.addWidget(self._pages, 1)

        self._overlay = DragDropOverlay(self)
        self._toast = ToastNotification(self)
        self._media_player = MediaPreviewWidget(self)
        self._media_player.hide()

        self._switch_page(0)

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(236)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(4)

        # ── brand ──────────────────────────────────────────────
        brand = QWidget()
        brand_layout = QHBoxLayout(brand)
        brand_layout.setContentsMargins(4, 0, 4, 0)
        brand_layout.setSpacing(10)
        mark = QLabel()
        mark.setPixmap(icons.pixmap("logo", themes.current_palette().accent, 34))
        mark.setFixedSize(34, 34)
        brand_layout.addWidget(mark)
        names = QVBoxLayout()
        names.setSpacing(0)
        name = QLabel("OmniDownloader")
        name.setObjectName("logoText")
        sub = QLabel("download manager")
        sub.setObjectName("logoSub")
        names.addWidget(name)
        names.addWidget(sub)
        brand_layout.addLayout(names)
        brand_layout.addStretch()
        self._brand_mark = mark
        layout.addWidget(brand)
        layout.addSpacing(18)

        section = QLabel("WORKSPACE")
        section.setObjectName("navSection")
        layout.addWidget(section)
        layout.addSpacing(2)

        # ── navigation ─────────────────────────────────────────
        self._nav_buttons: list[QPushButton] = []
        for index, (_glyph, label) in enumerate(NAV_ITEMS):
            btn = QPushButton(f"  {label}")
            btn.setObjectName("navBtn")
            btn.setCheckable(False)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setMinimumHeight(38)
            btn.setIconSize(QSize(18, 18))
            btn.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            btn.clicked.connect(lambda _checked=False, i=index: self._switch_page(i))
            self._nav_buttons.append(btn)
            layout.addWidget(btn)

        layout.addStretch()

        # ── footer: theme + version ────────────────────────────
        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFixedHeight(1)
        layout.addWidget(divider)
        layout.addSpacing(6)

        self._theme_btn = QPushButton("  Light mode" if self._theme == "dark"
                                      else "  Dark mode")
        self._theme_btn.setObjectName("themeToggle")
        self._theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._theme_btn.setMinimumHeight(36)
        self._theme_btn.setIconSize(QSize(16, 16))
        self._theme_btn.clicked.connect(self._toggle_theme)
        layout.addWidget(self._theme_btn)

        try:
            from omnidownloader import __version__
            version = __version__
        except Exception:  # noqa: BLE001
            version = "0.1.0"
        self._version_label = QLabel(f"Version {version}")
        self._version_label.setObjectName("sidebarFooter")
        self._version_label.setContentsMargins(10, 4, 0, 0)
        layout.addWidget(self._version_label)
        return sidebar

    def _connect_signals(self) -> None:
        self._dm.job_added.connect(self._on_job_added)
        self._dm.job_removed.connect(self._on_job_removed)
        self._dm.job_state_changed.connect(self._on_job_state_changed)
        self._dm.job_progress.connect(self._on_job_progress)
        self._dm.global_speed_update.connect(self._dashboard.update_global_speed)
        self._dashboard.url_submitted.connect(self._on_url_submitted)
        self._dashboard.url_submitted_with_checksum.connect(
            self._on_url_submitted_with_checksum)
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
        # Scheduler rules are applied by main.py's _apply_scheduler_rules
        # against the scheduler instance; connecting them here as well applied
        # every rule edit twice.
        self._media_player.closed.connect(self._on_media_player_closed)
        self._dm.format_selection_needed.connect(self._on_format_selection_needed)

    # ── navigation / theme ──────────────────────────────────────
    def _switch_page(self, index: int) -> None:
        self._pages.setCurrentIndex(index)
        palette = themes.current_palette()
        for i, btn in enumerate(self._nav_buttons):
            active = i == index
            btn.setProperty("active", active)
            glyph = NAV_ITEMS[i][0]
            btn.setIcon(icons.icon(glyph, palette.accent if active else palette.text_2, 18))
            style = btn.style()
            if style is not None:
                style.unpolish(btn)
                style.polish(btn)

    def _toggle_theme(self) -> None:
        self._set_theme("light" if self._theme == "dark" else "dark")

    def _set_theme(self, theme: str) -> None:
        """Switch to *theme* and reflect it in the header button."""
        if theme not in ("dark", "light"):
            return
        self._theme = theme
        self._apply_theme()

    def _apply_theme(self) -> None:
        themes.set_current_mode(self._theme)
        palette = themes.current_palette()
        app = QApplication.instance()
        if app is not None:
            app.setPalette(themes.qt_palette(self._theme))
            app.setStyleSheet(themes.generate_stylesheet(self._theme))
        self.setStyleSheet(themes.generate_stylesheet(self._theme))

        self._dashboard._speed_graph.set_accent_color(palette.accent)
        self._dashboard.restyle()
        self._history.restyle()
        self._toast.apply_theme()
        self._brand_mark.setPixmap(icons.pixmap("logo", palette.accent, 34))
        # The toggle shows what clicking it will do, not what is active.
        if self._theme == "dark":
            self._theme_btn.setText("  Light mode")
            self._theme_btn.setIcon(icons.icon("sun", palette.text_2, 16))
        else:
            self._theme_btn.setText("  Dark mode")
            self._theme_btn.setIcon(icons.icon("moon", palette.text_2, 16))
        # Page-2 icons are drawn per state; re-run the active page selection.
        self._switch_page(self._pages.currentIndex())

    # ── slots ───────────────────────────────────────────────────
    def _on_url_submitted(self, url: str) -> None:
        self._dm.enqueue(url)

    def _on_url_submitted_with_checksum(self, url: str, checksum: str) -> None:
        """Enqueue a URL that carries an expected digest."""
        job = self._dm.enqueue(url)
        job.metadata["expected_checksum"] = checksum
        logger.info("Queued %s with %s verification", url, checksum.split(":")[0])

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
            try:
                self._dm.set_max_concurrent(int(settings["max_concurrent"]))
            except (TypeError, ValueError):
                pass
        if "speed_limit_kbs" in settings:
            try:
                val = float(settings["speed_limit_kbs"] or "0") * 1024
                self._dm.bandwidth_manager.set_global_rate(val)
            except ValueError:
                pass
        # Per-task cap was collected by the settings panel but never applied.
        if "per_task_limit_kbs" in settings:
            try:
                val = float(settings["per_task_limit_kbs"] or "0") * 1024
                self._dm.set_default_task_rate(val)
            except (TypeError, ValueError):
                pass
        # Theme was collected and dropped: selecting Dark/Light did nothing.
        theme = settings.get("theme")
        if theme in ("dark", "light") and theme != self._theme:
            self._theme = theme
            self._apply_theme()

    def _on_open_folder(self, job_id: str) -> None:
        job = self._dm.get_job(job_id)
        if not (job and job.file_path):
            return
        path = Path(job.file_path)
        # Open the containing folder for files, the folder itself for dirs.
        target = path if path.is_dir() else path.parent
        # Platform_utils open_path: no shell (a directory name containing
        # quotes or $(...) used to break out of the xdg-open command line),
        # correct launcher per OS, and no console flash on Windows.
        platform_utils.open_path(target)

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

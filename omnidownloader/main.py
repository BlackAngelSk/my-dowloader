#!/usr/bin/env python3
"""OmniDownloader — Ultra-fast modular download manager."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from pathlib import Path

from omnidownloader.core import platform_utils

LOG_DIR = platform_utils.logs_dir()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "omnidownloader.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("omnidownloader")

# Windows: asyncio subprocesses (yt-dlp, ffmpeg, aria2c, tor) only work on the
# Proactor loop.  If anything else installs the selector policy, every spawn
# raises NotImplementedError, so pin it before the engine loop is created.
if platform_utils.is_windows():  # pragma: no cover - Windows only
    _proactor = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if _proactor is not None:
        asyncio.set_event_loop_policy(_proactor())

_loop: asyncio.AbstractEventLoop | None = None
#: Coroutines scheduled before the engine loop was live (see schedule_async).
_pending_coros: list = []


def schedule_async(coro):
    """Schedule a coroutine on the background asyncio loop (thread-safe).

    Anything requested before the engine loop starts is buffered and replayed
    by ``run_engine`` instead of being dropped — a dropped coroutine meant the
    auto-install of yt-dlp/ffmpeg and every anonymity action silently did
    nothing.
    """
    if _loop is not None and _loop.is_running():
        _loop.call_soon_threadsafe(asyncio.ensure_future, coro)
    else:
        logger.debug("Engine loop not running yet — deferring %r", coro)
        _pending_coros.append(coro)


def _installed_version() -> str:
    """Version from the installed package metadata, with a safe fallback."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version("omnidownloader")
    except Exception:  # noqa: BLE001 - source checkout without metadata
        return "0.0.0+source"


def _drain_pending_coros() -> None:
    """Run everything that was scheduled before the loop started."""
    if not _pending_coros:
        return
    logger.info("Running %d deferred coroutine(s)", len(_pending_coros))
    while _pending_coros:
        coro = _pending_coros.pop(0)
        asyncio.ensure_future(coro)


def main() -> None:
    # ── Headless diagnostics mode ────────────────────────────────
    # `python -m omnidownloader --diagnose` prints a measured report of what
    # works on this machine and exits, without starting the GUI.
    if "--diagnose" in sys.argv:
        from omnidownloader.diagnostics import main as diagnostics_main

        argv = [a for a in sys.argv[1:] if a != "--diagnose"]
        sys.exit(diagnostics_main(argv))

    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QTimer

    app = QApplication(sys.argv)
    app.setApplicationName("OmniDownloader")
    # Single source of truth: the installed package metadata.  A hard-coded
    # string here that drifts from pyproject.toml silently disables the
    # updater (it compares applicationVersion() against the release tag).
    app.setApplicationVersion(_installed_version())

    # Create asyncio event loop for background thread
    global _loop
    _loop = asyncio.new_event_loop()

    # ── Core engine ──────────────────────────────────────────
    from omnidownloader.core.download_manager import DownloadManager
    dm = DownloadManager(max_concurrent=4)


    # ── Proxy & Anonymity ────────────────────────────────────
    from omnidownloader.core.proxy_manager import ProxyManager

    proxy_mgr = ProxyManager()
    dm.set_proxy_manager(proxy_mgr)

    # ── Persisted settings ───────────────────────────────────
    # Nothing was ever saved before: every restart reset the download
    # directory, speed caps, proxy config, theme, scheduler rules and history.
    from omnidownloader.services.config_store import ConfigStore

    store = ConfigStore()
    saved = store.load()
    if saved.get("download_dir"):
        dm.download_dir = saved["download_dir"]
    if saved.get("max_concurrent"):
        dm.set_max_concurrent(int(saved["max_concurrent"]))
    if saved.get("per_task_limit_kbs"):
        try:
            dm.set_default_task_rate(float(saved["per_task_limit_kbs"]) * 1024)
        except (TypeError, ValueError):
            pass
    if saved.get("proxy"):
        proxy_mgr.from_dict(saved["proxy"])
    if saved.get("kill_switch_interval"):
        proxy_mgr.set_kill_switch_interval(saved["kill_switch_interval"])

    # ── Load modules via plugin system ───────────────────────
    from omnidownloader.services.plugin_loader import PluginLoader, user_plugin_dir

    # The plugin directory is created and passed in: it was never configured
    # anywhere, so load_external_plugins() always iterated an empty list.
    plugin_dir = user_plugin_dir()
    loader = PluginLoader(plugin_dirs=[str(plugin_dir)])
    modules = loader.load_all()
    for mod in modules:
        dm.register_module(mod)

    # Wire proxy_manager into modules that accept it
    from omnidownloader.modules.http_downloader import HTTPDownloader
    from omnidownloader.modules.media_extractor import MediaExtractor
    from omnidownloader.modules.image_scraper import ImageScraper
    from omnidownloader.modules.torrent_downloader import TorrentDownloader

    # ── Dependency check & auto-install (needed before wiring paths) ──
    from omnidownloader.services.dependency_manager import DependencyManager

    deps = DependencyManager()
    status = deps.check_all()
    for name, ok in status.items():
        if not ok:
            logger.info("%s not found locally — will auto-install in background.", name)

    # Schedule background auto-download once the async engine is running.
    # After download finishes, update the paths wired into MediaExtractor.
    async def _auto_install_deps():
        try:
            paths = await deps.ensure_all()
            if "yt-dlp" in paths:
                for mod in modules:
                    if isinstance(mod, MediaExtractor):
                        mod._ytdlp = paths["yt-dlp"]
            if "ffmpeg" in paths:
                for mod in modules:
                    if isinstance(mod, MediaExtractor):
                        mod._ffmpeg = paths["ffmpeg"]
            missing = [n for n, ok in deps.check_all().items() if not ok]
            if not missing:
                logger.info("All dependencies are ready.")
            else:
                logger.warning("Some dependencies could not be installed: %s", missing)
        except Exception:
            logger.exception("Background dependency auto-install failed")

    # 2-second delay so the UI renders first, then schedule on the
    # background asyncio loop (the engine thread starts shortly after).
    QTimer.singleShot(2000, lambda: schedule_async(_auto_install_deps()))

    for mod in modules:
        if isinstance(mod, (HTTPDownloader, MediaExtractor, ImageScraper)):
            mod._proxy_manager = proxy_mgr
        if isinstance(mod, (HTTPDownloader, TorrentDownloader)):
            mod._bw = dm.bandwidth_manager
        if isinstance(mod, MediaExtractor):
            mod._ytdlp = deps.ytdlp_path
            mod._ffmpeg = deps.ffmpeg_path

    # ── Tor Manager ──────────────────────────────────────────
    from omnidownloader.core.tor_manager import TorManager

    tor_mgr = TorManager()
    proxy_mgr._tor_manager = tor_mgr

    # ── UI ───────────────────────────────────────────────────
    from omnidownloader.ui.main_window import MainWindow

    window = MainWindow(dm)
    window.show()

    # ── Auto-Update Service ────────────────────────────────────
    from omnidownloader.services.update_service import UpdateService
    from omnidownloader.ui.widgets.update_dialog import UpdateDialog

    update_svc = UpdateService(
        current_version=app.applicationVersion(),
        parent=window,
    )

    def _show_update_dialog(info):
        dialog = UpdateDialog(info, update_svc, parent=window)
        dialog.download_requested.connect(update_svc.download_update)
        dialog.install_requested.connect(UpdateService.install_update)
        dialog.restart_requested.connect(UpdateService.restart_app)
        dialog.exec()

    update_svc.update_available.connect(_show_update_dialog)
    update_svc.up_to_date.connect(
        lambda: window.show_toast("OmniDownloader is up to date ✓")
    )

    # Wire "Check for Updates" button in Settings page
    settings_panel = window._settings._panel
    settings_panel._check_update_btn.clicked.connect(update_svc.check_for_updates)

    # ── yt-dlp freshness ────────────────────────────────────────
    async def _update_ytdlp(force: bool = False):
        """Refresh yt-dlp when a newer release exists (weekly, or on demand)."""
        import time as _time

        if not force:
            last = float(saved.get("ytdlp_last_check") or 0)
            if _time.time() - last < 7 * 86400:
                return
        updated, message = await deps.update_ytdlp(force=force)
        logger.info("yt-dlp update check: %s", message)
        store.note_ytdlp_check(deps.installed_ytdlp_version())
        if updated:
            for mod in modules:
                if isinstance(mod, MediaExtractor):
                    mod._ytdlp = deps.ytdlp_path
        try:
            settings_panel._ytdlp_label.setText(deps.installed_ytdlp_version())
            window.show_toast(message)
        except Exception:  # noqa: BLE001 - UI is best-effort here
            pass

    settings_panel._update_ytdlp_btn.clicked.connect(
        lambda: schedule_async(_update_ytdlp(force=True))
    )
    QTimer.singleShot(4000, lambda: schedule_async(_update_ytdlp()))

    # Non-blocking auto-check on startup (2s delay so UI renders first)
    from PyQt6.QtCore import QTimer
    QTimer.singleShot(2000, update_svc.check_for_updates)

    # Wire anonymity page signals
    from omnidownloader.core.proxy_manager import ProxyConfig, ProxyType
    anon = window._anonymity
    anon.proxy_config_changed.connect(
        lambda cfg: (proxy_mgr.configure(ProxyConfig(
            enabled=cfg["enabled"],
            proxy_type=ProxyType(cfg["proxy_type"]),
            host=cfg["host"], port=cfg["port"],
            username=cfg["username"], password=cfg["password"],
        )), store.save(proxy=proxy_mgr.to_dict()))
    )

    async def _handle_tor_toggle(enabled):
        if enabled:
            proxy_mgr.set_tor_enabled(True)
            ok = await tor_mgr.start()
            # Emit instead of touching the label: this runs on the engine
            # thread and Qt widgets are not thread-safe.
            anon.tor_status_ready.emit(tor_mgr.is_running, ok)
        else:
            await tor_mgr.stop()
            proxy_mgr.set_tor_enabled(False)
            anon.tor_status_ready.emit(False, False)

    anon.tor_toggle.connect(lambda e: schedule_async(_handle_tor_toggle(e)))

    # One IPChecker for the whole session so its cache can actually work
    # (a new instance per click always started with an empty cache).
    from omnidownloader.services.ip_checker import IPChecker
    ip_checker = IPChecker()

    async def _handle_ip_check():
        url = proxy_mgr.get_proxy_url() if proxy_mgr.enabled else ""
        result = await ip_checker.check(url)
        anon.ip_info_ready.emit(result.ip, result.country, result.isp)

    anon.ip_check_requested.connect(lambda: schedule_async(_handle_ip_check()))

    async def _handle_rotate():
        ok = await tor_mgr.rotate_identity()
        if ok:
            await _handle_ip_check()

    anon.tor_rotate_requested.connect(lambda: schedule_async(_handle_rotate()))

    anon.kill_switch_toggle.connect(proxy_mgr.set_kill_switch)
    anon.kill_switch_interval_changed.connect(proxy_mgr.set_kill_switch_interval)
    proxy_mgr.kill_switch_triggered.connect(dm.pause_all_active_jobs)
    # When the proxy recovers, resume what the kill switch paused — nothing
    # did before, so downloads stayed stalled until resumed by hand.
    proxy_mgr.kill_switch_cleared.connect(dm.resume_all_active_jobs)

    # ── Bandwidth Scheduler ─────────────────────────────────────
    from omnidownloader.core.scheduler import BandwidthScheduler

    scheduler = BandwidthScheduler(
        bandwidth_manager=dm.bandwidth_manager, parent=window,
    )
    dm.set_scheduler(scheduler)

    scheduler.rule_changed.connect(window._scheduler_page.update_active_rule)

    def _apply_scheduler_rules(sched, rules):
        from omnidownloader.core.scheduler import SchedulerRule
        sched.clear_rules()
        for r in rules:
            sched.add_rule(SchedulerRule.from_dict(r))

    def _on_rules_changed(rules_list):
        """Apply rule edits and remember them across restarts."""
        _apply_scheduler_rules(scheduler, rules_list)
        store.save(scheduler_rules=list(rules_list))

    window._scheduler_page.rules_changed.connect(_on_rules_changed)

    # Restore saved scheduler rules into the panel and the scheduler.
    saved_rules = saved.get("scheduler_rules") or []
    if saved_rules:
        window._scheduler_page.set_rules(saved_rules)
        _apply_scheduler_rules(scheduler, saved_rules)

    # Restore the saved theme preference.
    saved_theme = saved.get("theme")
    if saved_theme in ("dark", "light") and saved_theme != window._theme:
        window._set_theme(saved_theme)
    # Repopulate the settings form with what is actually in effect.
    settings_panel_owner = window._settings
    settings_panel_owner.set_defaults(store.settings())

    # Restore finished downloads into the history page.
    from omnidownloader.core.models import DownloadJob as _DownloadJob
    for record in reversed(store.load_history()):
        try:
            window._history.add_job(_DownloadJob.from_dict(record))
        except Exception:  # noqa: BLE001 - one bad record must not block startup
            logger.warning("Skipping unreadable history record")

    def _persist_settings(settings: dict) -> None:
        """Settings changed in the UI: apply and remember them."""
        window._on_settings_changed(settings)
        store.save(**{k: v for k, v in settings.items()})
        if settings.get("theme") in ("dark", "light"):
            window._set_theme(settings["theme"])

    window._settings.settings_changed.connect(_persist_settings)

    # ── Clipboard Monitor ───────────────────────────────────────
    from omnidownloader.services.clipboard_monitor import ClipboardMonitor
    clipboard = ClipboardMonitor(poll_interval_ms=1000, parent=window)
    clipboard.url_detected.connect(window.show_toast)

    # ── Start async engine in background thread ───────────────
    async def _run_all():
        await asyncio.gather(
            dm.run(),
            scheduler.start(),
        )

    def run_engine():
        if _loop is None:
            return
        # Keep a crash here from silently killing dispatch: the GUI would stay
        # up looking healthy while nothing was ever downloaded again.
        try:
            _drain_pending_coros()
            _loop.run_until_complete(_run_all())
        except BaseException:
            logger.exception("Fatal error in the download engine thread")
            logger.critical(
                "The download engine has stopped — restart OmniDownloader. "
                "New downloads will not be dispatched."
            )

    engine_thread = threading.Thread(target=run_engine, daemon=True)
    engine_thread.start()

    logger.info("OmniDownloader started successfully.")

    # ── Shutdown ──────────────────────────────────────────────
    # Close module resources (aiohttp sessions, torrent handles) so the app
    # doesn't leave sockets open or spray "Unclosed client session" warnings.
    def _on_quit() -> None:
        if _loop is not None and _loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(dm.shutdown(), _loop).result(timeout=5)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Shutdown cleanup incomplete: %s", exc)
        # Persist history + the settings actually in effect, so the next start
        # comes back the way the user left it.
        from omnidownloader.core.models import DownloadState
        try:
            finished = [
                j.to_dict() for j in dm.all_jobs()
                if j.state in (DownloadState.COMPLETED, DownloadState.FAILED,
                               DownloadState.CANCELLED)
            ]
            finished.sort(key=lambda d: d.get("completed_at") or 0, reverse=True)
            store.save_history(finished)
            store.save(**window._settings._panel.get_settings(),
                       proxy=proxy_mgr.to_dict(),
                       kill_switch_enabled=proxy_mgr.kill_switch_active)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not persist settings/history: %s", exc)

    app.aboutToQuit.connect(_on_quit)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""OmniDownloader — Ultra-fast modular download manager."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from pathlib import Path

LOG_DIR = Path.home() / ".omnidownloader" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "omnidownloader.log"),
    ],
)
logger = logging.getLogger("omnidownloader")

_loop: asyncio.AbstractEventLoop | None = None


def schedule_async(coro):
    """Schedule a coroutine on the background asyncio loop (thread-safe)."""
    if _loop is not None and _loop.is_running():
        _loop.call_soon_threadsafe(asyncio.ensure_future, coro)


def main() -> None:
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QTimer

    app = QApplication(sys.argv)
    app.setApplicationName("OmniDownloader")
    app.setApplicationVersion("0.1.0")

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

    # ── Load modules via plugin system ───────────────────────
    from omnidownloader.services.plugin_loader import PluginLoader

    loader = PluginLoader()
    modules = loader.load_all()
    for mod in modules:
        dm.register_module(mod)

    # Wire proxy_manager into modules that accept it
    from omnidownloader.modules.http_downloader import HTTPDownloader
    from omnidownloader.modules.media_extractor import MediaExtractor
    from omnidownloader.modules.image_scraper import ImageScraper
    from omnidownloader.modules.torrent_downloader import TorrentDownloader

    # ── Dependency check (needed before wiring paths) ──────────
    from omnidownloader.services.dependency_manager import DependencyManager

    deps = DependencyManager()
    status = deps.check_all()
    for name, ok in status.items():
        if not ok:
            logger.warning("%s not found — certain features may be unavailable.", name)

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

    # Wire anonymity page signals
    from omnidownloader.core.proxy_manager import ProxyConfig, ProxyType
    anon = window._anonymity
    anon.proxy_config_changed.connect(
        lambda cfg: proxy_mgr.configure(ProxyConfig(
            enabled=cfg["enabled"],
            proxy_type=ProxyType(cfg["proxy_type"]),
            host=cfg["host"], port=cfg["port"],
            username=cfg["username"], password=cfg["password"],
        ))
    )

    async def _handle_tor_toggle(enabled):
        if enabled:
            proxy_mgr.set_tor_enabled(True)
            ok = await tor_mgr.start()
            anon.update_tor_status(tor_mgr.is_running, ok)
        else:
            await tor_mgr.stop()
            proxy_mgr.set_tor_enabled(False)
            anon.update_tor_status(False, False)

    anon.tor_toggle.connect(lambda e: schedule_async(_handle_tor_toggle(e)))

    async def _handle_ip_check():
        from omnidownloader.services.ip_checker import IPChecker
        checker = IPChecker()
        url = proxy_mgr.get_proxy_url() if proxy_mgr.enabled else ""
        result = await checker.check(url)
        anon.update_ip_info(result.ip, result.country, result.isp)

    anon.ip_check_requested.connect(lambda: schedule_async(_handle_ip_check()))

    async def _handle_rotate():
        ok = await tor_mgr.rotate_identity()
        if ok:
            await _handle_ip_check()

    anon.tor_rotate_requested.connect(lambda: schedule_async(_handle_rotate()))

    anon.kill_switch_toggle.connect(proxy_mgr.set_kill_switch)
    proxy_mgr.kill_switch_triggered.connect(dm.pause_all_active_jobs)

    # ── Bandwidth Scheduler ─────────────────────────────────────
    from omnidownloader.core.scheduler import BandwidthScheduler

    scheduler = BandwidthScheduler(
        bandwidth_manager=dm.bandwidth_manager, parent=window,
    )
    dm.set_scheduler(scheduler)

    window._scheduler_page.rules_changed.connect(
        lambda rules: _apply_scheduler_rules(scheduler, rules)
    )
    scheduler.rule_changed.connect(window._scheduler_page.update_active_rule)

    def _apply_scheduler_rules(sched, rules):
        from omnidownloader.core.scheduler import SchedulerRule
        sched.clear_rules()
        for r in rules:
            sched.add_rule(SchedulerRule.from_dict(r))

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
        if _loop is not None:
            _loop.run_until_complete(_run_all())

    engine_thread = threading.Thread(target=run_engine, daemon=True)
    engine_thread.start()

    logger.info("OmniDownloader started successfully.")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

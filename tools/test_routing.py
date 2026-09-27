#!/usr/bin/env python3
"""Routing tests: which module claims which URL, and the HTML hand-off.

Regression guard for the bug where MediaExtractor.can_handle() accepted every
http(s) URL and was registered first, so HTTPDownloader (the multi-connection
segmented engine) could never be selected for any direct download.

Usage: .venv/bin/python tools/test_routing.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web  # noqa: E402
from PyQt6.QtCore import QCoreApplication  # noqa: E402

from omnidownloader.core.download_manager import DownloadManager  # noqa: E402
from omnidownloader.core.models import DownloadJob, DownloadModule  # noqa: E402
from omnidownloader.modules.http_downloader import HTTPDownloader  # noqa: E402
from omnidownloader.modules.image_scraper import ImageScraper  # noqa: E402
from omnidownloader.modules.media_extractor import MediaExtractor  # noqa: E402
from omnidownloader.services.plugin_loader import PluginLoader  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def build_manager() -> DownloadManager:
    """A manager wired exactly like main.py does it."""
    dm = DownloadManager(max_concurrent=4)
    for mod in PluginLoader().load_all():
        dm.register_module(mod)
    return dm


def test_can_handle() -> None:
    media, http, image = MediaExtractor(), HTTPDownloader(), ImageScraper()

    cases = [
        # (url, expected module attr, expected True/False)
        ("https://www.youtube.com/watch?v=abc", media, True),
        ("https://youtu.be/abc", media, True),
        ("https://music.youtube.com/watch?v=abc", media, True),
        ("https://vimeo.com/12345", media, True),
        ("https://www.twitch.tv/videos/1", media, True),
        ("rtmp://live.example.com/app/stream", media, True),
        ("https://notyoutube.com/watch?v=abc", media, False),
        ("https://example.com/big.iso", media, False),
        ("https://cdn.example.com/video.mp4", media, False),
        ("scrape:https://example.com", media, False),
        # HTTP must claim the plain-file cases it was written for
        ("https://example.com/big.iso", http, True),
        ("https://cdn.example.com/video.mp4", http, True),
        ("https://example.com/file.zip?token=abc", http, True),
        ("https://www.youtube.com/watch?v=abc", http, False),
        ("https://vimeo.com/12345", http, False),
    ]
    for url, mod, expected in cases:
        got = mod.can_handle(url)
        label = type(mod).__name__
        check(f"{label}.can_handle({url[:46]}) == {expected}", got is expected,
              f"got {got}")


def test_module_selection() -> None:
    dm = build_manager()
    expected = [
        ("https://www.youtube.com/watch?v=abc", MediaExtractor),
        ("https://youtu.be/abc", MediaExtractor),
        ("https://example.com/archive.tar.gz", HTTPDownloader),
        ("https://mirror.example.org/debian.iso", HTTPDownloader),
    ]
    for url, cls in expected:
        mod = dm.find_module_for_url(url)
        check(f"routes to {cls.__name__}: {url[:44]}",
              isinstance(mod, cls), type(mod).__name__ if mod else "None")

    for url in ("https://example.com/cat.jpg", "https://example.com/logo.png"):
        mod = dm.find_module_for_url(url)
        check(f"image URL not stolen by media/http: {url[-12:]}",
              not isinstance(mod, (MediaExtractor, HTTPDownloader)),
              type(mod).__name__ if mod else "None")


def test_host_matching() -> None:
    check("subdomain matching does not match a suffix impostor",
          not MediaExtractor._host_matches("notyoutube.com", {"youtube.com"}))
    check("subdomain matching accepts real subdomains",
          MediaExtractor._host_matches("m.youtube.com", {"youtube.com"}))


def test_webpage_detection() -> None:
    dm = build_manager()
    check("HTML content-type is detected as a web page",
          dm._looks_like_webpage({"content_type": "text/html; charset=utf-8"}))
    check("a binary content-type is not a web page",
          not dm._looks_like_webpage({"content_type": "application/zip"}))
    check("missing content-type is not a web page",
          not dm._looks_like_webpage({}))


async def start_html_server() -> tuple[web.AppRunner, str]:
    async def handler(request):
        return web.Response(text="<html><body>watch here</body></html>",
                            content_type="text/html")

    async def binary(request):
        return web.Response(body=b"\x00" * 4096,
                            content_type="application/octet-stream",
                            headers={"Content-Length": "4096"})

    app = web.Application()
    app.router.add_get("/page", handler)
    app.router.add_get("/file.bin", binary)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


async def _true_probe(*args, **kwargs) -> bool:
    """Stand-in for MediaExtractor.probe_url that always says 'media'."""
    return True


async def test_html_handoff() -> None:
    """HTML pages go to yt-dlp; real files stay with the HTTP engine.

    yt-dlp itself refuses localhost URLs (SSRF guard), so the probe result is
    stubbed here — the point under test is the hand-off decision, not yt-dlp.
    """
    runner, base = await start_html_server()
    real_probe = MediaExtractor.probe_url
    try:
        # (a) a page yt-dlp can extract → media module
        MediaExtractor.probe_url = _true_probe
        dm = build_manager()
        job = DownloadJob(url=f"{base}/page")
        mod = await dm._resolve_module(job)
        check("HTML page is handed to yt-dlp when it can extract it",
              isinstance(mod, MediaExtractor) and job.module == DownloadModule.MEDIA,
              f"module={type(mod).__name__}, job.module={job.module.value}")
        await dm.shutdown()

        # (b) a page yt-dlp cannot extract → HTTP is the right fallback
        MediaExtractor.probe_url = _false_probe
        dm2 = build_manager()
        job2 = DownloadJob(url=f"{base}/page")
        mod2 = await dm2._resolve_module(job2)
        check("HTML page falls back to HTTP when yt-dlp cannot extract it",
              isinstance(mod2, HTTPDownloader),
              type(mod2).__name__ if mod2 else "None")
        await dm2.shutdown()

        # (c) a real binary URL must not be probed away from the HTTP engine
        MediaExtractor.probe_url = _true_probe
        dm3 = build_manager()
        job3 = DownloadJob(url=f"{base}/file.bin")
        mod3 = await dm3._resolve_module(job3)
        check("binary file is not stolen from the HTTP engine",
              isinstance(mod3, HTTPDownloader),
              type(mod3).__name__ if mod3 else "None")
        await dm3.shutdown()
    finally:
        MediaExtractor.probe_url = real_probe
        await runner.cleanup()


async def _false_probe(*args, **kwargs) -> bool:
    """Stand-in for MediaExtractor.probe_url that always says 'not media'."""
    return False


async def test_unclaimed_url_fails_cleanly() -> None:
    """A URL nobody can handle must end FAILED with a message, not hang."""
    dm = build_manager()
    real_probe = MediaExtractor.probe_url
    try:
        MediaExtractor.probe_url = _false_probe
        dm2 = build_manager()
        # A scheme no module implements at all
        job = DownloadJob(url="gopher://example.com/1")
        mod = await dm2._resolve_module(job)
        check("unhandleable URL is rejected with an error message",
              mod is None and bool(job.error_message),
              job.error_message or "no message")
    finally:
        MediaExtractor.probe_url = real_probe


async def main() -> int:
    app = QCoreApplication(sys.argv)      # Qt signals need an app instance
    assert app is not None
    tmp = Path(tempfile.mkdtemp(prefix="routingtest-"))
    try:
        test_can_handle()
        test_module_selection()
        test_host_matching()
        test_webpage_detection()
        await test_html_handoff()
        await test_unclaimed_url_fails_cleanly()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failures = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
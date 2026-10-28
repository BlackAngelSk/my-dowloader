#!/usr/bin/env python3
"""URL normalisation tests: the "no module can handle this URL" complaint.

Users paste whatever the browser address bar shows — ``youtube.com/watch?v=…``
with no scheme — and on Windows the clipboard is full of such links. Every one
of them used to reach the router scheme-less, match no module and fail with a
message that blamed yt-dlp.

Usage: .venv/bin/python tools/test_url_normalize.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import QCoreApplication  # noqa: E402

from omnidownloader.core.download_manager import DownloadManager  # noqa: E402
from omnidownloader.core.models import DownloadModule  # noqa: E402
from omnidownloader.core.url_utils import (  # noqa: E402
    KNOWN_SCHEMES, normalize_url, url_scheme,
)
from omnidownloader.services.clipboard_monitor import ClipboardMonitor  # noqa: E402
from omnidownloader.ui.widgets.url_input_bar import URLInputBar  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def eq(name: str, got, expected) -> None:
    check(name, got == expected, f"got {got!r}, want {expected!r}")


def test_normalize() -> None:
    eq("bare host gets https", normalize_url("youtube.com/watch?v=abc"),
       "https://youtube.com/watch?v=abc")
    eq("bare host with port keeps port",
       normalize_url("example.com:8080/file.iso"),
       "https://example.com:8080/file.iso")
    eq("protocol-relative link", normalize_url("//cdn.example.com/a.bin"),
       "https://cdn.example.com/a.bin")
    eq("already-normalised URL is untouched",
       normalize_url("https://example.com/a.zip"), "https://example.com/a.zip")
    eq("magnet link is untouched",
       normalize_url("magnet:?xt=urn:btih:abc&dn=Ubuntu"),
       "magnet:?xt=urn:btih:abc&dn=Ubuntu")
    eq("scrape: prefix is untouched", normalize_url("scrape:https://example.com"),
       "scrape:https://example.com")
    eq("unsupported scheme is preserved for a clear error",
       normalize_url("gopher://example.com/1"), "gopher://example.com/1")
    eq("zero-width characters are stripped",
       normalize_url("\u200bhttps://example.com/a.zip\ufeff"),
       "https://example.com/a.zip")
    eq("non-breaking space is stripped",
       normalize_url("\u00a0https://example.com/a.zip"),
       "https://example.com/a.zip")
    eq("quotes and angle brackets are stripped",
       normalize_url('"https://example.com/a.zip"'), "https://example.com/a.zip")
    eq("prose punctuation is dropped",
       normalize_url("https://example.com/a.zip."), "https://example.com/a.zip")
    eq("empty input stays empty", normalize_url("   "), "")
    eq("windows path with drive letter is untouched",
       normalize_url(r"C:\Users\me\Downloads\a.torrent"),
       r"C:\Users\me\Downloads\a.torrent")
    eq("plain text is left alone", normalize_url("not a url at all"),
       "not a url at all")


def test_scheme_helpers() -> None:
    eq("url_scheme parses https", url_scheme("https://Example.com"), "https")
    eq("url_scheme parses magnet", url_scheme("magnet:?xt=urn:btih:x"), "magnet")
    eq("url_scheme returns empty without a scheme", url_scheme("example.com/x"), "")
    check("http and https are known download schemes",
          {"http", "https", "magnet", "torrent"} <= KNOWN_SCHEMES)


def test_routing_after_normalisation() -> None:
    from omnidownloader.services.plugin_loader import PluginLoader

    dm = DownloadManager(max_concurrent=1)
    for mod in PluginLoader().load_all():      # same wiring as main.py
        dm.register_module(mod)
    job = dm.enqueue("youtube.com/watch?v=abc")
    eq("enqueue repairs the scheme", job.url, "https://youtube.com/watch?v=abc")
    eq("repaired media URL routes to MEDIA", job.module, DownloadModule.MEDIA)

    job2 = dm.enqueue("cdn.example.com/big.iso")
    eq("repaired direct URL routes to HTTP", job2.module, DownloadModule.HTTP)


def test_input_bar() -> None:
    url, checksum = URLInputBar.parse_input("youtube.com/watch?v=abc")
    eq("input bar adds the scheme", url, "https://youtube.com/watch?v=abc")
    eq("no checksum parsed", checksum, "")

    url2, sum2 = URLInputBar.parse_input(
        "example.com/a.iso sha256:" + "a" * 64)
    eq("input bar keeps the checksum", sum2, "sha256:" + "a" * 64)
    eq("input bar normalises the URL part", url2, "https://example.com/a.iso")


def test_clipboard_detection() -> None:
    eq("bare youtube host is detected",
       ClipboardMonitor.first_downloadable("look: youtube.com/watch?v=abc"),
       "https://youtube.com/watch?v=abc")
    eq("scheme link is detected",
       ClipboardMonitor.first_downloadable("https://youtu.be/abc"),
       "https://youtu.be/abc")
    eq("magnet is detected",
       ClipboardMonitor.first_downloadable("magnet:?xt=urn:btih:abc"),
       "magnet:?xt=urn:btih:abc")
    eq("image link is detected",
       ClipboardMonitor.first_downloadable("https://example.com/cat.jpg"),
       "https://example.com/cat.jpg")
    eq("impostor domain is not detected",
       ClipboardMonitor.first_downloadable("notyoutube.com/watch?v=abc"), "")
    eq("plain text is not detected",
       ClipboardMonitor.first_downloadable("hello world"), "")


def test_error_messages() -> None:
    dm = DownloadManager(max_concurrent=1)
    msg = dm.unhandleable_reason("gopher://example.com/1")
    check("unsupported scheme names the scheme", "gopher:" in msg, msg)
    msg2 = dm.unhandleable_reason("not a url at all")
    check("non-URL explains the missing scheme", "does not look like a link" in msg2, msg2)
    msg3 = dm.unhandleable_reason("https://example.com/page",
                                  media=_FakeMedia())
    check("missing yt-dlp is reported as missing, not unsupported",
          "not available" in msg3, msg3)
    msg4 = dm.unhandleable_reason("https://example.com/page", media=_FakeMedia(ok=True))
    check("a real yt-dlp refusal still says yt-dlp does not support it",
          "does not support it" in msg4, msg4)
    msg5 = dm.unhandleable_reason("https://example.com/cat.jpg")
    check("image link failure is explained", "image" in msg5, msg5)


class _FakeMedia:
    """Stand-in for MediaExtractor whose yt-dlp path does or does not exist."""

    def __init__(self, ok: bool = False) -> None:
        self._ytdlp = sys.executable if ok else "definitely-not-yt-dlp-xyz"


def main() -> int:
    app = QCoreApplication(sys.argv)      # Qt signals need an app instance
    assert app is not None
    test_normalize()
    test_scheme_helpers()
    test_routing_after_normalisation()
    test_input_bar()
    test_clipboard_detection()
    test_error_messages()

    failures = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

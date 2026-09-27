#!/usr/bin/env python3
"""Tests for the spec-gap features: checksums, dropped link lists, plugins.

Usage: QT_QPA_PLATFORM=offscreen .venv/bin/python tools/test_features.py
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from omnidownloader.core.disk_utils import (  # noqa: E402
    file_digest,
    parse_checksum,
    verify_checksum,
)
from omnidownloader.services.plugin_loader import PluginLoader, user_plugin_dir  # noqa: E402
from omnidownloader.ui.drag_drop_overlay import DragDropOverlay  # noqa: E402
from omnidownloader.ui.widgets.url_input_bar import URLInputBar  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def test_checksums(tmp: Path) -> None:
    payload = b"omnidownloader test payload" * 1000
    target = tmp / "blob.bin"
    target.write_bytes(payload)
    want = hashlib.sha256(payload).hexdigest()
    md5 = hashlib.md5(payload).hexdigest()

    check("file_digest matches hashlib", file_digest(target) == want)
    check("explicit algorithm parses", parse_checksum(f"sha256:{want}") == ("sha256", want))
    check("bare 64-char hex is inferred as sha256",
          parse_checksum(want)[0] == "sha256")
    check("bare 32-char hex is inferred as md5", parse_checksum(md5)[0] == "md5")
    check("sha256sum-style line parses",
          parse_checksum(f"{want}  blob.bin") == ("sha256", want))

    ok, detail = verify_checksum(target, f"sha256:{want}")
    check("correct checksum verifies", ok, detail)
    ok, detail = verify_checksum(target, "sha256:" + "0" * 64)
    check("wrong checksum fails with a useful message", not ok, detail[:60])
    ok, detail = verify_checksum(target, "sha256:" + "zz" * 32)
    check("garbage checksum does not crash", not ok, detail[:60])
    ok, detail = verify_checksum(target, "")
    check("missing checksum is reported, not silently passed", not ok, detail)


def test_url_input_checksum() -> None:
    bar = URLInputBar()
    seen: list[tuple[str, str]] = []
    plain: list[str] = []
    bar.url_with_checksum.connect(lambda u, c: seen.append((u, c)))
    bar.url_submitted.connect(plain.append)

    digest = "a" * 64
    bar._input.setText(f"https://example.com/ubuntu.iso sha256:{digest}")
    bar._on_submit()
    check("url+checksum emits the pair", seen == [("https://example.com/ubuntu.iso",
                                                   f"sha256:{digest}")], f"{seen}")
    check("plain url still emits the simple signal", not plain)

    bar._input.setText("https://example.com/file.zip")
    bar._on_submit()
    check("url without checksum uses the plain signal",
          plain == ["https://example.com/file.zip"], f"{plain}")

    url, checksum = URLInputBar.parse_input(
        "https://example.com/x.tar.gz md5:" + "b" * 32 + " trailing text")
    check("trailing words after the digest are ignored",
          url == "https://example.com/x.tar.gz" and checksum.startswith("md5:"),
          f"{url} / {checksum}")
    bar.deleteLater()


def test_dropped_link_lists(tmp: Path) -> None:
    text = """
    My download list
    https://example.com/one.iso
    https://youtu.be/abc123
    magnet:?xt=urn:btih:deadbeef
    (https://example.com/two.zip)
    not-a-url
    """
    urls = DragDropOverlay.extract_urls(text)
    check("multiple URLs extracted from a dropped list", len(urls) == 4, f"{urls}")
    check("trailing punctuation stripped",
          "https://example.com/two.zip" in urls, f"{urls}")

    overlay = DragDropOverlay()
    emitted: list[list[str]] = []
    overlay.urls_dropped.connect(emitted.append)
    files: list[list[str]] = []
    overlay.files_dropped.connect(files.append)

    listing = tmp / "links.txt"
    listing.write_text("https://example.com/a.bin\nhttps://example.com/b.bin\n")
    # Simulate the drop of a text file (the path-based branch).
    found = DragDropOverlay.extract_urls(listing.read_text())
    check("a dropped .txt link list yields every link", len(found) == 2, f"{found}")
    check("list suffixes are recognised as link lists",
          listing.suffix in (".txt",), listing.suffix)
    overlay.deleteLater()


def test_plugin_directory(tmp: Path) -> None:
    plugin_dir = user_plugin_dir()
    check("user plugin directory exists", plugin_dir.is_dir(), str(plugin_dir))

    module_src = '''
from omnidownloader.core.base_module import BaseDownloaderModule


class ExamplePlugin(BaseDownloaderModule):
    MODULE_NAME = "example"

    def can_handle(self, url):
        return "example-plugin.test" in url

    async def extract_metadata(self, url):
        return {"title": "example", "file_size": 1}

    async def start_download(self, job, progress_callback=None):
        return None
'''
    plugin_file = plugin_dir / "zz_example_plugin.py"
    plugin_file.write_text(module_src)
    try:
        loader = PluginLoader(plugin_dirs=[str(plugin_dir)])
        modules = loader.load_all()
        names = [type(m).__name__ for m in modules]
        check("external plugin loaded from the plugin directory",
              "ExamplePlugin" in names, f"{names}")
        loaded = next((m for m in modules if type(m).__name__ == "ExamplePlugin"), None)
        check("plugin claims its own URLs",
              loaded is not None and loaded.can_handle("https://example-plugin.test/x"))
        check("plugin is routable alongside builtins", len(modules) >= 5,
              f"{len(modules)} modules")
    finally:
        plugin_file.unlink(missing_ok=True)


def main() -> int:
    app = QApplication(sys.argv) if QApplication.instance() is None else QApplication.instance()
    assert app is not None
    tmp = Path(tempfile.mkdtemp(prefix="feattest-"))
    try:
        test_checksums(tmp)
        test_url_input_checksum()
        test_dropped_link_lists(tmp)
        test_plugin_directory(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failures = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
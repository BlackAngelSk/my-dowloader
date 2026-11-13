#!/usr/bin/env python3
"""UI design invariants: theme completeness, icons, cards, filters.

The redesign moved every colour into ``ui/themes.py`` and every glyph into
``ui/icons.py``.  These checks keep it that way — a hard-coded hex value or an
emoji creeping back in is exactly the kind of thing that looks fine on the
machine it was written on and broken on the next one.

Usage: QT_QPA_PLATFORM=offscreen .venv/bin/python tools/test_ui_theme.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from omnidownloader.core.models import DownloadJob, DownloadState  # noqa: E402
from omnidownloader.ui import icons, themes  # noqa: E402
from omnidownloader.ui.pages.dashboard_page import DashboardPage  # noqa: E402
from omnidownloader.ui.pages.history_page import HistoryPage  # noqa: E402
from omnidownloader.ui.widgets.download_card import DownloadCard  # noqa: E402
from omnidownloader.ui.widgets.toast_notification import ToastNotification  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
UI_DIR = Path(__file__).resolve().parent.parent / "omnidownloader" / "ui"


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def eq(name: str, got, expected) -> None:
    check(name, got == expected, f"got {got!r}, want {expected!r}")


# ── themes ──────────────────────────────────────────────────────
def test_stylesheet() -> None:
    for mode in ("dark", "light"):
        qss = themes.generate_stylesheet(mode)
        check(f"{mode}: stylesheet has no unsubstituted placeholders",
              "$" not in qss, qss[qss.find("$"):qss.find("$") + 40] if "$" in qss else "")
        for selector in ("#downloadCard", "#navBtn", "#urlInput", "#pill",
                         "QTabBar::tab", "QListWidget::item", "#statChip"):
            check(f"{mode}: stylesheet styles {selector}", selector in qss)
        palette = themes.get_palette(mode)
        check(f"{mode}: accent appears in the stylesheet", palette.accent in qss)
        check(f"{mode}: surfaces differ", palette.bg != palette.surface)

    dark, light = themes.DARK_COLORS, themes.LIGHT_COLORS
    check("dark and light are not the same palette", dark.bg != light.bg)
    check("dark text is lighter than its background",
          dark.text > dark.bg, f"{dark.text} vs {dark.bg}")
    check("light text is darker than its background",
          light.text < light.bg, f"{light.text} vs {light.bg}")

    themes.set_current_mode("light")
    eq("current_palette follows the mode", themes.current_palette().bg, light.bg)
    themes.set_current_mode("dark")
    eq("current_palette switches back", themes.current_palette().bg, dark.bg)

    pal = themes.qt_palette("dark")
    check("qt palette carries the theme text colour",
          pal.windowText().color().name().lower() == dark.text.lower(),
          pal.windowText().color().name())


def test_no_hardcoded_colours() -> None:
    """Widgets must ask the palette, not paint their own hex values."""
    offenders = []
    hex_re = re.compile(r"#[0-9A-Fa-f]{6}\b")
    for path in UI_DIR.rglob("*.py"):
        if path.name in ("themes.py", "icons.py"):
            continue
        text = path.read_text()
        for match in hex_re.finditer(text):
            line = text[:match.start()].count("\n") + 1
            offenders.append(f"{path.name}:{line} {match.group(0)}")
    check("no widget hard-codes a hex colour", not offenders, ", ".join(offenders[:5]))


def test_icons() -> None:
    for name in icons._PAINTERS:
        pm = icons.pixmap(name, "#FFFFFF", 24)
        check(f"icon {name} renders", not pm.isNull() and pm.width() > 0)
    # A painted icon must actually contain pixels, not just be a blank canvas.
    pm = icons.pixmap("download", "#FFFFFF", 24)
    image = pm.toImage()
    painted = sum(
        1 for x in range(image.width()) for y in range(image.height())
        if image.pixelColor(x, y).alpha() > 0
    )
    check("painted icons are not empty", painted > 20, f"{painted} px")
    check("unknown icon names fail loudly",
          _raises(lambda: icons.pixmap("nope", "#FFF", 16)))


def _raises(fn) -> bool:
    try:
        fn()
    except KeyError:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


# ── widgets ─────────────────────────────────────────────────────
def test_card() -> None:
    job = DownloadJob(url="https://example.com/ubuntu.iso", state=DownloadState.DOWNLOADING,
                      file_name="ubuntu.iso", file_size=1000, downloaded_bytes=250)
    card = DownloadCard(job)
    eq("card tile shows the file type", card._tile.text(), "ISO")
    eq("card percent text", card._percent.text(), "25%")
    eq("running job pill", card._badge.text(), "Downloading")
    eq("running job pill tone", card._badge.property("tone"), "active")
    check("card has a painted pause icon", not card._pause_btn.icon().isNull())

    job.state = DownloadState.PAUSED
    card.update_progress()
    eq("paused job pill", card._badge.text(), "Paused")
    eq("paused job tone", card._badge.property("tone"), "warn")

    job.state = DownloadState.COMPLETED
    card.update_progress()
    eq("completed job pill", card._badge.text(), "Done")
    eq("completed job tone", card._badge.property("tone"), "done")
    check("open-folder is enabled once complete", card._open_btn.isEnabled())

    job.state = DownloadState.FAILED
    job.error_message = "HTTP 404 — gone"
    card.update_progress()
    eq("failed job tone", card._badge.property("tone"), "error")
    check("the failure reason is shown in the subtitle",
          "404" in card._sub.text(), card._sub.text())

    magnet = DownloadJob(url="magnet:?xt=urn:btih:abc", state=DownloadState.PENDING)
    eq("magnet jobs get a TOR tile", DownloadCard(magnet)._tile.text(), "TOR")

    card.apply_theme()          # must not raise on either theme
    themes.set_current_mode("light")
    card.apply_theme()
    themes.set_current_mode("dark")
    check("card survives a theme switch", True)


def test_dashboard_filters() -> None:
    page = DashboardPage()
    jobs = [
        DownloadJob(url="https://e.com/a.iso", state=DownloadState.DOWNLOADING, file_name="a.iso"),
        DownloadJob(url="https://e.com/b.iso", state=DownloadState.PAUSED, file_name="b.iso"),
        DownloadJob(url="https://e.com/c.iso", state=DownloadState.COMPLETED, file_name="c.iso"),
    ]
    for job in jobs:
        page.add_job(job)
    check("every job gets a card", len(page._cards) == 3)
    check("the empty state hides when cards exist", not page._empty.isVisible()
          or page._empty.isHidden())

    page._set_filter("active")
    visible = [c for c in page._cards.values() if not c.isHidden()]
    eq("the Active filter keeps only running/queued cards", len(visible), 1)

    page._set_filter("paused")
    visible = [c for c in page._cards.values() if not c.isHidden()]
    eq("the Paused filter keeps only paused cards", len(visible), 1)

    page._set_filter("finished")
    visible = [c for c in page._cards.values() if not c.isHidden()]
    eq("the Finished filter keeps only finished cards", len(visible), 1)

    page._set_filter("all")
    visible = [c for c in page._cards.values() if not c.isHidden()]
    eq("All shows everything again", len(visible), 3)

    page.update_global_speed(12.5 * 1024 * 1024)
    check("the throughput chip shows the speed",
          "MB/s" in page._speed_chip._value.text(), page._speed_chip._value.text())
    check("the header counts downloading jobs", page._active_chip._value.text() == "1",
          page._active_chip._value.text())

    page.remove_job(jobs[0].id)
    check("removing a job drops its card", len(page._cards) == 2)
    page.restyle()
    check("dashboard restyles without raising", True)


def test_history_and_toast() -> None:
    page = HistoryPage()
    done = DownloadJob(url="https://e.com/x.zip", state=DownloadState.COMPLETED,
                       file_name="x.zip", file_size=2048)
    page.add_job(done)
    eq("history counts its cards", page._subtitle.text(), "1 transfer")
    check("history cards are themed", not page._cards[done.id]._tile.styleSheet() == "")
    page.remove_job(done.id)
    eq("empty history says so", page._subtitle.text(), "Finished and failed transfers")
    page.restyle()

    toast = ToastNotification()
    toast.show_for_url("https://example.com/big.iso")
    check("a URL toast offers Download", toast._download_btn.isEnabled())
    check("the toast paints without raising", not toast.grab().isNull())
    toast.show_for_url("OmniDownloader is up to date")
    check("a notice toast disables Download", not toast._download_btn.isEnabled())
    toast._auto_dismiss()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    assert app is not None
    test_stylesheet()
    test_no_hardcoded_colours()
    test_icons()
    test_card()
    test_dashboard_filters()
    test_history_and_toast()

    failures = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

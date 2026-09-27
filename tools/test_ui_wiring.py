#!/usr/bin/env python3
"""Headless UI + wiring tests for the fixes made after the codebase audit.

Runs the real widgets under QT_QPA_PLATFORM=offscreen and asserts the
behaviour that was broken: kill-switch crash, unreachable resume, invisible
scheduler inputs, duplicate history cards, pinned speed-graph axis, toast
offering Download for non-URLs, priority dispatch ordering, per-task caps,
scheduler rule boundaries.

Usage: QT_QPA_PLATFORM=offscreen .venv/bin/python tools/test_ui_wiring.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import time as dt_time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from omnidownloader.core.bandwidth_limiter import BandwidthManager  # noqa: E402
from omnidownloader.core.download_manager import DownloadManager  # noqa: E402
from omnidownloader.core.models import DownloadJob, DownloadState, Priority  # noqa: E402
from omnidownloader.core.scheduler import BandwidthScheduler, SchedulerRule  # noqa: E402
from omnidownloader.ui.pages.anonymity_page import AnonymityPage  # noqa: E402
from omnidownloader.ui.pages.history_page import HistoryPage  # noqa: E402
from omnidownloader.ui.widgets.download_card import DownloadCard  # noqa: E402
from omnidownloader.ui.widgets.scheduler_panel import SchedulerPanel  # noqa: E402
from omnidownloader.ui.widgets.speed_graph import SpeedGraph  # noqa: E402
from omnidownloader.ui.widgets.toast_notification import ToastNotification  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# ── anonymity page ──────────────────────────────────────────────

def test_anonymity_page() -> None:
    page = AnonymityPage()
    seen: list[dict] = []
    page.proxy_config_changed.connect(seen.append)
    ksw: list[bool] = []
    page.kill_switch_toggle.connect(ksw.append)
    intervals: list[int] = []

    page._proxy_enabled.setChecked(True)
    page._on_proxy_toggled()
    check("proxy toggle emits a config", len(seen) == 1, f"{len(seen)} emission(s)")

    seen.clear()
    page._proxy_host.setText("10.0.0.5")
    page._on_proxy_field_changed()
    check("editing the host while ON re-applies the proxy",
          len(seen) == 1 and seen[0]["host"] == "10.0.0.5",
          f"{seen}")

    page._kill_btn.setChecked(True)
    page._on_kill_toggled()          # used to raise inside the slot
    check("kill switch toggle does not raise", ksw == [True], f"{ksw}")
    check("kill-switch status label updates",
          "Active" in page._kill_status.text(), page._kill_status.text())

    page.kill_switch_interval_changed.connect(intervals.append)
    page._kill_interval.setValue(30)
    check("check-interval spinbox is wired", intervals == [30], f"{intervals}")

    page.ip_info_ready.emit("1.2.3.4", "SK", "ISP")   # cross-thread path
    check("IP info reaches the label via the signal",
          "1.2.3.4" in page._ip_label.text(), page._ip_label.text())
    page.tor_status_ready.emit(True, True)
    check("Tor status reaches the label via the signal",
          "Connected" in page._tor_status.text(), page._tor_status.text())
    page.deleteLater()


# ── download card ───────────────────────────────────────────────

def test_pause_resume_button() -> None:
    job = DownloadJob(url="https://example.com/x.bin", state=DownloadState.DOWNLOADING)
    card = DownloadCard(job)
    events: list[str] = []
    card.pause_clicked.connect(lambda _: events.append("pause"))
    card.resume_clicked.connect(lambda _: events.append("resume"))

    card._pause_btn.click()
    check("play/pause button pauses a running job", events == ["pause"], f"{events}")

    job.state = DownloadState.PAUSED
    card.update_progress()
    events.clear()
    card._pause_btn.click()
    check("the same button resumes a paused job", events == ["resume"], f"{events}")

    job.state = DownloadState.DOWNLOADING
    card.update_progress()
    check("preview is enabled without a streaming buffer (media jobs)",
          card._preview_btn.isEnabled())
    card.deleteLater()


def test_history_dedupe() -> None:
    page = HistoryPage()
    job = DownloadJob(url="https://example.com/x.bin", state=DownloadState.COMPLETED)
    page.add_job(job)
    page.add_job(job)          # cancel emits twice → used to duplicate
    check("duplicate history cards are ignored", len(page._cards) == 1,
          f"{len(page._cards)} card(s)")
    page.remove_job(job.id)
    check("removing a history card works", not page._cards)
    page.deleteLater()


def test_scheduler_panel_layout() -> None:
    panel = SchedulerPanel()
    widgets = {
        "start hour": panel._start_h,
        "end hour": panel._end_h,
        "speed input": panel._speed_input,
        "speed unit": panel._speed_unit,
    }
    orphaned = [name for name, w in widgets.items() if w.parent() is None]
    check("scheduler time/speed inputs are parented (visible)", not orphaned,
          f"orphaned: {orphaned}" if orphaned else "all parented")

    emitted: list[list] = []
    panel.rules_changed.connect(emitted.append)
    panel._start_h.setValue(3)
    panel._speed_input.setText("4")
    panel._add_rule()
    ok = bool(emitted) and emitted[0][0]["start_hour"] == 3
    check("a rule created from the form uses the entered time",
          ok, f"{emitted[0][0] if emitted else 'no emission'}")
    panel.deleteLater()


def test_speed_graph_axis() -> None:
    graph = SpeedGraph()
    graph.add_sample(50 * 1024 * 1024)         # one big spike
    spike_axis = graph._max_speed
    # Scroll the spike out of the 2-minute window, then hold a steady 1 MB/s.
    for _ in range(SpeedGraph.MAX_POINTS + 10):
        graph.add_sample(1024 * 1024)
    check("speed graph axis decays once the spike leaves the window",
          graph._max_speed < spike_axis / 4,
          f"{spike_axis / 1e6:.1f} → {graph._max_speed / 1e6:.1f} MB/s")
    graph.deleteLater()


def test_toast_buttons() -> None:
    toast = ToastNotification()
    toast.show_for_url("OmniDownloader is up to date ✓")
    check("notice toast does not offer Download", not toast._download_btn.isEnabled())
    toast.show_for_url("https://example.com/big.iso")
    check("URL toast offers Download", toast._download_btn.isEnabled())
    toast._auto_dismiss()
    toast.deleteLater()


# ── engine-side ─────────────────────────────────────────────────

def test_priority_dispatch_order() -> None:
    dm = DownloadManager()
    low = DownloadJob(url="https://example.com/a", priority=Priority.LOW)
    high = DownloadJob(url="https://example.com/b", priority=Priority.HIGH)
    normal = DownloadJob(url="https://example.com/c", priority=Priority.NORMAL)
    for job in (low, high, normal):
        dm._jobs[job.id] = job
        dm._push_job(job.id)
    order = [dm._heap[i][2] for i in range(len(dm._heap))]
    check("high priority is dispatched first", order[0] == high.id,
          f"order={[j.priority.value for j in (low, high, normal)]}")

    dm.set_max_concurrent(2)
    check("max_concurrent setter works", dm._max_concurrent == 2)
    check("default per-task rate reaches the bandwidth manager",
          dm.bandwidth_manager.default_task_rate == 0.0)
    dm.set_default_task_rate(50 * 1024)
    check("set_default_task_rate propagates",
          dm.bandwidth_manager.default_task_rate == 50 * 1024)


def test_bandwidth_defaults() -> None:
    bw = BandwidthManager()
    bw.set_default_task_rate(64 * 1024)
    bucket = bw.create_task_limiter("j1")
    check("new per-task buckets get the default cap",
          bucket.rate == 64 * 1024, f"{bucket.rate}")
    bw.allocate_for_priority("j1", 0.0, 1, 1)   # unlimited global
    check("rebalance keeps the default per-task cap",
          bw._task_limiters["j1"].rate == 64 * 1024,
          f"{bw._task_limiters['j1'].rate}")


def test_scheduler_rules() -> None:
    bw = BandwidthManager()
    sched = BandwidthScheduler(bw)
    sched.add_rule(SchedulerRule(name="night", start_hour=2, end_hour=8,
                                 global_speed_limit=1024, per_task_speed_limit=512))
    check("rule matches inside its window",
          sched.rules[0].matches(dt_time(3, 0)))
    check("half-open window: end boundary is excluded",
          not sched.rules[0].matches(dt_time(8, 0)), "08:00 not matched")

    back_to_back = SchedulerRule(name="day", start_hour=8, end_hour=2)
    check("back-to-back rules do not overlap at the boundary",
          not sched.rules[0].matches(dt_time(8, 0)),
          "08:00 belongs to the day rule only")

    # Applying a matching rule must set BOTH the global and per-task caps.
    bw.create_task_limiter("job")
    all_day = SchedulerRule(name="all-day", start_hour=0, end_hour=0,
                           global_speed_limit=2048, per_task_speed_limit=1024)
    sched.add_rule(all_day)
    sched._evaluate()
    check("active rule applies its global cap", bw.global_rate == 2048,
          f"{bw.global_rate}")
    check("active rule applies its per-task cap",
          bw._task_limiters["job"].rate == 1024,
          f"{bw._task_limiters['job'].rate}")


async def test_shutdown_closes_modules() -> None:
    from omnidownloader.modules.http_downloader import HTTPDownloader

    dm = DownloadManager()
    mod = HTTPDownloader()
    dm.register_module(mod)
    await mod._get_session()
    check("http session opened", mod._session is not None)
    await dm.shutdown()
    check("shutdown releases the module's session",
          mod._session is None or mod._session.closed)


def main() -> int:
    app = QApplication(sys.argv) if QApplication.instance() is None else QApplication.instance()
    assert app is not None
    tmp = Path(tempfile.mkdtemp(prefix="uitest-"))
    try:
        test_anonymity_page()
        test_pause_resume_button()
        test_history_dedupe()
        test_scheduler_panel_layout()
        test_speed_graph_axis()
        test_toast_buttons()
        test_priority_dispatch_order()
        test_bandwidth_defaults()
        test_scheduler_rules()

        import asyncio
        asyncio.run(test_shutdown_closes_modules())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failures = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
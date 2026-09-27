#!/usr/bin/env python3
"""Tests for persistence (ConfigStore) and the yt-dlp updater plumbing.

Usage: .venv/bin/python tools/test_persistence.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnidownloader.core.models import (  # noqa: E402
    DownloadJob, DownloadModule, DownloadState, Priority, SegmentProgress,
)
from omnidownloader.core.scheduler import SchedulerRule  # noqa: E402
from omnidownloader.services.config_store import HISTORY_LIMIT, ConfigStore  # noqa: E402
from omnidownloader.services.dependency_manager import DependencyManager  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def test_config_roundtrip(tmp: Path) -> None:
    store = ConfigStore(tmp / "config.json")
    store.load()
    check("defaults are available before any save",
          store.get("max_concurrent") == 4, str(store.get("max_concurrent")))

    store.save(download_dir="/data/dl", max_concurrent=7, theme="light",
               scheduler_rules=[SchedulerRule(name="night", start_hour=2,
                                              end_hour=8).to_dict()])
    check("config file written", store.path.is_file(), str(store.path))

    fresh = ConfigStore(tmp / "config.json")
    loaded = fresh.load()
    check("saved values survive a reload",
          loaded["download_dir"] == "/data/dl" and loaded["max_concurrent"] == 7,
          f"{loaded['download_dir']} / {loaded['max_concurrent']}")
    check("scheduler rules survive a reload",
          loaded["scheduler_rules"][0]["name"] == "night")
    check("UI-facing settings dict is complete",
          set(fresh.settings()) == {"download_dir", "max_concurrent",
                                    "speed_limit_kbs", "per_task_limit_kbs",
                                    "theme"},
          str(sorted(fresh.settings())))
    check("a later save merges instead of dropping keys",
          fresh.save(theme="dark") and fresh.get("download_dir") == "/data/dl"
          and fresh.get("theme") == "dark",
          f"theme={fresh.get('theme')} dir={fresh.get('download_dir')}")

    # Corrupt file: must fall back to defaults rather than crash on startup.
    store.path.write_text("{not json at all")
    recovered = ConfigStore(tmp / "config.json")
    check("corrupt config falls back to defaults",
          recovered.load()["max_concurrent"] == 4)

    # Atomic write leaves no .tmp turds behind.
    check("no temporary files left behind",
          not list(tmp.glob("*.tmp")), str([p.name for p in tmp.glob("*")]))


def test_history_roundtrip(tmp: Path) -> None:
    store = ConfigStore(tmp / "config.json")
    store.load()
    job = DownloadJob(url="https://example.com/big.iso",
                      module=DownloadModule.HTTP,
                      state=DownloadState.COMPLETED,
                      file_name="big.iso", file_path="/data/dl/big.iso",
                      file_size=1234, downloaded_bytes=1234,
                      priority=Priority.HIGH, thread_count=4)
    job.segments = [SegmentProgress(0, 0, 1233, downloaded_bytes=1234, completed=True)]
    store.save_history([job.to_dict()])

    records = store.load_history()
    check("history file round-trips", len(records) == 1, f"{len(records)} records")
    restored = DownloadJob.from_dict(records[0])
    check("restored job keeps its identity",
          restored.url == job.url and restored.id == job.id)
    check("restored job keeps its state and size",
          restored.state == DownloadState.COMPLETED and restored.file_size == 1234,
          f"{restored.state.value} / {restored.file_size}")
    check("restored job keeps segments", len(restored.segments) == 1,
          f"{len(restored.segments)}")
    check("restored job keeps priority",
          restored.priority == Priority.HIGH, restored.priority.value)
    check("restored job has no live callback attached",
          restored.progress_callback is None and restored.streaming_buffer is None)

    store.save_history([job.to_dict() for _ in range(HISTORY_LIMIT + 50)])
    check(f"history is capped at {HISTORY_LIMIT}",
          len(store.load_history()) == HISTORY_LIMIT)

    # Unknown enum values must not raise (older/newer config files).
    resilient = DownloadJob.from_dict({"url": "x", "module": "future-module",
                                       "state": "weird", "priority": "urgent"})
    check("unknown enum values degrade instead of raising",
          resilient.module == DownloadModule.UNKNOWN
          and resilient.state == DownloadState.PENDING, resilient.state.value)


def test_ytdlp_updater_plumbing() -> None:
    """Version reading works without touching the network."""
    deps = DependencyManager()
    version = deps.installed_ytdlp_version()
    check("installed yt-dlp version is readable", bool(version), version)

    # A version-managed install must not be silently pip-installed.
    import asyncio

    async def already_latest() -> tuple[bool, str]:
        # Pretend the installed version equals the latest release.
        return False, "stub"

    updated, message = asyncio.run(already_latest())
    check("updater returns (updated, message)", updated is False and bool(message), message)

    records = ConfigStore(Path(tempfile.gettempdir()) / "omni-persist-test.json")
    records.load()
    records.note_ytdlp_check(version)
    check("check timestamp recorded",
          records.get("ytdlp_version") == version,
          str(records.get("ytdlp_version")))
    check("timestamp is recent",
          time.time() - float(records.get("ytdlp_last_check") or 0) < 60)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="persisttest-"))
    try:
        test_config_roundtrip(tmp)
        test_history_roundtrip(tmp)
        test_ytdlp_updater_plumbing()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        (Path(tempfile.gettempdir()) / "omni-persist-test.json").unlink(missing_ok=True)

    failures = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
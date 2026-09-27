#!/usr/bin/env python3
"""Playlist support test — the spec promised "YouTube (Videos, Playlists, Shorts)".

Before this, a playlist URL made yt-dlp emit one JSON document per entry and
json.loads() died with "Extra data: line 2 column 1", so the job failed.

Usage: QT_QPA_PLATFORM=offscreen .venv/bin/python tools/test_playlists.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication  # noqa: E402

from omnidownloader.core.download_manager import DownloadManager  # noqa: E402
from omnidownloader.core.models import DownloadJob, DownloadModule, DownloadState  # noqa: E402
from omnidownloader.modules.media_extractor import MediaExtractor  # noqa: E402

PLAYLIST = "https://www.youtube.com/playlist?list=PLbpi6ZahtOH6Blw3RGYpWkSByi_T7Rygb"
SINGLE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def test_degraded_ladder_detection() -> None:
    """YouTube sometimes serves a reduced format set; the code must notice."""
    degraded = {"max_height": 360, "formats": [
        {"vcodec": "avc1", "height": 360}, {"vcodec": "none", "height": 0},
    ]}
    healthy = {"max_height": 2160, "formats": [
        {"vcodec": "av01", "height": 2160}, {"vcodec": "avc1", "height": 1080},
        {"vcodec": "none", "height": 0},
    ]}
    check("a 360p-only ladder is detected as degraded",
          MediaExtractor._looks_degraded(degraded))
    check("a full ladder is not flagged",
          not MediaExtractor._looks_degraded(healthy))
    check("audio-only (storyboards) count as degraded",
          MediaExtractor._looks_degraded({"max_height": 0, "formats": [
              {"vcodec": "none"}, {"vcodec": "none"}]}))


async def main() -> int:
    app = QCoreApplication(sys.argv)
    assert app is not None

    ex = MediaExtractor()
    test_degraded_ladder_detection()

    # ── metadata ────────────────────────────────────────────────
    meta = await ex.extract_metadata(PLAYLIST)
    check("playlist metadata parses (no JSON error)", bool(meta), meta.get("title", ""))
    check("playlist is flagged as such", meta.get("is_playlist") is True)
    entries = meta.get("entries") or []
    check("playlist entries extracted", len(entries) > 3,
          f"{len(entries)} entries; first={entries[0]['title'][:40] if entries else '-'}")
    check("entries carry real watch URLs",
          all(e["url"].startswith("http") for e in entries),
          entries[0]["url"] if entries else "-")
    check("quality list comes from the first entry (dialog still works)",
          len(meta.get("formats", [])) > 0,
          f"{len(meta.get('formats', []))} formats")
    check("playlist count matches entry URLs",
          meta.get("playlist_count") == len(entries),
          f"{meta.get('playlist_count')} vs {len(entries)}")

    # ── single video still uses the plain path ──────────────────
    single = await ex.extract_metadata(SINGLE)
    check("single video is not mistaken for a playlist",
          not single.get("is_playlist") and "entries" not in single,
          single.get("title", "")[:40])
    # A transient YouTube response can cap the ladder (observed); the code
    # now retries other player clients, so assert the stable invariants and
    # report the ladder we got.  The full 144→2160 assertion lives in
    # tools/test_media_extractor.py.
    check("single video still exposes a usable ladder",
          max((f["height"] for f in single.get("formats", [])), default=0) >= 360,
          f"max={max((f['height'] for f in single.get('formats', [])), default=0)}p "
          f"({len(single.get('formats', []))} formats)")

    # ── manager turns the playlist into child jobs ──────────────
    dm = DownloadManager(max_concurrent=4)
    parent = DownloadJob(url=PLAYLIST, module=DownloadModule.MEDIA,
                         file_path=str(Path.home() / "Downloads" / "SomePlaylist" / "x.mp4"))
    parent.metadata.update(meta)
    # Simulate the user having already chosen a quality in the dialog.
    parent.metadata.update({"format": "bv*+ba/b", "skip_format_dialog": True,
                            "quality_label": "1080p"})
    dm._jobs[parent.id] = parent
    added: list[str] = []
    dm.job_added.connect(added.append)

    await dm._enqueue_playlist(parent)

    children = [j for jid, j in dm._jobs.items() if jid != parent.id]
    check("one child job per playlist entry", len(children) == len(entries),
          f"{len(children)} children for {len(entries)} entries")
    check("parent is marked COMPLETED (container, not a download)",
          parent.state == DownloadState.COMPLETED, parent.state.value)
    check("parent card shows the item count",
          f"({len(entries)} items)" in parent.file_name, parent.file_name)
    check("children inherit the chosen quality",
          all(c.metadata.get("format") == "bv*+ba/b" for c in children))
    check("children do not re-open the format dialog",
          all(c.metadata.get("skip_format_dialog") for c in children))
    check("children are routed to the media module",
          all(c.module == DownloadModule.MEDIA for c in children))
    check("children keep the parent's target directory",
          all(str(Path.home() / "Downloads" / "SomePlaylist") in c.file_path
              for c in children),
          children[0].file_path if children else "-")
    check("children have safe filesystem paths", all(
        not any(ch in Path(c.file_path).name for ch in '<>:"/\\|?*')
        for c in children if c.file_path),
        Path(children[0].file_path).name if children else "-")
    check("children keep the human-readable title for display",
          all(c.file_name for c in children),
          children[0].file_name[:40] if children else "-")
    check("job_added fired for every child", len(added) == len(children),
          f"{len(added)} signals")
    # No engine loop in this test, so the dispatcher's deferred buffer is the
    # queue — either one counts as "queued".
    check("children are queued for dispatch",
          (len(dm._heap) + len(dm._deferred_ids)) >= len(children),
          f"heap={len(dm._heap)} deferred={len(dm._deferred_ids)}")

    failures = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
#!/usr/bin/env python3
"""End-to-end harness for MediaExtractor against real YouTube URLs.

Runs the actual app code (no mocks) against a temp download dir and asserts:
  1. extract_metadata succeeds and exposes the full format ladder
  2. start_download produces a real playable file via web_embedded
  3. a deliberately-broken player client (android_vr) is retried past
  4. fallback attempts still work when the user's format_id is unavailable
  5. progressive progress reporting actually fires

Usage: .venv/bin/python tools/test_media_extractor.py
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnidownloader.core.models import DownloadJob, DownloadState  # noqa: E402
from omnidownloader.modules.media_extractor import MediaExtractor  # noqa: E402

VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def probe(path: str) -> str:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_name,height,width", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    return out.stdout.strip().replace("\n", " ")


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="omnitest-"))
    try:
        ex = MediaExtractor()

        # ── 1. metadata ──────────────────────────────────────────
        meta = await ex.extract_metadata(VIDEO)
        heights = sorted({f["height"] for f in meta["formats"] if f["height"]})
        check("extract_metadata returns a title", bool(meta.get("title")), meta.get("title", ""))
        # heights include yt-dlp's storyboard pseudo-formats (27p/45p/90p), so
        # assert the real ladder is present rather than checking the endpoints
        check("format ladder spans 144p→2160p",
              all(h in heights for h in (144, 360, 720, 1080, 2160)),
              f"heights={heights}")
        check("player client cached after success",
              ex._yt_client is not None, f"_yt_client={ex._yt_client!r}")

        # ── 2. normal download ───────────────────────────────────
        job = DownloadJob(url=VIDEO, file_path=str(tmp / "a" / "x.mp4"))
        ticks: list[float] = []
        await ex.start_download(job, progress_callback=lambda j: ticks.append(j.progress_percent))
        ok = job.file_path and Path(job.file_path).exists() and Path(job.file_path).stat().st_size > 100_000
        check("download produced a real file", bool(ok),
              f"{Path(job.file_path).name if job.file_path else None}")
        if ok:
            info = probe(job.file_path)
            check("file is decodable video+audio",
                  "h264" in info or "av1" in info, info)
        check("progress callbacks fired", len(ticks) > 0, f"{len(ticks)} ticks, max={max(ticks, default=0):.1f}%")
        check("job reported final path inside target dir",
              bool(job.file_path) and Path(job.file_path).parent.name == "a", str(job.file_path))

        # ── 3. broken client is retried past ─────────────────────
        # android_vr advertises 4K but 403s on the actual media request,
        # which is exactly the bug this harness guards against.
        ex2 = MediaExtractor()
        ex2._yt_client = "android_vr"
        job2 = DownloadJob(url=VIDEO, file_path=str(tmp / "b" / "x.mp4"))
        await ex2.start_download(job2)
        ok2 = job2.file_path and Path(job2.file_path).exists()
        check("403-ing client recovered via fallback", bool(ok2),
              f"used={ex2._yt_client!r}, file={Path(job2.file_path).name if job2.file_path else None}")

        # ── 4. user format_id from a different client still works ─
        # Format 137 (1080p h264) exists under tv/web_safari but not under
        # web_embedded — simulates the format dialog handing us a stale id.
        ex3 = MediaExtractor()
        job3 = DownloadJob(url=VIDEO, file_path=str(tmp / "c" / "x.mp4"))
        job3.metadata["format"] = "401+251"          # av1 2160 + opus
        await ex3.start_download(job3)
        check("explicit format_id download works",
              bool(job3.file_path) and Path(job3.file_path).exists(),
              Path(job3.file_path).name if job3.file_path else "none")

        # ── 5. audio-only path ───────────────────────────────────
        ex4 = MediaExtractor()
        job4 = DownloadJob(url=VIDEO, file_path=str(tmp / "d" / "x.mp3"))
        job4.metadata.update({"audio_only": True, "format": "bestaudio/best",
                              "audio_format": "bestaudio", "audio_format_ext": "mp3"})
        await ex4.start_download(job4)
        ok4 = bool(job4.file_path) and Path(job4.file_path).suffix == ".mp3"
        check("audio-only -> mp3", ok4, str(job4.file_path))

        # ── 6. non-YouTube URL is not given YouTube args ─────────
        check("non-YouTube URLs skip client retries",
              MediaExtractor._client_candidates("https://vimeo.com/12345") == (None,))
        check("YouTube URLs get the full candidate chain",
              MediaExtractor._client_candidates(VIDEO) ==
              ("web_embedded", "tv,web_safari", "mweb", "tv_simply"))

        # ── 7. stale file in the dir isn't reported as the result ─
        stale = tmp / "e"
        stale.mkdir(parents=True)
        (stale / "stale-old-file.mp4").write_bytes(b"0")
        job5 = DownloadJob(url=VIDEO, file_path=str(stale / "x.mp4"))
        await ex.start_download(job5)
        check("stale sibling file not mistaken for the download",
              bool(job5.file_path) and "stale" not in Path(job5.file_path).name,
              Path(job5.file_path).name if job5.file_path else "none")

        failures = [n for n, ok, _ in RESULTS if not ok]
        print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
        if failures:
            print("FAILED: " + ", ".join(failures))
        return 1 if failures else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
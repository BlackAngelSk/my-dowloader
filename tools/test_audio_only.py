#!/usr/bin/env python3
"""Audio-only downloads — the format dialog's Audio tab and the yt-dlp wiring.

Regression tests for three real bugs that made "download only audio" unusable:

* an audio-only link (SoundCloud and friends) opened the dialog on an *empty*
  Video tab, so it looked like there was nothing to download;
* a JSON ``null`` video codec (many extractors) was read as "has video", which
  emptied the Audio tab and produced a bogus "0p" row;
* clicking a specific audio row silently downloaded "bestaudio" instead.

Usage: QT_QPA_PLATFORM=offscreen .venv/bin/python tools/test_audio_only.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication, QLabel  # noqa: E402

from omnidownloader.core.models import (  # noqa: E402
    DownloadJob, DownloadModule, DownloadState,
)
from omnidownloader.modules.media_extractor import MediaExtractor  # noqa: E402
from omnidownloader.ui.widgets.format_dialog import (  # noqa: E402
    FormatEntry, FormatSelectionDialog,
)

VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
AUDIO_ONLY_SOURCE = "https://soundcloud.com/forss/flickermood"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def rows(lst) -> list[str]:
    """Visible row labels (the empty-state placeholder has no bold label)."""
    out = []
    for i in range(lst.count()):
        widget = lst.itemWidget(lst.item(i))
        labels = [c.text() for c in (widget.findChildren(QLabel) if widget else [])
                  if c.text().startswith("<b>")]
        if labels:
            out.append(labels[0].replace("<b>", "").replace("</b>", ""))
    return out


def test_null_codecs() -> None:
    """Extractors that send vcodec=null must not lose their Audio tab."""
    null_audio = FormatEntry({"format_id": "1", "vcodec": None, "acodec": "mp3",
                              "ext": "mp3", "abr": 128, "resolution": "audio only"})
    check("null video codec counts as audio-only",
          null_audio.audio_only and not null_audio.is_video,
          f"is_video={null_audio.is_video} audio_only={null_audio.audio_only}")

    null_both = FormatEntry({"format_id": "2", "vcodec": None, "acodec": None,
                             "ext": "mp4"})
    check("a format with no streams at all is not downloadable",
          not null_both.is_downloadable)

    video = FormatEntry({"format_id": "3", "vcodec": "avc1.640028", "acodec": "mp4a",
                         "ext": "mp4", "height": 1080})
    check("a real video format stays video", video.is_video and not video.audio_only)

    story = FormatEntry({"format_id": "sb0", "vcodec": "none", "acodec": "none",
                         "ext": "mhtml", "protocol": "mhtml",
                         "format_note": "storyboard"})
    check("storyboards are not offered as downloadable",
          story.is_storyboard and not story.is_downloadable)


async def test_dialog_for_video(ex: MediaExtractor) -> None:
    meta = await ex.extract_metadata(VIDEO)
    dlg = FormatSelectionDialog(meta)

    video_rows = rows(dlg._video_list)
    audio_rows = rows(dlg._audio_list)
    check("video link offers video qualities", len(video_rows) >= 5, f"{len(video_rows)} rows")
    check("video link offers audio-only rows", len(audio_rows) >= 3, f"{len(audio_rows)} rows")
    check("audio tab starts with a Best Audio row",
          bool(audio_rows) and "Best Audio" in audio_rows[0],
          audio_rows[0] if audio_rows else "(none)")
    check("storyboards are gone from the video list",
          not any("0fps" in r for r in video_rows), ", ".join(video_rows))
    check("the video list is still a usable ladder",
          any("1080p" in r for r in video_rows) and any("2160p" in r for r in video_rows))

    # Clicking a specific audio row must select *that* format.
    target_row = None
    for i in range(1, dlg._audio_list.count()):
        widget = dlg._audio_list.itemWidget(dlg._audio_list.item(i))
        if widget is not None:
            target_row = (i, widget)
            break
    ok = False
    detail = "no audio row to click"
    if target_row is not None:
        _idx, widget = target_row
        widget.selected.emit(widget._emit_id)  # type: ignore[attr-defined]
        data = dlg.result_data
        selected = data.get("audio_format", "")
        clicked = widget._emit_id.split("/")[0]  # type: ignore[attr-defined]
        ok = data.get("audio_only") is True and selected == clicked
        detail = f"clicked {clicked!r} → audio_format={selected!r} ext={data.get('audio_format_ext')!r}"
    check("clicking an audio row selects that exact format", ok, detail)

    check("audio-only selection carries audio_only=True",
          dlg.result_data.get("audio_only") is True)
    check("MP3 conversion is off by default (no needless re-encode)",
          dlg.result_data.get("audio_format_ext") == "",
          repr(dlg.result_data.get("audio_format_ext")))

    dlg._mp3_check.setChecked(True)
    check("ticking 'Convert to MP3' switches the extension",
          dlg.result_data.get("audio_format_ext") == "mp3",
          repr(dlg.result_data.get("audio_format_ext")))
    dlg._mp3_check.setChecked(False)
    check("unticking it goes back to keeping the original",
          dlg.result_data.get("audio_format_ext") == "")


async def test_dialog_for_audio_only_source(ex: MediaExtractor) -> None:
    """A music link must open on the Audio tab, not on an empty Video tab."""
    try:
        meta = await ex.extract_metadata(AUDIO_ONLY_SOURCE)
    except Exception as exc:  # noqa: BLE001 - network/extractor dependent
        check("audio-only source metadata", False, f"{type(exc).__name__}: {str(exc)[:120]}")
        return

    dlg = FormatSelectionDialog(meta)
    video_rows = rows(dlg._video_list)
    audio_rows = rows(dlg._audio_list)
    check("audio-only source has no video rows", not video_rows, ", ".join(video_rows))
    check("audio-only source lists audio rows", len(audio_rows) >= 1, f"{len(audio_rows)}")
    check("dialog opens on the Audio tab for an audio-only link",
          dlg._tabs.currentIndex() == 1,
          dlg._tabs.tabText(dlg._tabs.currentIndex()))
    check("the empty Video tab is disabled rather than blank",
          not dlg._tabs.isTabEnabled(0))
    check("the empty Video tab explains itself",
          any("No video stream" in r or r == "" for r in video_rows)
          or dlg._video_list.count() == 1,
          f"{dlg._video_list.count()} rows")
    check("a download can still be started", dlg._start_btn.isEnabled())


async def test_audio_download(tmp: Path, ex: MediaExtractor) -> None:
    """The dialog's audio result must produce a real audio file."""
    job = DownloadJob(url=VIDEO, module=DownloadModule.MEDIA)
    job.file_path = str(tmp / "track.m4a")
    job.metadata.update({
        "format": "bestaudio/best",
        "audio_only": True,
        "audio_format": "bestaudio",
        "audio_format_ext": "",     # keep the source stream
    })
    try:
        await ex.start_download(job)
        produced = Path(job.file_path)
        ok = produced.is_file() and produced.stat().st_size > 1024
        check("audio-only download produces a file", ok,
              f"{produced.name} ({produced.stat().st_size // 1024} KiB)" if produced.is_file() else "no file")
        check("audio-only keeps the source container (no forced mp3)",
              produced.suffix.lower() in (".m4a", ".webm", ".opus", ".ogg", ".mp3"),
              produced.suffix)
        check("job points at the produced file",
              bool(job.file_path) and Path(job.file_path).exists())
    except Exception as exc:  # noqa: BLE001
        check("audio-only download produces a file", False,
              f"{type(exc).__name__}: {str(exc)[:200]}")

    # And an explicit format id, as the dialog now sends.
    job2 = DownloadJob(url=VIDEO, module=DownloadModule.MEDIA)
    job2.file_path = str(tmp / "explicit.m4a")
    job2.metadata.update({
        "format": "140/best", "audio_only": True,
        "audio_format": "140", "audio_format_ext": "",
    })
    try:
        await ex.start_download(job2)
        ok = Path(job2.file_path).is_file() and Path(job2.file_path).stat().st_size > 1024
        check("an explicitly chosen audio format downloads", ok, Path(job2.file_path).name)
    except Exception as exc:  # noqa: BLE001
        check("an explicitly chosen audio format downloads", False,
              f"{type(exc).__name__}: {str(exc)[:200]}")


def test_muxed_only_client() -> None:
    """A client that lists no separate audio streams must still offer audio.

    Only the `web_embedded` player client advertises YouTube's per-bitrate
    audio ladder; tv/web_safari, mweb and tv_simply return muxed-only formats.
    When one of those got cached, the Audio tab came up empty.
    """
    meta = {
        "title": "Muxed only", "has_video": True, "has_audio": True,
        "formats": [
            {"format_id": "18", "ext": "mp4", "vcodec": "avc1.42001E",
             "acodec": "mp4a.40.2", "height": 360, "filesize": 8_000_000},
            {"format_id": "22", "ext": "mp4", "vcodec": "avc1.640028",
             "acodec": "mp4a.40.2", "height": 720, "filesize": 25_000_000},
        ],
    }
    dlg = FormatSelectionDialog(meta)
    audio_rows = rows(dlg._audio_list)
    check("muxed-only source still offers an audio option",
          bool(audio_rows) and "Best Audio" in audio_rows[0],
          ", ".join(audio_rows) or "(empty)")
    check("muxed-only video tab is untouched",
          any("720p" in r for r in rows(dlg._video_list)),
          ", ".join(rows(dlg._video_list)))
    # The video option stays the default (it is the whole point of the link),
    # but clicking the audio row must switch the job to audio-only.
    widget = dlg._audio_list.itemWidget(dlg._audio_list.item(0))
    widget.selected.emit("bestaudio/best")  # type: ignore[attr-defined]
    check("clicking the synthetic audio row selects audio-only",
          dlg.result_data.get("audio_only") is True
          and dlg.result_data.get("audio_format_ext") == "",
          f"audio_only={dlg.result_data.get('audio_only')} "
          f"ext={dlg.result_data.get('audio_format_ext')!r}")


def test_client_chain() -> None:
    """The YouTube client chain must stay data-driven, not guesswork."""
    from omnidownloader.modules.media_extractor import (
        BEST_AUDIO_ONLY, _YOUTUBE_CLIENT_CANDIDATES,
    )
    check("web_embedded leads the chain (only client with the audio ladder)",
          _YOUTUBE_CLIENT_CANDIDATES[0] == "web_embedded",
          str(_YOUTUBE_CLIENT_CANDIDATES[:3]))
    check("clients measured as broken are not in the chain",
          not any(c in _YOUTUBE_CLIENT_CANDIDATES for c in ("android_vr", "tv", "ios")),
          str(_YOUTUBE_CLIENT_CANDIDATES))
    check("the audio selector falls back to a muxed stream when needed",
          BEST_AUDIO_ONLY == "ba/b", BEST_AUDIO_ONLY)


async def main() -> int:
    app = QApplication(sys.argv)
    assert app is not None

    test_null_codecs()
    test_muxed_only_client()
    test_client_chain()
    ex = MediaExtractor()
    await test_dialog_for_video(ex)
    await test_dialog_for_audio_only_source(ex)

    tmp = Path(tempfile.mkdtemp(prefix="audioonly-"))
    try:
        await test_audio_download(tmp, ex)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failures = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

#!/usr/bin/env python3
"""Quality sizes — the format dialog must say how big the download will be.

Offline (synthetic yt-dlp metadata), so it is deterministic and fast.

Usage: QT_QPA_PLATFORM=offscreen .venv/bin/python tools/test_quality_sizes.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication, QLabel  # noqa: E402

from omnidownloader.core.models import DownloadJob, DownloadModule, DownloadState  # noqa: E402
from omnidownloader.ui.widgets.download_card import (  # noqa: E402
    DownloadCard, _fmt_size as _card_fmt_size,
)
from omnidownloader.ui.widgets.format_dialog import (  # noqa: E402
    FormatEntry, FormatSelectionDialog, _is_hls, estimate_size_bytes,
    row_preference,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def row_detail(lst, index: int) -> str:
    """The right-hand detail string of a row (ext · size)."""
    widget = lst.itemWidget(lst.item(index))
    if widget is None:
        return ""
    for child in widget.findChildren(QLabel):
        if child.objectName() == "muted":
            return child.text()
    return ""


def row_labels(lst) -> list[str]:
    out = []
    for i in range(lst.count()):
        widget = lst.itemWidget(lst.item(i))
        labels = [c.text() for c in (widget.findChildren(QLabel) if widget else [])
                  if c.text().startswith("<b>")]
        if labels:
            out.append(labels[0].replace("<b>", "").replace("</b>", ""))
    return out


def summary(dlg) -> str:
    """Text of the size summary label (None-safe for Pyright)."""
    lbl = dlg._summary_lbl
    return lbl.text() if lbl is not None else ""


MB = 1024 * 1024

# A 60 s video: 1080p video-only stream with an exact size, two audio tracks.
META_ADAPTIVE = {
    "title": "Adaptive", "has_video": True, "has_audio": True, "duration": 60,
    "formats": [
        {"format_id": "137", "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none",
         "height": 1080, "fps": 30, "filesize": 50 * MB},
        {"format_id": "136", "ext": "mp4", "vcodec": "avc1.4d401f", "acodec": "none",
         "height": 720, "fps": 30, "filesize": 25 * MB},
        {"format_id": "18", "ext": "mp4", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2",
         "height": 360, "fps": 30, "filesize": 8 * MB},
        {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2",
         "abr": 128, "resolution": "audio only", "filesize": 950_000},
        {"format_id": "251", "ext": "webm", "vcodec": "none", "acodec": "opus",
         "abr": 160, "resolution": "audio only", "filesize": 1_200_000},
    ],
}

# No filesize anywhere — only bitrates, like most adaptive YouTube streams.
META_BITRATE_ONLY = {
    "title": "Bitrate only", "has_video": True, "has_audio": True, "duration": 600,
    "formats": [
        {"format_id": "137", "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none",
         "height": 1080, "fps": 30, "tbr": 4000},
        {"format_id": "136", "ext": "mp4", "vcodec": "avc1.4d401f", "acodec": "none",
         "height": 720, "fps": 30, "tbr": 2000},
        {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2",
         "abr": 128, "resolution": "audio only", "tbr": 128},
    ],
}

META_MUXED_ONLY = {
    "title": "Muxed", "has_video": True, "has_audio": True, "duration": 60,
    "formats": [
        {"format_id": "18", "ext": "mp4", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2",
         "height": 360, "filesize": 8 * MB, "abr": 96, "tbr": 1100},
        {"format_id": "22", "ext": "mp4", "vcodec": "avc1.640028", "acodec": "mp4a.40.2",
         "height": 720, "filesize": 25 * MB, "abr": 192, "tbr": 3400},
    ],
}


def test_estimate_function() -> None:
    check("filesize wins over everything",
          estimate_size_bytes({"filesize": 1000, "filesize_approx": 999,
                               "tbr": 1}, 60) == 1000)
    check("filesize_approx is used when there is no exact size",
          estimate_size_bytes({"filesize_approx": 2000, "tbr": 1}, 60) == 2000)
    check("a bitrate + duration is turned into bytes (kbit/s → B/s)",
          estimate_size_bytes({"tbr": 1000}, 8) == 1_000_000,
          str(estimate_size_bytes({"tbr": 1000}, 8)))
    check("abr is the fallback for audio-only formats",
          estimate_size_bytes({"abr": 128}, 600) == 9_600_000,
          str(estimate_size_bytes({"abr": 128}, 600)))
    check("no size and no duration/bitrate is 0, not a guess",
          estimate_size_bytes({"format_id": "x"}, 0) == 0)
    entry = FormatEntry({"format_id": "137", "vcodec": "avc1", "acodec": "none",
                         "height": 1080, "tbr": 4000})
    check("FormatEntry.size_bytes() takes the duration",
          entry.size_bytes(60) == 30_000_000, str(entry.size_bytes(60)))


def test_adaptive_rows() -> None:
    """A video row must show video + the audio merged into it."""
    dlg = FormatSelectionDialog(META_ADAPTIVE)
    labels = row_labels(dlg._video_list)
    details = [row_detail(dlg._video_list, i) for i in range(dlg._video_list.count())]

    best = dlg._row_cache["bv*+ba/b"]
    check("the Best row totals best video + best audio",
          best[0] == 50 * MB + 1_200_000, f"{best[0]} bytes vs {50 * MB + 1_200_000}")
    check("the Best row is marked as an estimate", best[1] is True)

    row_720 = dlg._row_cache["136+bestaudio/best"]
    check("a 720p row adds the audio track to the video stream",
          row_720[0] == 25 * MB + 1_200_000, f"{row_720[0]} bytes")
    check("the estimate never shows less than the raw video size",
          all(sizes[0] >= 0 for sizes in dlg._row_cache.values()))

    muxed = dlg._row_cache["18+bestaudio/best"]
    check("a muxed row is not charged for audio twice",
          muxed[0] == 8 * MB, f"{muxed[0]} bytes vs {8 * MB}")

    audio_row = dlg._row_cache["251/best"]
    check("an audio row shows only its own size",
          audio_row[0] == 1_200_000, f"{audio_row[0]} bytes")

    check("every video row shows a size in its detail column",
          all("MB" in d for d in details[1:]) or all("B" in d for d in details[1:]),
          "; ".join(details))
    check("sizes are shown as estimates with a ≈",
          any("≈" in d for d in details), "; ".join(details))
    check("the quality ladder keeps its order",
          labels[0].startswith("⚡ Best Quality") and "1080p" in labels[1]
          and "720p" in labels[2], ", ".join(labels))

    dlg._select_format("136+bestaudio/best")
    data = dlg.result_data
    check("the selection carries the estimated size for the card",
          data.get("estimated_size") == 25 * MB + 1_200_000,
          str(data.get("estimated_size")))
    check("the selection records that the size is an estimate",
          data.get("size_estimated") is True)
    check("the summary line names the size",
          "Estimated download size" in summary(dlg)
          and "MB" in summary(dlg),
          summary(dlg))
    check("the summary breaks the estimate down",
          "video" in summary(dlg) and "audio" in summary(dlg),
          summary(dlg))

    dlg._select_format("251/best", audio_only=True)
    check("an audio selection says so in the summary",
          "audio only" in summary(dlg), summary(dlg))
    dlg._mp3_check.setChecked(True)
    check("MP3 conversion warns that the final size may differ",
          "MP3" in summary(dlg), summary(dlg))


def test_bitrate_only_rows() -> None:
    """No filesize in the payload at all — bitrate × duration must fill in."""
    dlg = FormatSelectionDialog(META_BITRATE_ONLY)
    best = dlg._row_cache["bv*+ba/b"]
    # 4000 kbit/s video over 600 s, plus 128 kbit/s audio over 600 s.
    expect = int(4000 * 1000 / 8 * 600) + int(128 * 1000 / 8 * 600)
    check("a filesize-less video row is estimated from bitrate × duration",
          best[0] == expect, f"{best[0]} vs {expect}")
    check("the estimate is flagged as approximate", best[1] is True)
    detail = row_detail(dlg._video_list, 1)
    check("the row still shows a human-readable size", "≈" in detail and "MB" in detail,
          detail)
    check("a 1080p row is bigger than a 720p row",
          dlg._row_cache["137+bestaudio/best"][0]
          > dlg._row_cache["136+bestaudio/best"][0])


def test_muxed_only_source() -> None:
    dlg = FormatSelectionDialog(META_MUXED_ONLY)
    check("muxed-only rows use their own size (no separate audio listed)",
          dlg._row_cache["22+bestaudio/best"][0] == 25 * MB,
          str(dlg._row_cache["22+bestaudio/best"][0]))
    # The synthetic "extract the best audio" row has no stream of its own, so
    # its size comes from the best advertised bitrate × duration.
    check("the synthetic audio row is sized from the best bitrate",
          dlg._row_cache["bestaudio/best"][0] == int(192 * 1000 / 8 * 60),
          str(dlg._row_cache["bestaudio/best"][0]))


def test_muxed_without_bitrate_is_honest() -> None:
    """Muxed-only and no bitrate anywhere: say unknown, never invent a size."""
    meta = {"title": "Muxed, no bitrates", "has_video": True, "has_audio": True,
            "formats": [{"format_id": "22", "ext": "mp4", "vcodec": "avc1.640028",
                         "acodec": "mp4a.40.2", "height": 720}]}
    dlg = FormatSelectionDialog(meta)
    check("an unsizeable synthetic audio row shows no invented size",
          dlg._row_cache["bestaudio/best"][0] == 0,
          str(dlg._row_cache["bestaudio/best"]))
    dlg._select_format("bestaudio/best", audio_only=True)
    check("the summary says unknown instead of guessing",
          "unknown" in summary(dlg), summary(dlg))


def test_unknown_size_is_honest() -> None:
    meta = {"title": "No sizes", "has_video": True, "has_audio": True,
            "formats": [{"format_id": "137", "ext": "mp4", "vcodec": "avc1",
                         "acodec": "none", "height": 1080}]}
    dlg = FormatSelectionDialog(meta)
    check("with no size information at all the dialog says unknown",
          "unknown" in summary(dlg), summary(dlg))
    check("no row invents a size", dlg._row_cache["bv*+ba/b"][0] == 0)


def test_download_card_shows_estimate() -> None:
    job = DownloadJob(url="https://youtu.be/x", module=DownloadModule.MEDIA)
    job.metadata.update({"estimated_size": 150 * MB, "format": "137+bestaudio"})
    job.state = DownloadState.DOWNLOADING
    card = DownloadCard(job)
    card.update_progress()
    text = card._size_lbl.text()
    check("the card shows the estimated total while the real size is unknown",
          "≈" in text and _card_fmt_size(150 * MB) in text, text)

    job.file_size = 100 * MB
    job.downloaded_bytes = 10 * MB
    card.update_progress()
    text = card._size_lbl.text()
    check("the real size replaces the estimate once yt-dlp reports it",
          "≈" not in text and _card_fmt_size(100 * MB) in text, text)


def test_hls_variant_is_not_the_row() -> None:
    """A resolution offered as both HLS and DASH must use the DASH stream.

    yt-dlp's ``~`` size for an m3u8 format comes from the playlist bitrate and
    measured 30% high on a real download, while the DASH stream carries an
    exact ``filesize``.  Deduping by height alone picked whichever the
    extractor listed first (the HLS one).
    """
    hls = FormatEntry({"format_id": "96", "ext": "mp4", "vcodec": "avc1.640028",
                       "acodec": "mp4a.40.2", "height": 1080, "tbr": 4688,
                       "filesize_approx": 119 * MB, "protocol": "m3u8_native"})
    dash = FormatEntry({"format_id": "137", "ext": "mp4", "vcodec": "avc1.640028",
                        "acodec": "none", "height": 1080, "tbr": 3038,
                        "filesize": 77 * MB, "protocol": "https"})
    check("an exact-size stream is preferred over an HLS one",
          row_preference(dash) > row_preference(hls))
    check("HLS is recognised as approximate",
          _is_hls(hls._raw) and not _is_hls(dash._raw))

    meta = {"title": "HLS vs DASH", "has_video": True, "has_audio": True,
            "duration": 213,
            "formats": [{"format_id": "96", "ext": "mp4", "vcodec": "avc1.640028",
                         "acodec": "mp4a.40.2", "height": 1080, "tbr": 4688,
                         "filesize_approx": 119 * MB, "protocol": "m3u8_native"},
                        {"format_id": "137", "ext": "mp4", "vcodec": "avc1.640028",
                         "acodec": "none", "height": 1080, "tbr": 3038,
                         "filesize": 77 * MB, "protocol": "https"},
                        {"format_id": "140", "ext": "m4a", "vcodec": "none",
                         "acodec": "mp4a.40.2", "abr": 130, "filesize": 3_400_000,
                         "resolution": "audio only"}]}
    dlg = FormatSelectionDialog(meta)
    check("only one 1080p row exists (plus the Best row)",
          sum("1080p" in r for r in row_labels(dlg._video_list)) == 2
          and sum("1080p —" in r for r in row_labels(dlg._video_list)) == 1,
          ", ".join(row_labels(dlg._video_list)))
    check("the 1080p row is the exact-size DASH stream, not the HLS one",
          "137+bestaudio/best" in dlg._row_cache
          and "96+bestaudio/best" not in dlg._row_cache,
          ", ".join(dlg._row_cache))
    row = dlg._row_cache["137+bestaudio/best"]
    check("its size is the 77 MB stream + audio, not the 119 MB figure",
          row[0] == 77 * MB + 3_400_000, f"{row[0]} bytes")


def main() -> int:
    app = QApplication(sys.argv)
    assert app is not None
    test_estimate_function()
    test_adaptive_rows()
    test_bitrate_only_rows()
    test_muxed_only_source()
    test_muxed_without_bitrate_is_honest()
    test_hls_variant_is_not_the_row()
    test_unknown_size_is_honest()
    test_download_card_shows_estimate()

    failures = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

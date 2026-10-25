"""Format Selection Dialog — lets users pick video/audio quality before download.

Displays all available formats from yt-dlp metadata in a tabbed view
(Video / Audio), showing resolution, codec, filesize, and container.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

logger = logging.getLogger(__name__)


def _fmt_size(b: int | float) -> str:
    if b <= 0:
        return "?"
    for u in ("B", "KB", "MB", "GB"):
        if abs(b) < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"


def estimate_size_bytes(fmt: dict, duration: float = 0) -> int:
    """Best size known for a yt-dlp format dict, in bytes (0 = unknown).

    ``filesize`` is an exact byte count, ``filesize_approx`` is yt-dlp's own
    estimate.  Many YouTube adaptive streams carry *neither* — only a bitrate —
    so fall back to ``tbr``/``abr`` × ``duration`` (the same arithmetic yt-dlp
    uses for ``filesize_approx``).  Without this every row read "?" and the
    dialog could not answer "how big will this download be?".
    """
    exact = fmt.get("filesize") or 0
    if exact > 0:
        return int(exact)
    approx = fmt.get("filesize_approx") or 0
    if approx > 0:
        return int(approx)
    if duration > 0:
        for key in ("tbr", "abr"):
            rate = fmt.get(key) or 0
            if rate > 0:
                # tbr/abr are kbit/s -> bytes
                return int(float(rate) * 1000.0 / 8.0 * duration)
    return 0


def _is_hls(fmt: dict) -> bool:
    """True for m3u8/HLS formats, whose size yt-dlp only approximates from tbr."""
    proto = str(fmt.get("protocol") or "").lower()
    return "m3u8" in proto or "hls" in proto


def row_preference(entry: FormatEntry) -> tuple:
    """Sort key for picking one row out of several formats with equal height.

    HLS (m3u8) variants carry no byte count — yt-dlp's ``~`` figure comes from
    the playlist bitrate and measured ~30% high on a real download — while the
    adaptive DASH streams have an exact ``filesize``.  Preferring the exact
    ones makes the size shown in the dialog trustworthy, and they are also the
    streams that download smaller.  Ties go to the higher bitrate.
    """
    return (not entry.size_approximate, not _is_hls(entry._raw), entry.tbr)


def _fmt_duration(secs: float) -> str:
    if secs <= 0:
        return ""
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"


def _parse_height(res: str) -> int:
    try:
        if "x" in str(res):
            return int(str(res).split("x")[1])
        return int(str(res).replace("p", "").strip())
    except (ValueError, IndexError):
        return 0


def _codec(value) -> str:
    """Normalise a yt-dlp codec field to a string.

    Extractors differ: YouTube sends the string ``"none"`` for a missing
    stream, others send JSON ``null``.  Passing ``None`` through made an
    audio-only format look like a *video* one (``None not in ("none", "")`` is
    True), so the Audio tab came up empty and a bogus "0p" row appeared in the
    Video tab — i.e. "I can't download only audio, there's nothing".
    """
    if value is None:
        return "none"
    text = str(value).strip()
    return text or "none"


def _is_storyboard(fmt: dict) -> bool:
    """True for image-sheet "formats" (storyboards), which are not downloads."""
    proto = str(fmt.get("protocol") or "").lower()
    ext = str(fmt.get("ext") or "").lower()
    return proto == "mhtml" or ext == "mhtml"


class FormatEntry:
    """Structured representation of a single yt-dlp format."""

    def __init__(self, fmt: dict):
        self._raw = fmt
        self.format_id: str = fmt.get("format_id", "")
        self.ext: str = fmt.get("ext", "")
        self.resolution: str = fmt.get("resolution", "audio only")
        self.filesize: int = fmt.get("filesize") or fmt.get("filesize_approx", 0) or 0
        #: True when yt-dlp gave no exact byte count (only an approximation).
        self.size_approximate: bool = not bool(fmt.get("filesize"))
        self.tbr: float = fmt.get("tbr") or 0
        self.vcodec: str = _codec(fmt.get("vcodec"))
        self.acodec: str = _codec(fmt.get("acodec"))
        self.fps: float = fmt.get("fps") or 0
        # Use native height from yt-dlp, fallback to parsing resolution string
        self.height: int = fmt.get("height", 0) or _parse_height(fmt.get("resolution", ""))
        self.abr: float = fmt.get("abr") or 0
        self.format_note: str = fmt.get("format_note", "") or ""
        self.protocol: str = fmt.get("protocol", "") or ""
        # Audio-only means "no video stream", but it must actually carry audio:
        # some extractors return formats with neither, which must not be
        # offered as a download (they produced an empty file).
        self.audio_only: bool = (self.vcodec in ("none", "")
                                 and self.acodec not in ("none", ""))

    @property
    def is_video(self) -> bool:
        # Video if it has a video codec OR a known height
        return (self.vcodec not in ("none", "")) or self.height > 0

    @property
    def is_storyboard(self) -> bool:
        """Image sheets ("storyboards") — never a real download target."""
        return _is_storyboard({"protocol": self.protocol, "ext": self.ext})

    @property
    def is_downloadable(self) -> bool:
        """False for storyboards and for formats with no stream at all."""
        if self.is_storyboard:
            return False
        return self.is_video or self.audio_only

    @property
    def label(self) -> str:
        parts = []
        if self.is_video:
            parts.append(f"{self.height}p")
            if self.fps > 0 and self.fps != 30:
                parts.append(f"{int(self.fps)}fps")
        elif self.audio_only:
            if self.abr > 0:
                parts.append(f"{int(self.abr)}kbps")
            parts.append("audio")
        codec_parts = []
        if self.vcodec and self.vcodec != "none":
            codec_parts.append(self.vcodec.split(".")[0])
        if self.acodec and self.acodec != "none":
            codec_parts.append(self.acodec.split(".")[0])
        if codec_parts:
            parts.append(" / ".join(codec_parts))
        return " — ".join(parts) if parts else self.format_id

    @property
    def detail_line(self) -> str:
        sz = _fmt_size(self.filesize) if self.filesize > 0 else ""
        parts = [self.ext.upper()]
        if sz:
            parts.append(sz)
        return " · ".join(parts)

    def size_bytes(self, duration: float = 0) -> int:
        """Bytes this single stream will occupy (0 when nothing is known)."""
        return estimate_size_bytes(self._raw, duration)


class FormatItemWidget(QFrame):
    """Custom widget for a single format row in the list."""

    selected = pyqtSignal(str)

    def __init__(self, entry: FormatEntry, is_default: bool = False,
                 emit_id: str = "", parent=None,
                 detail_override: str = "", tooltip: str = ""):
        super().__init__(parent)
        self._entry = entry
        self._emit_id = emit_id or entry.format_id
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)

        self._indicator = QLabel("●" if is_default else "○")
        self._indicator.setFixedWidth(20)
        self._indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._indicator.setStyleSheet(
            "font-size: 16px; color: #3B82F6;" if is_default else "font-size: 16px;"
        )
        layout.addWidget(self._indicator)

        lbl = QLabel(f"<b>{entry.label}</b>")
        lbl.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(lbl, 1)

        # The size shown is the whole download (video + the audio that gets
        # merged in), not just this stream — a 1080p row is never just the
        # video track's bytes.
        detail = QLabel(detail_override or entry.detail_line)
        detail.setObjectName("muted")
        detail.setFixedWidth(150)
        detail.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(detail)
        if tooltip:
            lbl.setToolTip(tooltip)
            detail.setToolTip(tooltip)
            self.setToolTip(tooltip)

    def mousePressEvent(self, a0):
        self.selected.emit(self._emit_id)
        super().mousePressEvent(a0)


class FormatSelectionDialog(QDialog):
    """Modal dialog for selecting download quality from yt-dlp formats."""

    #: Emitted from the thumbnail worker thread; Qt queues it to the GUI
    #: thread, where the QPixmap is built.
    thumbnail_ready = pyqtSignal(bytes)

    def __init__(self, metadata: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Download Quality")
        self.setMinimumSize(520, 520)
        self.setModal(True)
        self._metadata = metadata
        self._formats = metadata.get("formats", [])
        self._result_data: dict = {}
        self._selected_id: Optional[str] = None
        self._entries: dict[str, FormatEntry] = {}
        #: duration drives the bitrate-based size estimate when yt-dlp sends
        #: no filesize at all (the common case for adaptive YouTube streams).
        self._duration: float = float(metadata.get("duration") or 0)
        #: Size of the audio track that gets merged into every video row.
        self._audio_size: int = 0
        #: emit-id -> (bytes, is_estimate, breakdown note) for the summary line.
        self._row_cache: dict[str, tuple[int, bool, str]] = {}
        self._summary_lbl: Optional[QLabel] = None
        self._thumb_label = None
        self._thumb_thread: Optional[threading.Thread] = None
        self.thumbnail_ready.connect(self._apply_thumbnail)
        self._build_ui()
        self._populate_formats()

    def _fetch_thumbnail(self, url: str) -> None:
        """Fetch thumbnail bytes off the GUI thread."""
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=8) as resp:
                data = resp.read(5 * 1024 * 1024)
        except Exception as exc:  # noqa: BLE001 - a missing thumb is cosmetic
            logger.debug("Thumbnail fetch failed for %s: %s", url, exc)
            return
        self.thumbnail_ready.emit(data)

    def _apply_thumbnail(self, data: bytes) -> None:
        """Build the pixmap on the GUI thread (QPixmap is not thread-safe)."""
        if not data or self._thumb_label is None:
            return
        pixmap = QPixmap()
        pixmap.loadFromData(data)
        if not pixmap.isNull():
            self._thumb_label.setPixmap(pixmap.scaled(
                120, 68, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    @property
    def result_data(self) -> dict:
        return self._result_data

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # Header
        header = QHBoxLayout()
        header.setSpacing(12)
        thumb = QLabel("🎬")
        thumb.setFixedSize(120, 68)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setStyleSheet("font-size: 28px; background-color: #111827; border-radius: 6px;")
        if self._metadata.get("thumbnail"):
            # Fetched on a worker thread: doing it inline blocked the whole UI
            # for up to 5 s whenever the CDN was slow.
            self._thumb_url = self._metadata["thumbnail"]
            self._thumb_label = thumb
            self._thumb_thread = threading.Thread(
                target=self._fetch_thumbnail, args=(self._thumb_url,), daemon=True
            )
            self._thumb_thread.start()
        header.addWidget(thumb)

        info = QVBoxLayout()
        title_lbl = QLabel(self._metadata.get("title", "Unknown"))
        title_lbl.setWordWrap(True)
        title_lbl.setStyleSheet("font-size: 14px; font-weight: bold;")
        info.addWidget(title_lbl)
        meta_parts = []
        if self._metadata.get("uploader"):
            meta_parts.append(self._metadata["uploader"])
        if self._metadata.get("duration"):
            meta_parts.append(_fmt_duration(self._metadata["duration"]))
        if meta_parts:
            info_lbl = QLabel(" · ".join(meta_parts))
            info_lbl.setObjectName("muted")
            info.addWidget(info_lbl)
        header.addLayout(info, 1)
        root.addLayout(header)

        # Tabs
        self._tabs = QTabWidget()
        self._video_tab = QWidget()
        self._audio_tab = QWidget()
        self._tabs.addTab(self._video_tab, "🎬 Video")
        self._tabs.addTab(self._audio_tab, "🎵 Audio")
        self._video_list = QListWidget()
        self._video_list.setFrameShape(QListWidget.Shape.NoFrame)
        vlay = QVBoxLayout(self._video_tab)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.addWidget(self._video_list)
        self._audio_list = QListWidget()
        self._audio_list.setFrameShape(QListWidget.Shape.NoFrame)
        alay = QVBoxLayout(self._audio_tab)
        alay.setContentsMargins(0, 0, 0, 0)
        alay.addWidget(self._audio_list)
        # Converting to MP3 means a lossy re-encode, so it is opt-in: the
        # default keeps whatever the source provides (m4a/opus/webm).
        self._mp3_check = QCheckBox("Convert to MP3 (re-encode)")
        self._mp3_check.setToolTip(
            "Off: keep the original audio stream (no quality loss).\n"
            "On: convert to MP3 with ffmpeg."
        )
        self._mp3_check.toggled.connect(
            lambda _checked: self._select_format(self._selected_id or "bestaudio/best",
                                                 self._result_data.get("audio_only", False))
        )
        alay.addWidget(self._mp3_check)
        root.addWidget(self._tabs, 1)

        # Answers "how big will the file be?" for whatever row is selected —
        # the per-row detail column can only hold a short string.
        self._summary_lbl = QLabel("Estimated download size: —")
        self._summary_lbl.setObjectName("muted")
        self._summary_lbl.setWordWrap(True)
        root.addWidget(self._summary_lbl)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("iconButton")
        cancel_btn.clicked.connect(self.reject)
        self._start_btn = QPushButton("⬇ Start Download")
        self._start_btn.setObjectName("primaryButton")
        self._start_btn.clicked.connect(self._on_accept)
        self._start_btn.setEnabled(False)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self._start_btn)
        root.addLayout(btn_row)

    def _populate_formats(self) -> None:
        """Parse formats into video and audio tabs.

        Storyboards are skipped (they are image sheets, not videos) and an
        audio-only source is opened on the Audio tab: a music URL used to open
        on an empty Video tab, so the dialog looked broken.
        """
        video_formats: list[FormatEntry] = []
        audio_formats: list[FormatEntry] = []

        # Build every entry first: the size of a video row depends on the
        # best audio track (it is merged into the same file), so the audio
        # side has to be known before any row is rendered.
        for fmt in self._formats:
            entry = FormatEntry(fmt)
            self._entries[entry.format_id] = entry
        self._audio_size = self._best_audio_size()

        seen_heights: set[int] = set()
        by_height: dict[int, FormatEntry] = {}
        unknown_height: list[FormatEntry] = []
        for entry in self._entries.values():
            if not entry.is_downloadable:
                continue
            if entry.is_video:
                if entry.height <= 0:
                    # No resolution to collapse on: keep every candidate, the
                    # way these rows behaved before one-per-height existed.
                    unknown_height.append(entry)
                    continue
                if entry.height in seen_heights:
                    continue
                seen_heights.add(entry.height)
                by_height[entry.height] = entry
            elif entry.audio_only:
                audio_formats.append(entry)

        # Several formats can share a resolution (an HLS muxed one and the
        # adaptive DASH ones).  Keep the candidate whose size is a real byte
        # count — it is both the number the dialog can show honestly and the
        # smaller download — instead of whichever the extractor listed first.
        video_formats = list(unknown_height)
        for height in by_height:
            hand_in = by_height[height]
            best = max((e for e in self._entries.values()
                        if e.is_downloadable and e.is_video and e.height == height),
                       key=row_preference, default=hand_in)
            video_formats.append(best)

        # Sort video by height desc, audio by bitrate desc
        video_formats.sort(key=lambda e: e.height, reverse=True)
        audio_formats.sort(key=lambda e: e.abr, reverse=True)

        # Populate video tab — "Best" option first, uses universal format string
        if video_formats:
            best = video_formats[0]
            self._add_format_item(self._video_list, best, is_default=True,
                                  format_id="bv*+ba/b",
                                  label_override=f"⚡ Best Quality ({best.height}p)")
            for entry in video_formats:
                fmt_str = f"{entry.format_id}+bestaudio/best"
                self._add_format_item(self._video_list, entry, format_id=fmt_str)
        else:
            self._tabs.setTabEnabled(0, False)
            self._add_empty_state(self._video_list, "No video stream in this link.")

        # Populate audio tab
        if audio_formats:
            best_audio = audio_formats[0]
            self._add_format_item(self._audio_list, best_audio, is_default=True,
                                  format_id="bestaudio/best",
                                  label_override="⚡ Best Audio",
                                  audio_only=True)
            for entry in audio_formats:
                self._add_format_item(self._audio_list, entry,
                                      format_id=f"{entry.format_id}/best",
                                      audio_only=True)
        elif self._metadata_has_audio():
            # Some player clients advertise no separate audio streams at all
            # (tv/web_safari, mweb, tv_simply).  yt-dlp can still extract the
            # audio, so offer that instead of an empty tab the user reads as
            # "there is nothing".
            placeholder = FormatEntry({
                "format_id": "bestaudio", "ext": self._source_ext(),
                "vcodec": "none", "acodec": "unknown",
                "abr": self._best_abr(), "resolution": "audio only",
            })
            self._add_format_item(self._audio_list, placeholder, is_default=True,
                                  format_id="bestaudio/best",
                                  label_override="⚡ Best Audio (from this link)",
                                  audio_only=True)
            self._add_empty_state(
                self._audio_list,
                "This link lists no separate audio streams — the best audio "
                "track will be extracted instead.",
            )
        else:
            self._tabs.setTabEnabled(1, False)
            self._add_empty_state(self._audio_list, "No audio stream in this link.")

        # Open on a tab that has something in it: an audio-only link used to
        # open on an empty "Video" tab, which reads as "there is nothing here".
        if not video_formats and self._metadata_has_audio():
            self._tabs.setCurrentIndex(1)

    def _best_audio_size(self) -> int:
        """Estimated bytes of the audio track merged into every video row.

        A video row downloads ``<format>+bestaudio/best``, so the file on disk
        is *video + audio*: showing only the video stream's bytes understated
        every row by 5–15% (and by far more for short clips).
        """
        sizes = [e.size_bytes(self._duration) for e in self._entries.values()
                 if e.audio_only]
        sizes = [s for s in sizes if s > 0]
        if sizes:
            return max(sizes)
        # No separate audio stream listed (some player clients are muxed-only):
        # fall back to the best advertised bitrate × duration.
        abr = self._best_abr()
        if abr > 0 and self._duration > 0:
            return int(abr * 1000.0 / 8.0 * self._duration)
        return 0

    def _total_size(self, entry: FormatEntry) -> tuple[int, bool, str]:
        """``(bytes, is_estimate, breakdown)`` for clicking *entry*'s row.

        ``is_estimate`` means the number is prefixed with ``≈`` — it is true
        whenever any part of the total came from an approximation.
        """
        base = entry.size_bytes(self._duration)
        if base <= 0:
            return 0, False, ""
        if entry.is_video and entry.acodec in ("none", ""):
            # Video-only stream: yt-dlp adds the best audio track to it.
            extra = self._audio_size
            if extra > 0:
                note = (f"video {'≈ ' if entry.size_approximate else ''}"
                        f"{_fmt_size(base)} + audio ≈ {_fmt_size(extra)}")
                return base + extra, True, note
            return base, entry.size_approximate, "video only, no audio stream listed"
        if entry.audio_only:
            return base, entry.size_approximate, "audio only"
        # Muxed stream (format 18/22 …): audio is already inside it.
        return base, entry.size_approximate, "video + audio in one stream"

    def _metadata_has_audio(self) -> bool:
        """True when the source carries audio at all (may be muxed only)."""
        if self._metadata.get("has_audio"):
            return True
        return any(_codec(f.get("acodec")) not in ("none", "")
                   for f in self._formats)

    def _best_abr(self) -> float:
        return max((FormatEntry(f).abr for f in self._formats), default=0)

    def _source_ext(self) -> str:
        """Container to show in the synthetic row: the best muxed/audio ext."""
        entries = [FormatEntry(f) for f in self._formats]
        audio_exts = [e.ext for e in entries if e.audio_only and e.ext]
        if audio_exts:
            return audio_exts[0]
        muxed = [e.ext for e in entries if e.is_video and e.ext]
        return muxed[0] if muxed else ""

    def _add_empty_state(self, lst: QListWidget, message: str) -> None:
        """Show *message* inside an otherwise empty list."""
        item = QListWidgetItem(lst)
        label = QLabel(message)
        label.setObjectName("muted")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        item.setSizeHint(label.sizeHint())
        lst.addItem(item)
        lst.setItemWidget(item, label)

    def _add_format_item(self, lst: QListWidget, entry: FormatEntry,
                         is_default: bool = False, format_id: str = "",
                         label_override: str = "", audio_only: bool = False) -> None:
        item = QListWidgetItem(lst)
        fid = format_id or entry.format_id
        total, is_est, note = self._total_size(entry)
        # Cache before selecting: _select_format reads it for the summary line
        # (and the default row is selected from right here).
        self._row_cache[fid] = (total, is_est, note)
        detail_override = ""
        tooltip = ""
        if total > 0:
            detail_override = (f"{entry.ext.upper()} · "
                               f"{'≈ ' if is_est else ''}{_fmt_size(total)}")
            tooltip = (f"Estimated download size: {'≈ ' if is_est else ''}"
                       f"{_fmt_size(total)}")
            if note:
                tooltip += f"\n({note})"
        widget = FormatItemWidget(entry, is_default, emit_id=fid,
                                  detail_override=detail_override, tooltip=tooltip)
        if label_override:
            for child in widget.findChildren(QLabel):
                if child.text().startswith("<b>"):
                    child.setText(f"<b>{label_override}</b>")
                    break
        ao = audio_only
        widget.selected.connect(lambda chosen_id: self._select_format(chosen_id, ao))
        item.setSizeHint(widget.sizeHint())
        lst.addItem(item)
        lst.setItemWidget(item, widget)
        if is_default and not self._selected_id:
            self._select_format(fid, audio_only)

    def _size_summary(self, format_id: str, audio_only: bool) -> str:
        """Human sentence for the summary label: how big is this download?"""
        cached = self._row_cache.get(format_id)
        if not cached:
            return "Estimated download size: unknown for this option."
        total, is_est, note = cached
        if total <= 0:
            return "Estimated download size: unknown for this option."
        text = (f"Estimated download size: {'≈ ' if is_est else ''}"
                f"{_fmt_size(total)}")
        extra = []
        if note:
            extra.append(note)
        if audio_only and self._mp3_check.isChecked():
            extra.append("MP3 is re-encoded, the final size may differ")
        if extra:
            text += f"   ({'; '.join(extra)})"
        return text

    def _select_format(self, format_id: str, audio_only: bool = False) -> None:
        self._selected_id = format_id
        self._start_btn.setEnabled(True)
        total, is_est, note = self._row_cache.get(format_id, (0, False, ""))
        self._result_data = {
            "format": format_id,
            "audio_only": audio_only,
            "audio_format": "bestaudio",
            "audio_format_ext": "",
            # Carried on the job so the download card can show the expected
            # size until yt-dlp reports the real one.
            "estimated_size": total,
            "size_estimated": is_est,
            "size_note": note,
        }
        if self._summary_lbl is not None:
            self._summary_lbl.setText(self._size_summary(format_id, audio_only))
        # Try to get quality label from the entry
        for entry in self._entries.values():
            if entry.format_id in format_id and entry.is_video:
                self._result_data["quality_label"] = f"{entry.height}p"
                break
        if audio_only:
            # Honour the row the user actually clicked instead of always
            # downloading "bestaudio" (picking 160 kbps used to silently give
            # you whatever was best).
            chosen = (format_id or "").split("/")[0].strip()
            if chosen and chosen != "bestaudio":
                self._result_data["audio_format"] = chosen
            entry = self._entries.get(chosen)
            if entry is not None:
                self._result_data["audio_format_label"] = entry.label
            self._result_data["audio_format_ext"] = (
                "mp3" if self._mp3_check.isChecked() else ""
            )
            self._result_data["quality_label"] = (
                "Audio · mp3" if self._mp3_check.isChecked() else "Audio · original"
            )
        elif "quality_label" not in self._result_data:
            self._result_data["quality_label"] = "Best"

    def _on_accept(self) -> None:
        if self._selected_id:
            self.accept()

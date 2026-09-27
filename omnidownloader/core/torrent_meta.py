"""Minimal BitTorrent metadata reader (bencode).

Needed because the torrent module's rich metadata path goes through libtorrent,
which is not always installed — the aria2c backend can still *fetch* a magnet's
metadata (``--bt-metadata-only --bt-save-metadata``) and save it as a
``<infohash>.torrent`` file.  Reading that file here gives the real name, total
size and file list without any C++ binding, which is what the UI, the job card
and selective-file download all need.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BENCODE_INT = b"i"
_BENCODE_LIST = b"l"
_BENCODE_DICT = b"d"
_BENCODE_END = b"e"


class BencodeError(ValueError):
    """Raised when a blob is not valid bencode."""


def decode(data: bytes, index: int = 0) -> tuple[Any, int]:
    """Decode one bencoded value.  Returns ``(value, next_index)``."""
    if index >= len(data):
        raise BencodeError("unexpected end of data")
    char = data[index:index + 1]

    if char == _BENCODE_INT:
        end = data.index(_BENCODE_END, index)
        try:
            return int(data[index + 1:end]), end + 1
        except ValueError as exc:
            raise BencodeError(f"bad integer at {index}") from exc

    if char == _BENCODE_LIST:
        index += 1
        items = []
        while data[index:index + 1] != _BENCODE_END:
            value, index = decode(data, index)
            items.append(value)
        return items, index + 1

    if char == _BENCODE_DICT:
        index += 1
        result: dict[bytes, Any] = {}
        while data[index:index + 1] != _BENCODE_END:
            key, index = decode(data, index)
            value, index = decode(data, index)
            result[key] = value
        return result, index + 1

    # Byte string: <length>:<bytes>
    colon = data.index(b":", index)
    try:
        length = int(data[index:colon])
    except ValueError as exc:
        raise BencodeError(f"bad string length at {index}") from exc
    start = colon + 1
    return data[start:start + length], start + length


@dataclass
class TorrentFile:
    """One entry of a torrent's file list."""

    path: str
    length: int


@dataclass
class TorrentMeta:
    """What the UI needs to know about a torrent."""

    name: str = ""
    total_size: int = 0
    files: list[TorrentFile] = field(default_factory=list)
    comment: str = ""

    @property
    def is_multi_file(self) -> bool:
        return len(self.files) > 1

    def as_dict(self) -> dict:
        return {
            "name": self.name or "Torrent",
            "total_size": self.total_size,
            "files": [{"path": f.path, "length": f.length} for f in self.files],
            "thumbnail": "",
            "comment": self.comment,
        }


def _decode_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def read_metadata(path: str | Path) -> TorrentMeta:
    """Parse a .torrent file into :class:`TorrentMeta`.

    Raises :class:`BencodeError` if the file is not a torrent (an HTML error
    page saved as one, for example).
    """
    blob = Path(path).read_bytes()
    if not blob.startswith(_BENCODE_DICT):
        raise BencodeError(f"{path} is not a torrent file (no bencode dict)")
    meta, _ = decode(blob)
    if not isinstance(meta, dict) or b"info" not in meta:
        raise BencodeError(f"{path} has no info dictionary")
    info = meta[b"info"]
    if not isinstance(info, dict):
        raise BencodeError(f"{path} has a malformed info dictionary")

    result = TorrentMeta(
        name=_decode_text(info.get(b"name.utf-8") or info.get(b"name", "")),
        comment=_decode_text(meta.get(b"comment", "")),
    )

    if b"length" in info:                      # single-file torrent
        result.total_size = int(info[b"length"])
        result.files = [TorrentFile(path=result.name, length=result.total_size)]
        return result

    files = info.get(b"files") or []           # multi-file torrent
    for entry in files:
        if not isinstance(entry, dict):
            continue
        length = int(entry.get(b"length", 0))
        parts = entry.get(b"path.utf-8") or entry.get(b"path") or []
        rel = "/".join(_decode_text(p) for p in parts)
        result.files.append(TorrentFile(path=rel, length=length))
        result.total_size += length
    if not result.name and result.files:
        result.name = result.files[0].path.split("/")[0]
    return result


def raw_info_span(data: bytes) -> bytes:
    """The exact byte span of a torrent's ``info`` value.

    BitTorrent's info-hash is the SHA-1 of these bytes *as they appear in the
    file*: decoding and re-encoding the dict would produce a different hash
    (dict key order, integer forms), so the raw span is extracted instead.
    """
    key = b"4:info"
    start = data.index(key) + len(key)
    depth = 0
    index = start
    while index < len(data):
        char = data[index:index + 1]
        if char in (b"d", b"l"):
            depth += 1
            index += 1
        elif char == b"e":
            depth -= 1
            index += 1
            if depth == 0:
                return data[start:index]
        elif char == b"i":
            index = data.index(b"e", index) + 1
        else:
            colon = data.index(b":", index)
            length = int(data[index:colon])
            index = colon + 1 + length
    raise BencodeError("unterminated info dictionary")


def infohash(path: str | Path) -> str:
    """The lowercase hex info-hash of a .torrent file ("" when unavailable)."""
    try:
        blob = Path(path).read_bytes()
        return hashlib.sha1(raw_info_span(blob)).hexdigest()
    except (OSError, ValueError, BencodeError) as exc:
        logger.debug("Could not compute the info-hash of %s: %s", path, exc)
        return ""


def infohash_from_magnet(magnet: str) -> str:
    """The info-hash carried by a magnet link ("" when absent)."""
    from urllib.parse import parse_qs, urlparse

    try:
        query = parse_qs(urlparse(magnet).query)
    except ValueError:
        return ""
    for xt in query.get("xt", []):
        if xt.lower().startswith("urn:btih:"):
            return xt[len("urn:btih:"):].strip().lower()
    return ""


def find_saved_metadata(directory: str | Path) -> Path | None:
    """Newest ``<40-hex>.torrent`` aria2c saved while resolving a magnet."""
    try:
        candidates = [
            p for p in Path(directory).glob("*.torrent")
            if len(p.stem) == 40 and all(c in "0123456789abcdefABCDEF" for c in p.stem)
        ]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)

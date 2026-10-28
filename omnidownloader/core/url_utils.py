"""URL tidying for user-supplied input.

People paste links from browsers, chat windows and text files.  On Windows in
particular the clipboard routinely carries a bare ``youtube.com/...`` (browsers
hide the scheme in the address bar) or a link wrapped in invisible characters,
and both used to reach the router as "no module can handle this URL".

:func:`normalize_url` repairs exactly those shapes and leaves everything else
alone, so a magnet link, a ``scrape:`` prefix or a local ``C:\\path\\a.torrent``
still arrives untouched.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Any explicit scheme, including non-download ones (``scrape:``, ``ftp:``).
#: Dots are excluded on purpose: ``example.com:8080/x`` is a bare host with a
#: port, not a scheme, and treating it as one skipped the https repair.
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+\-]*:")

#: ``host.tld`` / ``host.tld:port`` optionally followed by a path, query or
#: fragment — what a browser shows without the scheme.
_BARE_HOST_RE = re.compile(
    r"^(?:[A-Za-z0-9\-_]+\.)+[A-Za-z]{2,}(?::\d{1,5})?(?:[/?#].*)?$"
)

#: Characters that get pasted along with links and are never part of one
#: (zero-width spaces, BOM, non-breaking space).
_INVISIBLE = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00A0, 0x2028, 0x2029], None
)

#: Wrapping punctuation that ends up around a pasted link.
_WRAPPERS = "\"'`<> \t\r\n"

#: Sentence punctuation that can trail a link in prose.  ``)`` is deliberately
#: absent: it is meaningful in Wikipedia-style URLs.
_TRAILING = ".,;"

#: Schemes the app can act on (guarded in tests so a typo cannot silently
#: turn into a huge download).
KNOWN_SCHEMES = frozenset(
    {"http", "https", "magnet", "torrent", "rtmp", "rtsp", "mms", "scrape"}
)


def normalize_url(raw: str) -> str:
    """Return *raw* as a URL the router can handle.

    * strips whitespace, zero-width characters and wrapping quotes;
    * drops sentence punctuation glued on by a chat message;
    * restores a missing scheme (``youtu.be/x`` -> ``https://youtu.be/x``).

    Local files, magnet links and anything that already carries a scheme are
    returned unchanged (only cleaned of invisible characters).
    """
    text = (raw or "").translate(_INVISIBLE).strip().strip(_WRAPPERS).strip()
    if not text:
        return ""

    # A trailing "." or "," is prose punctuation; a trailing "." inside a URL
    # path is legal, but such a URL ends with an extension-like segment and is
    # left alone because the character before it is not a lone dot.
    while len(text) > 1 and text[-1] in _TRAILING:
        text = text[:-1]

    if text.startswith("//"):            # protocol-relative link
        return "https:" + text
    if _SCHEME_RE.match(text):
        return text                       # http(s)/magnet/scrape:/ftp: ... as-is
    if Path(text).exists():               # dropped local file, not a URL
        return text
    if _BARE_HOST_RE.match(text):
        return "https://" + text
    return text


def url_scheme(url: str) -> str:
    """Lower-case scheme of *url*, or ``""`` when there is none."""
    match = _SCHEME_RE.match(url or "")
    if not match:
        return ""
    return match.group(0)[:-1].lower()


def looks_like_web_url(url: str) -> bool:
    """True when *url* is http(s) — the one scheme every build understands."""
    return url_scheme(url) in {"http", "https"}

"""Self-diagnosis — report what actually works on this machine.

Run on the machine that misbehaves:

    python -m omnidownloader --diagnose
    python -m omnidownloader --diagnose --url "https://youtu.be/..." --download

Everything reported here is measured, not inferred: real binaries are executed,
real player clients are asked for real bytes, and failures print yt-dlp's own
error text.  The closing block is meant to be pasted into a bug report.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from omnidownloader.core import platform_utils
from omnidownloader.modules.media_extractor import (
    _YOUTUBE_CLIENT_CANDIDATES,
    MediaExtractor,
)
from omnidownloader.services.dependency_manager import DependencyManager

DEFAULT_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
_FINDINGS: list[str] = []


def _say(text: str = "") -> None:
    print(text, flush=True)


def _finding(text: str) -> None:
    _FINDINGS.append(text)
    _say(f"  ⚠ {text}")


def _run(argv: list[str], timeout: int = 60) -> tuple[int, str]:
    """Run a command, returning (returncode, combined output)."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, f"not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as exc:
        return 126, f"could not run {argv[0]}: {exc}"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


# ── sections ────────────────────────────────────────────────────

def check_environment() -> None:
    _say("── environment ─────────────────────────────────────────")
    _say(f"  platform    : {platform_utils.describe()}")
    _say(f"  python      : {sys.version.split()[0]}  ({sys.executable})")
    _say(f"  frozen exe  : {bool(getattr(sys, 'frozen', False))}")
    _say(f"  cwd         : {Path.cwd()}")
    _say(f"  data dir    : {platform_utils.user_data_dir()}")
    _say(f"  log file    : {platform_utils.logs_dir() / 'omnidownloader.log'}")
    if platform_utils.is_windows():  # pragma: no cover - Windows only
        _say("  (Windows: ffmpeg/yt-dlp must be .exe and findable — checked below)")
    if not platform_utils.has_pwrite():
        _say("  (no os.pwrite: positional writes use per-fd lseek — expected on Windows)")


def check_modules() -> None:
    _say("\n── download modules ────────────────────────────────────")
    try:
        from omnidownloader.services.plugin_loader import PluginLoader

        modules = PluginLoader().load_all()
    except Exception as exc:  # noqa: BLE001 - diagnosis must not crash
        _finding(f"module loading failed: {exc!r}")
        return
    if not modules:
        _finding("no download modules loaded — nothing can ever be routed")
        return
    for mod in modules:
        _say(f"  {mod.display_name():10s} {type(mod).__name__}")
    if not any(isinstance(m, MediaExtractor) for m in modules):
        _finding("MediaExtractor did not load — video URLs cannot be handled")


def check_binaries() -> DependencyManager:
    _say("\n── external binaries ───────────────────────────────────")
    deps = DependencyManager()

    ytdlp = deps.ytdlp_path
    _say(f"  yt-dlp path : {ytdlp}")
    _say(f"  yt-dlp file : on-disk={Path(ytdlp).is_file()} on-PATH={bool(shutil.which('yt-dlp'))}")
    rc, out = _run([ytdlp, "--version"], timeout=60)
    if rc == 0:
        _say(f"  yt-dlp ver  : {out.splitlines()[0] if out else '?'}")
    else:
        _finding(f"yt-dlp is NOT runnable ({out.splitlines()[0][:120] if out else 'no output'})")

    ffmpeg = deps.find_ffmpeg_binary()
    _say(f"  ffmpeg path : {ffmpeg or '(none found)'}")
    if ffmpeg:
        rc, out = _run([ffmpeg, "-version"], timeout=30)
        if rc == 0:
            _say(f"  ffmpeg ver  : {out.splitlines()[0][:60] if out else '?'}")
        else:
            _finding(f"ffmpeg exists but will not run: {out.splitlines()[0][:120]}")
    else:
        _finding(
            "ffmpeg missing — any download needing a video+audio merge will fail "
            "(that is every YouTube format above 360p)"
        )

    bogus = [p for p in deps._deps_dir.glob("*") if p.is_file() and not deps._is_real_binary(p)]
    for path in bogus:
        _finding(f"bogus dependency file (archive saved as a binary?): {path}")
    return deps


async def check_player_clients(url: str) -> None:
    _say("\n── YouTube player clients (real 1-format download per client) ──")
    if not MediaExtractor._is_youtube_url(url):
        _say(f"  skipped: {url} is not a YouTube URL")
        return
    with tempfile.TemporaryDirectory(prefix="omndiag-") as tmp:
        for client in _YOUTUBE_CLIENT_CANDIDATES:
            cmd = [
                shutil.which("yt-dlp") or "yt-dlp",
                "-f", "worst[ext=mp4]/worst",
                "--no-warnings", "--no-playlist",
                "-P", tmp, "-o", f"{client}.%(ext)s",
                "--extractor-args", f"youtube:player_client={client}",
                url,
            ]
            rc, out = _run(cmd, timeout=120)
            produced = list(Path(tmp).glob(f"{client}.*"))
            if rc == 0 and produced:
                _say(f"  {client:16s} OK      ({produced[0].stat().st_size // 1024} KiB)")
            else:
                first = next((line for line in out.splitlines() if "ERROR" in line), out[:120])
                _say(f"  {client:16s} FAILED  {first.strip()[:110]}")


async def check_url_routing(url: str) -> None:
    _say("\n── URL routing ─────────────────────────────────────────")
    try:
        from omnidownloader.services.plugin_loader import PluginLoader

        modules = PluginLoader().load_all()
    except Exception as exc:  # noqa: BLE001
        _finding(f"cannot test routing: {exc!r}")
        return
    handler = next((m for m in modules if m.can_handle(url)), None)
    if handler is None:
        _finding(f"no module claims {url} — the job would sit as 'unknown' forever")
    else:
        _say(f"  {url}  ->  {handler.display_name()} ({type(handler).__name__})")

    try:
        meta = await MediaExtractor().extract_metadata(url)
        _say(f"  metadata     : {meta.get('title')!r}")
        heights = sorted({f["height"] for f in meta.get("formats", []) if f["height"]})
        _say(f"  video heights: {heights}")
    except Exception as exc:  # noqa: BLE001
        _finding(f"metadata extraction failed: {exc}")


async def check_full_download(url: str) -> None:
    _say("\n── end-to-end download ─────────────────────────────────")
    from omnidownloader.core.models import DownloadJob

    with tempfile.TemporaryDirectory(prefix="omndiag-dl-") as tmp:
        job = DownloadJob(url=url, file_path=str(Path(tmp) / "out.mp4"))
        try:
            await MediaExtractor().start_download(job)
        except Exception as exc:  # noqa: BLE001
            _finding(f"download failed: {exc}")
            return
        path = Path(job.file_path)
        if path.is_file() and path.stat().st_size > 0:
            _say(f"  OK: {path.name}  ({path.stat().st_size // 1024} KiB)")
        else:
            _finding("download reported success but produced no file")


async def run(url: str, do_download: bool) -> int:
    # Quiet the module-loader chatter so the report reads as a report, but
    # keep warnings — the player-client retry warnings are exactly what we
    # want visible here.
    logging.getLogger("omnidownloader.services.plugin_loader").setLevel(logging.WARNING)
    _say("OmniDownloader diagnostics")
    _say("=" * 60)
    check_environment()
    check_modules()
    check_binaries()
    await check_player_clients(url)
    await check_url_routing(url)
    if do_download:
        await check_full_download(url)

    _say("\n" + "=" * 60)
    if _FINDINGS:
        _say(f"{len(_FINDINGS)} problem(s) found — PASTE THIS BLOCK IN YOUR MESSAGE:")
        for item in _FINDINGS:
            _say(f"  - {item}")
    else:
        _say("No problems detected — downloads should work on this machine.")
    _say("=" * 60)
    return 1 if _FINDINGS else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omnidownloader --diagnose")
    parser.add_argument("--url", default=DEFAULT_URL, help="URL to test against")
    parser.add_argument("--download", action="store_true",
                        help="also perform one full real download")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args.url, args.download))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

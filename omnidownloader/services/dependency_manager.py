"""Dependency Manager — auto-download and update ffmpeg and yt-dlp binaries."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Optional

import aiohttp

from omnidownloader.core import platform_utils

logger = logging.getLogger(__name__)

# Retry settings for transient DNS / network errors
_MAX_RETRIES = 3
_RETRY_BACKOFF = [2, 5, 10]  # seconds between attempts

#: yt-dlp release assets, per platform and CPU architecture.  An x86_64 binary
#: does not run on Apple silicon/Raspberry Pi arm64 machines, so the asset is
#: chosen from the real architecture rather than assumed.
_YTDLP_URLS = {
    ("Linux", "x86_64"): "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux",
    ("Linux", "arm64"): "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux_aarch64",
    ("Linux", "x86"): "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux_x86",
    ("Darwin", "x86_64"): "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos",
    ("Darwin", "arm64"): "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos",
    ("Windows", "x86_64"): "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
    ("Windows", "arm64"): "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
}
_YTDLP_FALLBACK = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"

#: aria2c is the BitTorrent backend.  Only Windows ships an official portable
#: build (a zip of a single aria2c.exe); Linux and macOS install it from the
#: package manager, which is what ``install_hint("aria2c")`` says.  Without a
#: Windows build the torrent/magnet module is dead on arrival on Windows, so the
#: installer fetches it like ffmpeg.
ARIA2_RELEASES_API = "https://api.github.com/repos/aria2/aria2/releases/latest"
#: Fallback when the GitHub API is rate-limited or unreachable.  Pinned on
#: purpose: asset names on aria2's releases are versioned, not "latest".
ARIA2_PINNED_URL = (
    "https://github.com/aria2/aria2/releases/download/"
    "release-1.37.0/aria2-1.37.0-win-64bit-build1.zip"
)
ARIA2_WIN_ASSET_RE = re.compile(r"win-64bit.*\.zip$", re.IGNORECASE)

#: ffmpeg archives, per platform.  Values are lists of ``(url, archive_kind,
#: binaries)``: macOS ships ffmpeg and ffprobe as *separate* zip files, Linux
#: and Windows ship both inside one archive.  The "getrelease" URLs redirect to
#: the current build, so they cannot go stale the way a pinned version does.
FFMPEG_URLS: dict[str, list[tuple[str, str, tuple[str, ...]]]] = {
    "Linux": [(
        "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
        "tar.xz",
        ("ffmpeg", "ffprobe"),
    )],
    "Linux-arm64": [(
        "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz",
        "tar.xz",
        ("ffmpeg", "ffprobe"),
    )],
    "Darwin": [
        ("https://evermeet.cx/ffmpeg/getrelease/zip", "zip", ("ffmpeg",)),
        ("https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip", "zip", ("ffprobe",)),
    ],
    "Windows": [(
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        "zip",
        ("ffmpeg", "ffprobe"),
    )],
}


class DependencyManager:
    """Ensures yt-dlp and ffmpeg are available locally."""

    def __init__(self, deps_dir: str = "") -> None:
        self._deps_dir = Path(deps_dir) if deps_dir else platform_utils.deps_dir()
        self._deps_dir.mkdir(parents=True, exist_ok=True)
        self._system = platform.system()

    @property
    def ytdlp_path(self) -> str:
        """Return path to yt-dlp binary."""
        system = self._system
        ext = ".exe" if system == "Windows" else ""
        local = self._deps_dir / f"yt-dlp{ext}"
        if local.exists():
            return str(local)
        # Fall back to system PATH
        found = shutil.which("yt-dlp")
        if found:
            return found
        return str(local)  # will fail later if not downloaded

    @property
    def ffmpeg_path(self) -> str:
        """Return a *usable* ffmpeg path, or ``""`` when none is available.

        Only a real binary counts — a leftover archive stored under the
        binary's name is rejected.
        """
        return self.find_ffmpeg_binary()

    async def ensure_all(self) -> dict[str, str]:
        """Download every managed tool that is missing.

        Returns a dict mapping ``"yt-dlp"`` / ``"ffmpeg"`` (and ``"aria2c"`` on
        Windows, where torrents have no other backend) to their final paths.
        Errors for individual tools are logged but do not prevent the other
        from being downloaded.
        """
        wanted = [("yt-dlp", self.ensure_ytdlp), ("ffmpeg", self.ensure_ffmpeg)]
        if self._system == platform_utils.WINDOWS:
            wanted.append(("aria2c", self.ensure_aria2c))

        paths: dict[str, str] = {}
        for name, coro in wanted:
            try:
                paths[name] = await coro()
            except Exception:
                logger.exception("Failed to auto-install %s", name)
        return paths

    async def ensure_ytdlp(self, force: bool = False) -> str:
        """Download yt-dlp if not already present. Returns the path.

        ``force=True`` re-downloads even when a valid binary exists — used by
        the auto-updater, since YouTube breaks old extractors regularly.
        """
        if not force:
            # A yt-dlp provided by pip/pacman/Homebrew is usually a small
            # launcher *script*, which _is_real_binary() rightly calls "not a
            # binary".  It is still perfectly usable, and the old code tried to
            # delete it — on a system install that raises PermissionError
            # (/usr/bin/yt-dlp) and left the app with no yt-dlp at all.
            system = shutil.which("yt-dlp")
            if system and self._runs(system):
                logger.info("Using the system yt-dlp at %s", system)
                return system

        existing = Path(self.ytdlp_path)
        # Validate, don't just check existence: a truncated download or an
        # error page saved as "yt-dlp" used to be trusted forever.
        if not force and existing.is_file() and self._is_real_binary(existing):
            logger.info("yt-dlp found at %s", self.ytdlp_path)
            return self.ytdlp_path
        if existing.exists() and existing.is_file() and not force:
            if self._owns(existing):
                logger.warning("Replacing invalid yt-dlp binary at %s", existing)
                existing.unlink(missing_ok=True)
            else:
                # Never delete a file the app did not put there (a system
                # yt-dlp that fails to run, for instance).  Downloading our own
                # copy into deps/ still fixes the app: ytdlp_path prefers it.
                logger.warning(
                    "Ignoring unusable yt-dlp at %s (not managed by the app) — "
                    "downloading a private copy instead", existing)

        url = self._ytdlp_url()
        logger.info("Downloading yt-dlp for %s %s…", self._system, platform_utils.arch())
        dest = self._deps_dir / self._binary_name("yt-dlp")
        await self._download_file(url, dest)
        platform_utils.make_executable(dest)
        if not self._is_real_binary(dest):
            raise OSError(
                f"Downloaded yt-dlp at {dest} is not a usable binary — "
                f"see {platform_utils.install_hint('yt-dlp')}"
            )
        logger.info("yt-dlp installed at %s", dest)
        return str(dest)

    def _ytdlp_url(self) -> str:
        """The yt-dlp asset matching this platform *and* CPU architecture."""
        return (
            _YTDLP_URLS.get((self._system, platform_utils.arch()))
            or _YTDLP_URLS.get((self._system, "x86_64"))
            or _YTDLP_FALLBACK
        )

    def _owns(self, path: Path) -> bool:
        """True when *path* is a file this app downloaded (inside deps/)."""
        try:
            path.resolve().relative_to(self._deps_dir.resolve())
            return True
        except (ValueError, OSError):
            return False

    def _runs(self, path: str) -> bool:
        """True when *path* actually executes and prints a version.

        A pip/pacman/Homebrew yt-dlp is a small launcher script, not a
        PyInstaller binary, so "is it an executable file" is the wrong question
        — running ``--version`` is the only reliable check (and it catches a
        half-written download too).
        """
        if not path or not Path(path).exists():
            return False
        try:
            proc = subprocess.run(
                [path, "--version"],
                capture_output=True, timeout=40,
                **platform_utils.subprocess_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0 and bool(proc.stdout.strip())

    def _binary_name(self, tool: str) -> str:
        return f"{tool}.exe" if self._system == platform_utils.WINDOWS else tool

    async def ensure_ffmpeg(self) -> str:
        """Make ffmpeg available, downloading + extracting it if necessary.

        The per-platform sources point at *archives* (tar.xz on Linux, zip on
        Windows/macOS), so each one must be unpacked and the real binary
        located inside it.  Writing an archive straight to ``deps/ffmpeg``
        produces a file that merely *looks* like ffmpeg, and yt-dlp then dies
        mid-download when it tries to run it for the video+audio merge.

        macOS needs two downloads (ffmpeg and ffprobe are separate archives
        there); the other platforms get both from one archive.
        """
        binary = self.find_ffmpeg_binary()
        if binary:
            logger.info("ffmpeg found at %s", binary)
            return binary

        sources = self._ffmpeg_sources()
        if not sources:
            raise OSError(
                f"Unsupported platform: {self._system} / {platform_utils.arch()}"
            )

        errors: list[str] = []
        for url, kind, binaries in sources:
            logger.info("Downloading ffmpeg (%s) for %s…", ", ".join(binaries), self._system)
            archive = self._deps_dir / f"ffmpeg-dl.{kind}"
            try:
                await self._download_file(url, archive)
                self._extract_ffmpeg(archive, binaries)
            except Exception as exc:  # noqa: BLE001
                # A missing ffprobe must not lose a working ffmpeg: report it
                # and keep going.
                errors.append(f"{url}: {exc}")
                logger.warning("ffmpeg component download failed (%s): %s", binaries, exc)
            finally:
                archive.unlink(missing_ok=True)

        binary = self.find_ffmpeg_binary()
        if not binary:
            raise OSError(
                "ffmpeg is unavailable and could not be downloaded — "
                + platform_utils.install_hint("ffmpeg")
                + (f" (errors: {'; '.join(errors)})" if errors else "")
            )
        logger.info("ffmpeg installed at %s", binary)
        return binary

    def _ffmpeg_sources(self) -> list[tuple[str, str, tuple[str, ...]]]:
        """Archive list for this platform (arm64 Linux has its own builds)."""
        if self._system == platform_utils.LINUX and platform_utils.arch() == "arm64":
            return FFMPEG_URLS.get("Linux-arm64", [])
        return FFMPEG_URLS.get(self._system, [])

    # ── ffmpeg discovery / extraction ───────────────────────────

    def _ffmpeg_names(self) -> tuple[str, ...]:
        return (self._binary_name("ffmpeg"),)

    @staticmethod
    def _is_real_binary(path: Path) -> bool:
        """Return *True* when *path* is an executable, not a stray archive.

        Guards against the historical bug where a downloaded ``.tar.xz`` /
        ``.zip`` was stored under the binary's name: archives start with a
        known magic header, real executables do not.
        """
        try:
            if not path.is_file() or path.stat().st_size < 1024:
                return False
            with open(path, "rb") as fh:
                head = fh.read(8)
        except OSError:
            return False
        archive_magics = (
            b"PK\x03\x04",           # zip (Windows/macOS ffmpeg)
            b"\xfd7zXZ\x00",         # xz (Linux ffmpeg)
            b"\x1f\x8b\x08",         # gzip
            b"BZh",                  # bzip2
            b"7z\xbc\xaf\x27\x1c",   # 7-zip
            b"Rar!",                 # rar
            b"ustar",                # tar (read from offset 257 below)
            b"<htm",                 # an error page saved as a file
            b"<!DO",
        )
        if head.startswith(archive_magics):
            return False
        # tar keeps its magic at offset 257
        try:
            with open(path, "rb") as fh:
                fh.seek(257)
                return not fh.read(5).startswith(b"ustar")
        except OSError:
            return True

    def find_ffmpeg_binary(self) -> str:
        """Return a usable ffmpeg path, or ``""`` when none is available.

        Checks (in order): an extracted binary anywhere under the deps dir,
        then ``PATH``.
        """
        for name in self._ffmpeg_names():
            direct = self._deps_dir / name
            if self._is_real_binary(direct):
                return str(direct)
            # Extracted archives keep their own directory layout, e.g.
            # ffmpeg-7.0-essentials_build/bin/ffmpeg.exe — search for it.
            if self._deps_dir.is_dir():
                for found in self._deps_dir.rglob(name):
                    if self._is_real_binary(found):
                        return str(found)
        found = shutil.which("ffmpeg")
        return found or ""

    def _extract_ffmpeg(self, archive: Path, binaries: tuple[str, ...] = ()) -> Path:
        """Unpack *archive* and return the path to the extracted ffmpeg binary.

        *binaries* is what this archive is expected to contain (macOS splits
        ffmpeg and ffprobe into separate downloads).  Only the requested
        binaries are required to appear — an ffprobe-only archive is a
        success, not a missing-ffmpeg failure.
        """
        wanted = set(self._ffmpeg_names()) | {
            name if name.endswith(".exe") else f"{name}.exe" for name in binaries
        } | {"ffprobe", "ffprobe.exe", "ffplay", "ffplay.exe"}
        if binaries:
            wanted |= {b.replace(".exe", "") for b in binaries}

        extracted_names: set[str] = set()
        if archive.suffix == ".zip":
            import zipfile

            with zipfile.ZipFile(archive) as zf:
                for member in zf.namelist():
                    name = Path(member).name
                    if name not in wanted or member.endswith("/"):
                        continue
                    # Flatten: write straight into the deps dir, never trust
                    # member paths (zip-slip).
                    target = self._deps_dir / name
                    with zf.open(member) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
                    platform_utils.make_executable(target)
                    extracted_names.add(name)
        else:
            import tarfile

            with tarfile.open(archive, "r:*") as tf:
                for member in tf.getmembers():
                    name = Path(member.name).name
                    if not member.isfile() or name not in wanted:
                        continue
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        continue
                    target = self._deps_dir / name
                    with extracted, open(target, "wb") as out:
                        shutil.copyfileobj(extracted, out)
                    platform_utils.make_executable(target)
                    extracted_names.add(name)

        if binaries:
            missing = [
                b for b in binaries
                if b not in extracted_names and f"{b}.exe" not in extracted_names
            ]
            if missing:
                raise RuntimeError(
                    f"{archive.name} did not contain {', '.join(missing)}"
                )
            return Path(self._deps_dir / binaries[0].replace(".exe", ""))

        found = self.find_ffmpeg_binary_local()
        if not found:
            raise RuntimeError(
                f"Downloaded ffmpeg archive did not contain a usable ffmpeg binary: {archive.name}"
            )
        return Path(found)

    def find_ffmpeg_binary_local(self) -> str:
        """Look only inside the deps dir (no PATH fallback)."""
        for name in self._ffmpeg_names():
            direct = self._deps_dir / name
            if self._is_real_binary(direct):
                return str(direct)
        return ""

    # ── aria2c (torrent backend) ────────────────────────────────

    @property
    def aria2c_path(self) -> str:
        """A usable aria2c path, or ``""``.

        Order: the managed copy in ``deps/`` (downloaded here), then whatever
        the system provides (PATH / ``OMNI_ARIA2C_PATH``).
        """
        name = self._binary_name("aria2c")
        direct = self._deps_dir / name
        if self._is_real_binary(direct):
            return str(direct)
        if self._deps_dir.is_dir():
            for found in self._deps_dir.rglob(name):
                if self._is_real_binary(found):
                    return str(found)
        return platform_utils.find_aria2c()

    async def ensure_aria2c(self) -> str:
        """Make aria2c available, downloading the portable Windows build.

        aria2c is the only torrent backend on Windows (there is no libtorrent
        wheel for it here), so without this the magnet/torrent module refuses
        every job.  Linux and macOS take it from the package manager — the
        error below carries the right command.
        """
        found = self.aria2c_path
        if found:
            logger.info("aria2c found at %s", found)
            return found

        if self._system != platform_utils.WINDOWS:
            raise OSError(
                "aria2c is not downloaded on this platform — "
                + platform_utils.install_hint("aria2c")
            )

        url = await self._aria2_asset_url()
        logger.info("Downloading aria2c for %s…", self._system)
        archive = self._deps_dir / "aria2-dl.zip"
        try:
            await self._download_file(url, archive)
            self._extract_named(archive, ("aria2c.exe",))
        finally:
            archive.unlink(missing_ok=True)

        found = self.aria2c_path
        if not found:
            raise OSError(
                f"The aria2c archive from {url} did not contain a usable "
                "aria2c.exe"
            )
        logger.info("aria2c installed at %s", found)
        return found

    async def _aria2_asset_url(self) -> str:
        """Newest Windows portable aria2c zip, or the pinned URL.

        GitHub's API is unauthenticated here and rate-limits freely, so a
        failure must not stop the install — the pinned release is still a
        working aria2c.
        """
        try:
            timeout = aiohttp.ClientTimeout(total=20, connect=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    ARIA2_RELEASES_API,
                    headers={"Accept": "application/vnd.github+json"},
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    data = json.loads(await resp.text())
            assets = data.get("assets") or []
            for asset in assets:
                name = str(asset.get("name", ""))
                if ARIA2_WIN_ASSET_RE.search(name):
                    return str(asset["browser_download_url"])
            raise RuntimeError("no win-64bit asset in the latest release")
        except Exception as exc:  # noqa: BLE001
            logger.info("Falling back to the pinned aria2c release (%s)", exc)
            return ARIA2_PINNED_URL

    def _extract_named(self, archive: Path, binaries: tuple[str, ...]) -> list[str]:
        """Unpack *binaries* out of *archive* into the deps dir (flattened).

        Zip-slip safe: only the base name is used as the target path, so a
        hostile ``../../x`` member cannot escape the deps directory.
        """
        wanted = set(binaries) | {
            b.replace(".exe", "") for b in binaries
        } | {
            f"{b}.exe" for b in binaries if not b.endswith(".exe")
        }
        extracted: list[str] = []

        def _write(reader, target: Path) -> None:
            with reader, open(target, "wb") as out:
                shutil.copyfileobj(reader, out)
            platform_utils.make_executable(target)
            extracted.append(target.name)

        if archive.suffix.lower() == ".zip":
            import zipfile

            with zipfile.ZipFile(archive) as zf:
                for member in zf.namelist():
                    name = Path(member).name
                    if member.endswith("/") or name not in wanted:
                        continue
                    _write(zf.open(member), self._deps_dir / name)
        else:
            import tarfile

            with tarfile.open(archive, "r:*") as tf:
                for member in tf.getmembers():
                    name = Path(member.name).name
                    if not member.isfile() or name not in wanted:
                        continue
                    stream = tf.extractfile(member)
                    if stream is not None:
                        _write(stream, self._deps_dir / name)

        missing = [
            b for b in binaries
            if b not in extracted and f"{b}.exe" not in extracted
        ]
        if missing:
            raise RuntimeError(
                f"{archive.name} did not contain {', '.join(missing)}"
            )
        return extracted

    def optional_status(self) -> dict[str, bool]:
        """Per-tool availability for tools that are not required on every OS."""
        return {"aria2c": bool(self.aria2c_path)}

    def _make_executable(self, path: Path) -> None:
        platform_utils.make_executable(path)

    async def _download_file(self, url: str, dest: Path) -> None:
        """Download a file with automatic retries on DNS / network errors."""
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                timeout = aiohttp.ClientTimeout(total=300, connect=30)
                connector = aiohttp.TCPConnector(
                    force_close=True,
                    enable_cleanup_closed=True,
                )
                async with aiohttp.ClientSession(
                    timeout=timeout, connector=connector,
                ) as session:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"Download failed: HTTP {resp.status}")
                        # Write to a temporary file first, then rename atomically
                        tmp = dest.with_suffix(dest.suffix + ".tmp")
                        try:
                            with open(tmp, "wb") as f:
                                async for chunk in resp.content.iter_chunked(256 * 1024):
                                    f.write(chunk)
                            tmp.replace(dest)
                        except BaseException:
                            tmp.unlink(missing_ok=True)
                            raise
                return  # success
            except (
                aiohttp.ClientError,
                OSError,
                RuntimeError,
                asyncio.TimeoutError,
            ) as exc:
                last_exc = exc
                wait = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                logger.warning(
                    "Download attempt %d/%d failed for %s: %s — retrying in %ds",
                    attempt + 1, _MAX_RETRIES, url, exc, wait,
                )
                await asyncio.sleep(wait)
        # All retries exhausted
        raise RuntimeError(
            f"Failed to download {url} after {_MAX_RETRIES} attempts: {last_exc}"
        )

    # ── yt-dlp updates ──────────────────────────────────────────

    async def latest_ytdlp_version(self) -> str:
        """Latest released yt-dlp version, or "" when it can't be determined."""
        url = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.debug("yt-dlp release lookup returned HTTP %s", resp.status)
                        return ""
                    data = await resp.json()
                    return str(data.get("tag_name", "")).lstrip("v")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.debug("Could not check the latest yt-dlp version: %s", exc)
            return ""

    def installed_ytdlp_version(self) -> str:
        """Version reported by the yt-dlp we would actually run."""
        import subprocess

        binary = self.ytdlp_path
        try:
            proc = platform_utils.run_quiet([binary, "--version"], timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("Could not read the yt-dlp version: %s", exc)
            return ""
        return proc.stdout.strip().splitlines()[0] if proc.returncode == 0 else ""

    async def update_ytdlp(self, force: bool = False) -> tuple[bool, str]:
        """Update yt-dlp if a newer release exists.

        Returns ``(updated, message)``.  Three cases, in order:

        * a private copy we downloaded (``~/.omnidownloader/deps``) — replace it;
        * a standalone yt-dlp binary — let it self-update with ``-U``;
        * a package-managed install (pip/apt/pacman) — report it, because
          silently running pip behind the user's back would be rude.
        """
        current = self.installed_ytdlp_version()
        latest = await self.latest_ytdlp_version()

        if latest and current and not force:
            try:
                from packaging.version import Version
                if Version(latest) <= Version(current):
                    logger.info("yt-dlp %s is already the latest release", current)
                    return False, f"yt-dlp {current} is up to date"
            except Exception:  # noqa: BLE001 - non-PEP440 version strings
                if latest == current:
                    return False, f"yt-dlp {current} is up to date"

        owned = Path(self.ytdlp_path) == self._deps_dir / self._binary_name("yt-dlp")
        if owned:
            path = await self.ensure_ytdlp(force=True)
            new_version = self.installed_ytdlp_version()
            logger.info("yt-dlp updated to %s at %s", new_version, path)
            return True, f"yt-dlp updated to {new_version}"

        # Try the binary's own updater (only the standalone builds support it).
        import subprocess

        binary = self.ytdlp_path
        try:
            proc = platform_utils.run_quiet([binary, "-U"], timeout=180)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("yt-dlp -U failed: %s", exc)
            return False, f"could not run yt-dlp -U: {exc}"

        output = (proc.stdout + proc.stderr).strip()
        if proc.returncode == 0 and ("Updated yt-dlp" in output or "yt-dlp is up to date" in output):
            logger.info("yt-dlp self-update: %s", output.splitlines()[-1] if output else "ok")
            return True, output.splitlines()[-1] if output else "yt-dlp updated"

        logger.info(
            "yt-dlp at %s is not self-updatable (likely pip/system managed): %s",
            binary, output.splitlines()[0] if output else "no output",
        )
        return False, (
            f"yt-dlp {current or '?'} is managed outside the app"
            + (f" — latest release is {latest}" if latest else "")
            + "; update it with your package manager or pip"
        )

    def ytdlp_available(self) -> bool:
        """True when *some* usable yt-dlp exists (managed or system).

        One definition, used by both ``ensure_ytdlp`` and ``check_all``: they
        used to disagree, so a failing install still reported "yt-dlp ok" and
        the launcher printed "All dependencies are ready" next to a FAILED line.
        """
        if self._is_real_binary(self._deps_dir / self._binary_name("yt-dlp")):
            return True
        system = shutil.which("yt-dlp")
        return bool(system and self._runs(system))

    def check_all(self) -> dict[str, bool]:
        return {
            "yt-dlp": self.ytdlp_available(),
            "ffmpeg": bool(self.find_ffmpeg_binary()),
        }

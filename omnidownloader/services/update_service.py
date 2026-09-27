"""Update Service — checks GitHub releases for new versions and downloads updates.

Uses the GitHub Releases API to compare the running version against the latest
tag, then downloads and optionally installs the update.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiohttp
from packaging.version import Version

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from omnidownloader.core import platform_utils

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com/repos/BlackAngelSk/my-dowloader"
_RELEASES_URL = f"{_GITHUB_API}/releases/latest"
_REPO_URL = "https://github.com/BlackAngelSk/my-dowloader"

#: Release assets are named per platform, but the naming is easy to get wrong
#: (a "linux" build on Windows would be downloaded and fail to run), so the
#: platform is also confirmed from the file name.
_ASSET_PLATFORM_HINTS = {
    platform_utils.WINDOWS: lambda n: "macos" not in n.lower() and "linux" not in n.lower(),
    platform_utils.MACOS: lambda n: "macos" in n.lower() or "darwin" in n.lower(),
    platform_utils.LINUX: lambda n: "linux" in n.lower() or "appimage" in n.lower(),
}


@dataclass
class UpdateInfo:
    """Describes an available update."""
    version: str
    tag: str
    html_url: str
    body: str = ""
    assets: list[dict] = field(default_factory=list)

    @property
    def version_obj(self) -> Version:
        return Version(self.version)

    @property
    def platform_asset(self) -> Optional[dict]:
        """The release asset built for *this* platform.

        The release carries one artifact per OS, so picking "the first .zip"
        (or a Windows installer on Linux) would hand the user a file their
        machine cannot run.
        """
        wanted = {
            platform_utils.WINDOWS: (".exe", ".msi", ".zip"),
            platform_utils.MACOS: (".dmg", ".pkg", ".zip"),
            platform_utils.LINUX: (".appimage", ".tar.gz", ".zip"),
        }.get(platform_utils.system(), (".zip",))

        assets = [a for a in self.assets if a.get("name")]
        for suffix in wanted:
            for asset in assets:
                name = asset["name"]
                if name.lower().endswith(suffix) and _ASSET_PLATFORM_HINTS.get(
                    platform_utils.system(), lambda _n: True
                )(name):
                    return asset
        return None

    @property
    def installer_asset(self) -> Optional[dict]:
        """Windows-style installer, when the release ships one."""
        for a in self.assets:
            name = a.get("name", "")
            if name.endswith(".exe") and "Setup" in name:
                return a
        return None

    @property
    def zip_asset(self) -> Optional[dict]:
        for a in self.assets:
            name = a.get("name", "")
            if name.endswith(".zip") and "OmniDownloader" in name:
                return a
        return None

    @property
    def download_url(self) -> str:
        asset = self.platform_asset or self.installer_asset or self.zip_asset
        if asset:
            return asset["browser_download_url"]
        return f"{_REPO_URL}/archive/refs/tags/{self.tag}.zip"


class UpdateChecker(QThread):
    """Background thread that checks for updates without blocking the UI."""
    update_found = pyqtSignal(object)
    check_failed = pyqtSignal(str)
    up_to_date = pyqtSignal()

    def __init__(self, current_version: str, proxy_url: str = "", parent=None):
        super().__init__(parent)
        self._current = current_version
        self._proxy_url = proxy_url

    def run(self):
        try:
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            info = loop.run_until_complete(self._check())
            loop.close()
            if info is None:
                self.up_to_date.emit()
            else:
                self.update_found.emit(info)
        except Exception as exc:
            logger.warning("Update check failed: %s", exc)
            self.check_failed.emit(str(exc))

    async def _check(self) -> Optional[UpdateInfo]:
        timeout = aiohttp.ClientTimeout(total=15)
        connector = None
        if self._proxy_url:
            try:
                from aiohttp_socks import ProxyConnector
                connector = ProxyConnector.from_url(self._proxy_url)
            except Exception:
                pass
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as sess:
            headers = {"Accept": "application/vnd.github+json"}
            async with sess.get(_RELEASES_URL, headers=headers) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    raise RuntimeError(f"GitHub API returned {resp.status}")
                data = await resp.json()

        tag = data.get("tag_name", "")
        version_str = tag[1:] if tag.startswith("v") else tag
        try:
            remote = Version(version_str)
            local = Version(self._current)
        except Exception:
            return None
        if remote <= local:
            return None
        return UpdateInfo(
            version=version_str, tag=tag,
            html_url=data.get("html_url", ""),
            body=data.get("body", ""),
            assets=data.get("assets", []),
        )


class UpdateDownloader(QThread):
    """Downloads an update asset in the background."""
    progress = pyqtSignal(int, str)   # percent, speed text
    finished = pyqtSignal(str)        # file path of downloaded file
    error = pyqtSignal(str)

    def __init__(self, url: str, dest_dir: str, proxy_url: str = "", parent=None):
        super().__init__(parent)
        self._url = url
        self._dest_dir = dest_dir
        self._proxy_url = proxy_url

    def run(self):
        try:
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            path = loop.run_until_complete(self._download())
            loop.close()
            self.finished.emit(path)
        except Exception as exc:
            logger.error("Update download failed: %s", exc)
            self.error.emit(str(exc))

    async def _download(self) -> str:
        timeout = aiohttp.ClientTimeout(total=300, sock_read=60)
        connector = None
        if self._proxy_url:
            try:
                from aiohttp_socks import ProxyConnector
                connector = ProxyConnector.from_url(self._proxy_url)
            except Exception:
                pass
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as sess:
            async with sess.get(self._url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Download failed: HTTP {resp.status}")
                total = int(resp.headers.get("Content-Length", 0))
                dest = Path(self._dest_dir)
                cd = resp.headers.get("Content-Disposition", "")
                if "filename=" in cd:
                    # Sanitise: a server-supplied "../../evil" filename used to
                    # be joined onto the destination directory verbatim.
                    fname = Path(cd.split("filename=")[-1].strip('" ')).name
                else:
                    fname = Path(self._url.split("/")[-1].split("?")[0]).name
                if not fname:
                    fname = "update.bin"
                filepath = dest / fname
                downloaded = 0
                with open(filepath, "wb") as f:
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            pct = int(downloaded * 100 / total)
                            speed = f"{downloaded / (1024*1024):.1f} / {total / (1024*1024):.1f} MB"
                            self.progress.emit(pct, speed)
        return str(filepath)


class UpdateService(QObject):
    """Central update coordinator — checks, downloads, and installs updates.

    Usage::

        svc = UpdateService(version, parent=window)
        svc.update_available.connect(my_dialog.show_update)
        svc.check_for_updates()
    """
    update_available = pyqtSignal(object)
    check_failed = pyqtSignal(str)
    up_to_date = pyqtSignal()
    download_progress = pyqtSignal(int, str)
    download_finished = pyqtSignal(str)

    def __init__(self, current_version: str, proxy_url: str = "", parent=None):
        super().__init__(parent)
        self._version = current_version
        self._proxy_url = proxy_url
        self._checker: Optional[UpdateChecker] = None
        self._downloader: Optional[UpdateDownloader] = None

    def check_for_updates(self) -> None:
        if self._checker and self._checker.isRunning():
            return
        self._checker = UpdateChecker(self._version, self._proxy_url, parent=self)
        self._checker.update_found.connect(self.update_available.emit)
        self._checker.check_failed.connect(self.check_failed.emit)
        self._checker.up_to_date.connect(self.up_to_date.emit)
        self._checker.start()

    def download_update(self, info: UpdateInfo) -> None:
        if self._downloader and self._downloader.isRunning():
            return
        dest = tempfile.mkdtemp(prefix="omnidownloader_update_")
        url = info.download_url
        logger.info("Downloading update %s from %s", info.version, url)
        self._downloader = UpdateDownloader(url, dest, self._proxy_url, parent=self)
        self._downloader.progress.connect(self.download_progress.emit)
        self._downloader.finished.connect(self.download_finished.emit)
        self._downloader.error.connect(lambda e: logger.error("Download error: %s", e))
        self._downloader.start()

    @staticmethod
    def install_update(file_path: str) -> bool:
        """Install a downloaded update, quitting the app first if needed.

        Per platform:

        * Windows: run the Inno Setup installer.  It cannot overwrite
          OmniDownloader.exe while it is running, and ``/NORESTART`` means it
          will not retry after a reboot, so the app must exit *before* the
          installer starts.
        * macOS: mount the .dmg and reveal it — replacing a running .app
          bundle from inside the app is not something the OS supports.
        * Linux: chmod +x and run an AppImage in place, or unpack a tarball/
          zip next to the app.  Never over site-packages.
        """
        path = Path(file_path)

        if platform_utils.is_windows() and path.suffix.lower() in (".exe", ".msi"):
            try:
                subprocess.Popen([str(path), "/SILENT", "/NORESTART"], shell=False,
                                 **platform_utils.subprocess_kwargs())
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to launch installer: %s", exc)
                return False
            UpdateService._quit_app()
            return True

        if path.suffix.lower() == ".dmg":  # pragma: no cover - macOS only
            platform_utils.open_path(path)
            logger.info("Mounted %s — drag OmniDownloader.app to Applications", path.name)
            UpdateService._quit_app()
            return True

        if path.suffix.lower() == ".appimage":  # pragma: no cover - Linux only
            target = Path(sys.executable) if getattr(sys, "frozen", False) else path
            try:
                if getattr(sys, "frozen", False):
                    shutil.copy2(path, target)
                platform_utils.make_executable(target)
            except OSError as exc:
                logger.error("Failed to install AppImage: %s", exc)
                return False
            logger.info("AppImage updated at %s — restarting", target)
            UpdateService._quit_app()
            return True

        if path.suffix.lower() in (".zip", ".tar.gz", ".tgz"):
            try:
                app_dir = UpdateService._install_root()
                if app_dir is None:
                    return False
                UpdateService._unpack_archive(path, app_dir)
                return True
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to extract update: %s", exc)
                return False
        logger.warning("Unsupported update file type: %s", path.suffix)
        return False

    @staticmethod
    def _install_root() -> Optional[Path]:
        """Where an unpacked update may be written, or None if unsafe."""
        app_dir = (Path(sys.executable).parent if getattr(sys, "frozen", False)
                   else Path(__file__).parent.parent.parent)
        if "site-packages" in str(app_dir):
            logger.error("Refusing to unpack an update over site-packages (%s)", app_dir)
            return None
        return app_dir

    @staticmethod
    def _unpack_archive(path: Path, app_dir: Path) -> None:
        """Extract a release archive into *app_dir*, flattening the top folder."""
        import tarfile
        import zipfile

        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                prefix = ""
                if names:
                    top = names[0].split("/")[0]
                    if all(n.startswith(top + "/") for n in names if "/" in n):
                        prefix = top + "/"
                for name in names:
                    if not name.startswith(prefix):
                        continue
                    target = app_dir / name[len(prefix):]
                    if name.endswith("/"):
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(name) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
        else:
            with tarfile.open(path, "r:*") as tf:
                members = tf.getmembers()
                prefix = ""
                if members:
                    top = members[0].name.split("/")[0]
                    if all(m.name.startswith(top + "/") for m in members if "/" in m.name):
                        prefix = top + "/"
                for member in members:
                    if not member.isfile() or not member.name.startswith(prefix):
                        continue
                    target = app_dir / member.name[len(prefix):]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        continue
                    with extracted, open(target, "wb") as dst:
                        shutil.copyfileobj(extracted, dst)

    @staticmethod
    def _quit_app() -> None:
        """Quit the Qt application if one is running (no-op otherwise)."""
        try:
            from PyQt6.QtWidgets import QApplication
            inst = QApplication.instance()
            if inst is not None:
                inst.quit()
        except ImportError:
            pass

    @staticmethod
    def restart_app() -> None:
        """Relaunch the application and exit the current process."""
        args = [sys.executable]
        if not getattr(sys, "frozen", False):
            # Only a source checkout needs "-m omnidownloader"; the frozen
            # bootloader does not implement -m, so passing it just started a
            # second copy of the app while the old one kept running.
            args += ["-m", "omnidownloader"]
        try:
            # Detached from this process: on Windows the new copy must not be
            # in the same job object as the exiting one.
            subprocess.Popen(args, cwd=None, **platform_utils.subprocess_kwargs())
        except OSError as exc:
            logger.error("Failed to relaunch: %s", exc)
            return
        UpdateService._quit_app()


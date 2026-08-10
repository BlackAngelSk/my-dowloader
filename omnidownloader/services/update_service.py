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

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com/repos/BlackAngelSk/my-dowloader"
_RELEASES_URL = f"{_GITHUB_API}/releases/latest"
_REPO_URL = "https://github.com/BlackAngelSk/my-dowloader"


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
    def installer_asset(self) -> Optional[dict]:
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
        if sys.platform == "win32" and self.installer_asset:
            return self.installer_asset["browser_download_url"]
        if self.zip_asset:
            return self.zip_asset["browser_download_url"]
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
        version_str = tag.lstrip("v")
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
                    fname = cd.split("filename=")[-1].strip('" ')
                else:
                    fname = self._url.split("/")[-1].split("?")[0]
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
        path = Path(file_path)
        if sys.platform == "win32" and path.suffix == ".exe":
            try:
                subprocess.Popen([str(path), "/SILENT", "/NORESTART"], shell=False)
                return True
            except Exception as exc:
                logger.error("Failed to launch installer: %s", exc)
                return False
        if path.suffix == ".zip":
            import zipfile
            try:
                app_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) \
                    else Path(__file__).parent.parent.parent
                with zipfile.ZipFile(path, 'r') as zf:
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
                return True
            except Exception as exc:
                logger.error("Failed to extract update: %s", exc)
                return False
        logger.warning("Unsupported update file type: %s", path.suffix)
        return False

    @staticmethod
    def restart_app() -> None:
        if sys.platform == "win32":
            exe = sys.executable
            subprocess.Popen([exe, "-m", "omnidownloader"])
        else:
            os.execv(sys.executable, [sys.executable, "-m", "omnidownloader"])


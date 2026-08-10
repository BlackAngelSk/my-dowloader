"""Image Scraper — Batch image downloader and gallery scraper.

Scrapes images from web pages, social media threads, and direct URL
lists with deduplication and batch naming.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse

import aiohttp

from omnidownloader.core.base_module import BaseDownloaderModule
from omnidownloader.core.disk_utils import ensure_directory
from omnidownloader.core.models import DownloadJob, DownloadState

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".tiff", ".avif"}
IMAGE_URL_RE = re.compile(
    r'https?://[^\s\'"<>]+\.(?:jpg|jpeg|png|gif|webp|bmp|svg|tiff|avif)', re.I
)
SRC_RE = re.compile(r'src=["\']([^"\']+\.(?:jpg|jpeg|png|gif|webp))["\']', re.I)


class ImageScraper(BaseDownloaderModule):
    MODULE_NAME = "image"

    def __init__(self, proxy_manager=None) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._cancel_flags: dict[str, bool] = {}
        self._proxy_manager = proxy_manager

    def can_handle(self, url: str) -> bool:
        # Force image scraping with scrape: prefix
        if url.startswith("scrape:"):
            return True
        parsed = urlparse(url)
        pl = parsed.path.lower()
        # Direct image links
        if any(pl.endswith(e) for e in IMAGE_EXTENSIONS):
            return True
        # Known gallery/image hosting sites
        host = (parsed.hostname or "").removeprefix("www.")
        return any(d in host for d in [
            "imgur.com", "flickr.com", "500px.com", "deviantart.com",
            "reddit.com", "postimg.cc", "pixiv.net", "artstation.com",
            "pinterest.com", "unsplash.com", "wallhaven.cc",
            "gelbooru.com", "danbooru.donmai.us", "safebooru.org",
            "konachan.com", "e621.net", "rule34.xxx",
            "nhentai.net", "exhentai.org", "e-hentai.org",
            "albums.google.com", "photos.google.com",
            "smugmug.com", "photobucket.com",
        ])

    async def extract_metadata(self, url: str) -> dict[str, Any]:
        if url.startswith("scrape:"):
            url = url[len("scrape:"):]
        parsed = urlparse(url)
        pl = parsed.path.lower()
        if any(pl.endswith(e) for e in IMAGE_EXTENSIONS):
            async with self._get_session().head(url) as resp:
                size = int(resp.headers.get("Content-Length", 0))
                return {"title": Path(parsed.path).name or "image",
                        "file_size": size,
                        "images": [{"url": url, "width": 0, "height": 0, "size": size}]}
        images = await self._scrape_page(url)
        return {"title": f"Images from {parsed.hostname}",
                "file_size": -1, "images": images}

    async def start_download(
        self, job: DownloadJob, progress_callback: Optional[Callable] = None,
    ) -> None:
        # Strip scrape: prefix
        if job.url.startswith("scrape:"):
            job.url = job.url[len("scrape:"):]
        job.state = DownloadState.DOWNLOADING
        self._cancel_flags[job.id] = False

        images = job.metadata.get("images", [])
        if not images:
            meta = await self.extract_metadata(job.url)
            images = meta.get("images", [])
        if not images:
            job.state = DownloadState.FAILED
            job.error_message = "No images found."
            return

        job.file_size = sum(i.get("size", 0) for i in images if i.get("size", 0) > 0)

        # Deduplicate
        seen: set[str] = set()
        unique = [img for img in images if img["url"] not in seen and not seen.add(img["url"])]

        output_dir = Path(job.file_path or Path.home() / "Downloads" / "OmniDownloader" / "Images")
        ensure_directory(str(output_dir / "placeholder"))
        session = self._get_session()
        prefix = job.metadata.get("batch_prefix", "image")
        success = 0

        for idx, img in enumerate(unique):
            if self._cancel_flags.get(job.id, False):
                raise asyncio.CancelledError()
            ext = Path(urlparse(img["url"]).path).suffix or ".jpg"
            filepath = output_dir / f"{prefix}_{idx + 1:04d}{ext}"
            try:
                async with session.get(img["url"]) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        filepath.write_bytes(data)
                        success += 1
                        job.mark_downloaded(len(data))
                        job.progress_percent = ((idx + 1) / len(unique)) * 100
                        if progress_callback:
                            progress_callback(job)
            except Exception as exc:
                logger.warning("Error downloading %s: %s", img["url"], exc)

        job.file_path = str(output_dir)
        logger.info("Image batch: %d/%d saved to %s", success, len(unique), output_dir)

    async def cancel(self, job: DownloadJob) -> None:
        self._cancel_flags[job.id] = True

    async def _scrape_page(self, url: str) -> list[dict[str, Any]]:
        session = self._get_session()
        async with session.get(url) as resp:
            if resp.status != 200:
                return []
            html = await resp.text(errors="replace")

        raw_urls = IMAGE_URL_RE.findall(html)
        for m in SRC_RE.finditer(html):
            full = urljoin(url, m.group(1))
            if full not in raw_urls:
                raw_urls.append(full)

        seen: set[str] = set()
        images = []
        for u in raw_urls:
            if u not in seen:
                seen.add(u)
                images.append({"url": u, "width": 0, "height": 0, "size": 0})
        logger.info("Scraped %d image URLs from %s", len(images), url)
        return images

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = None
            if self._proxy_manager and self._proxy_manager.enabled:
                connector = self._proxy_manager.get_aiohttp_connector()
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}
            kwargs: dict = {"timeout": aiohttp.ClientTimeout(total=30), "headers": headers}
            if connector:
                kwargs["connector"] = connector
            self._session = aiohttp.ClientSession(**kwargs)
        return self._session

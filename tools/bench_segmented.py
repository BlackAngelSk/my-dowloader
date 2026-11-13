#!/usr/bin/env python3
"""Benchmark: does the connection pool actually beat one connection?

Real speed-test servers rate-limit (Hetzner disconnects, OVH answers 429), so a
public-URL benchmark says more about the host than about the engine.  This one
uses a local server that caps *each connection* at a fixed rate — which is what
a CDN or a per-IP throttle does to you in the real world — and measures the
same file with one connection and with a pool.

The interesting part is not just "more connections is faster": the piece grid
is four times the pool, so the last pieces are shared instead of finishing one
after another.  A pool that finishes each piece strictly in order would be
noticeably slower here.

Usage: .venv/bin/python tools/bench_segmented.py [--mb 24] [--rate 2]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web  # noqa: E402

from omnidownloader.core import ram_buffer  # noqa: E402
from omnidownloader.core.models import DownloadJob  # noqa: E402
from omnidownloader.modules import http_downloader  # noqa: E402
from omnidownloader.modules.http_downloader import HTTPDownloader  # noqa: E402

PAYLOAD = b""
RATE = 2 * 1024 * 1024          # bytes/s per connection
BLOCK = 64 * 1024


def digest_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def handle(request):
    """Serve a range at RATE bytes/s — the per-connection cap under test."""
    size = len(PAYLOAD)
    rng = request.headers.get("Range")
    if rng:
        spec = rng.partition("=")[2]
        start_s, _, end_s = spec.partition("-")
        start = int(start_s or 0)
        end = int(end_s) if end_s else size - 1
        end = min(end, size - 1)
        status = 206
        headers = {"Content-Range": f"bytes {start}-{end}/{size}",
                   "Accept-Ranges": "bytes"}
    else:
        start, end, status, headers = 0, size - 1, 200, {"Accept-Ranges": "bytes"}

    body = PAYLOAD[start:end + 1]
    resp = web.StreamResponse(status=status, headers={
        **headers, "Content-Length": str(len(body)),
        "Content-Type": "application/octet-stream",
    })
    await resp.prepare(request)
    # A fixed delay per block makes the cap independent of how the OS buffers.
    delay = BLOCK / RATE
    for offset in range(0, len(body), BLOCK):
        if request.transport is None or request.transport.is_closing():
            break
        try:
            await resp.write(body[offset:offset + BLOCK])
        except (ConnectionResetError, RuntimeError):
            break
        await asyncio.sleep(delay)
    try:
        await resp.write_eof()
    except (ConnectionResetError, RuntimeError):
        pass
    return resp


async def start_server():
    app = web.Application()
    app.router.add_get("/big", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/big"


async def run_once(url: str, path: Path, connections: int) -> tuple[float, int, str, int]:
    """Download *url* with a forced connection count; return (seconds, size, digest, pieces)."""
    original = ram_buffer.DynamicBufferSizer.calculate_threads.__func__
    ram_buffer.DynamicBufferSizer.calculate_threads = classmethod(
        lambda cls, size: connections)
    http_downloader.DynamicBufferSizer.calculate_threads = \
        ram_buffer.DynamicBufferSizer.calculate_threads
    dl = HTTPDownloader()
    try:
        started = time.monotonic()
        job = DownloadJob(url=url, file_path=str(path))
        await dl.start_download(job)
        elapsed = time.monotonic() - started
        return elapsed, job.file_size, digest_of(path), len(job.segments)
    finally:
        await dl.close()
        ram_buffer.DynamicBufferSizer.calculate_threads = classmethod(original)
        http_downloader.DynamicBufferSizer.calculate_threads = \
            ram_buffer.DynamicBufferSizer.calculate_threads


async def main() -> int:
    global PAYLOAD
    parser = argparse.ArgumentParser()
    parser.add_argument("--mb", type=int, default=24)
    parser.add_argument("--rate", type=float, default=2.0, help="MB/s per connection")
    args = parser.parse_args()

    RATE = int(args.rate * 1024 * 1024)
    PAYLOAD = bytes(range(256)) * ((args.mb * 1024 * 1024) // 256)
    size_mb = len(PAYLOAD) / 1e6
    print(f"file {size_mb:.0f} MB, cap {args.rate:.1f} MB/s per connection")
    print(f"theoretical: one connection ≈ {len(PAYLOAD) / RATE:.1f}s\n")

    tmp = Path(tempfile.mkdtemp(prefix="bench-segmented-"))
    runner, url = await start_server()
    try:
        t1, size1, d1, pieces1 = await run_once(url, tmp / "one.bin", 1)
        t8, size8, d8, pieces8 = await run_once(url, tmp / "pool.bin", 8)
    finally:
        await runner.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"1 connection : {t1:6.2f}s  {len(PAYLOAD) / t1 / 1e6:5.2f} MB/s  "
          f"{size1 / 1e6:.0f} MB  pieces={pieces1}")
    print(f"pool of 8    : {t8:6.2f}s  {len(PAYLOAD) / t8 / 1e6:5.2f} MB/s  "
          f"{size8 / 1e6:.0f} MB  pieces={pieces8}")
    print(f"speed-up     : {t1 / t8:.2f}x")

    ok = True
    if d1 != d8 or size1 != size8:
        print("FAIL: the pool produced different bytes than the single connection")
        ok = False
    if pieces8 <= 1:
        print("FAIL: the pool never split the file into pieces")
        ok = False
    if t1 / t8 < 2:
        print("FAIL: the pool was not meaningfully faster than one connection")
        ok = False
    print("\n" + ("PASS: byte-identical and faster" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

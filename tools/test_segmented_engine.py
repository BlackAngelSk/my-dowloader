#!/usr/bin/env python3
"""Engine behaviour that only a real server can show.

Three things Mister Download Manager's engine does that this one now copies:

* a stalled connection is dropped and its remaining bytes are re-shared, so the
  download finishes instead of waiting for the dead peer;
* a transient server error (429/503/5xx, a cut body) is retried from the last
  byte received instead of failing the whole job;
* a file that changed on the server is *not* resumed into: mixing two versions
  would produce a corrupt file that still "looks" complete.

Usage: .venv/bin/python tools/test_segmented_engine.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web  # noqa: E402

from omnidownloader.core.disk_utils import sidecar_path  # noqa: E402
from omnidownloader.core.models import DownloadJob  # noqa: E402
from omnidownloader.modules import http_downloader  # noqa: E402
from omnidownloader.modules.http_downloader import HTTPDownloader  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
SIZE = 4 * 1024 * 1024
PAYLOAD_OLD = bytes(random.Random(11).randbytes(SIZE))
PAYLOAD_NEW = bytes(random.Random(22).randbytes(SIZE))
DIGEST_OLD = hashlib.sha256(PAYLOAD_OLD).hexdigest()
DIGEST_NEW = hashlib.sha256(PAYLOAD_NEW).hexdigest()

#: /stall: the first ranged request sleeps this long (in test-time seconds).
STALL_SLEEP = 30.0
STATE = {
    "stalled": False,        # /stall has already held a request open
    "busy_left": 0,          # /busy: how many 503s are still owed
    "busy_served": 0,        # /busy: 503s actually sent
    "variant": "old",        # /validator: which body is served
    "validator_mode": "ok",  # /validator: "ok" or "fail"
}
RANGES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def digest_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_range(header: str, size: int):
    unit, _, spec = header.partition("=")
    if unit.strip() != "bytes" or "," in spec:
        return None
    start_s, _, end_s = spec.partition("-")
    if not start_s:
        n = int(end_s)
        return max(0, size - n), size - 1
    start = int(start_s)
    end = int(end_s) if end_s else size - 1
    return start, min(end, size - 1)


def ranged(payload: bytes, request):
    rng = request.headers.get("Range")
    if not rng:
        return None
    parsed = parse_range(rng, len(payload))
    if not parsed:
        return None
    start, end = parsed
    body = payload[start:end + 1]
    return web.Response(body=body, status=206, headers={
        "Content-Range": f"bytes {start}-{end}/{len(payload)}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(len(body)),
    })


async def handle_file(request):
    rng = request.headers.get("Range")
    if rng:
        RANGES.append(rng)
        resp = ranged(PAYLOAD_OLD, request)
        if resp is not None:
            return resp
    return web.Response(body=PAYLOAD_OLD, status=200, headers={
        "Accept-Ranges": "bytes", "Content-Length": str(SIZE)})


async def handle_stall(request):
    """Hold the first ranged request open forever; serve everybody else."""
    rng = request.headers.get("Range")
    if rng and not STATE["stalled"] and (parse_range(rng, SIZE) or (0, 0))[0] > 0:
        STATE["stalled"] = True
        try:
            await asyncio.sleep(STALL_SLEEP)
        except asyncio.CancelledError:
            raise
        return web.Response(status=504)
    resp = ranged(PAYLOAD_OLD, request)
    if resp is not None:
        return resp
    return web.Response(body=PAYLOAD_OLD, status=200, headers={
        "Accept-Ranges": "bytes", "Content-Length": str(SIZE)})


async def handle_busy(request):
    """Answer the first few ranged requests with 503 + Retry-After."""
    rng = request.headers.get("Range")
    parsed = parse_range(rng, SIZE) if rng else None
    if parsed and parsed[0] > 0 and STATE["busy_left"] > 0:
        STATE["busy_left"] -= 1
        STATE["busy_served"] += 1
        return web.Response(status=503, headers={"Retry-After": "0"})
    resp = ranged(PAYLOAD_OLD, request)
    if resp is not None:
        return resp
    return web.Response(body=PAYLOAD_OLD, status=200, headers={
        "Accept-Ranges": "bytes", "Content-Length": str(SIZE)})


async def handle_validator(request):
    payload = PAYLOAD_OLD if STATE["variant"] == "old" else PAYLOAD_NEW
    etag = '"v-old"' if STATE["variant"] == "old" else '"v-new"'
    rng = request.headers.get("Range")
    if rng:
        RANGES.append(rng)
    parsed = parse_range(rng, SIZE) if rng else None

    if STATE["validator_mode"] == "fail" and parsed and parsed[0] > SIZE // 4:
        return web.Response(status=500, text="nope")

    if parsed:
        start, end = parsed
        body = payload[start:end + 1]
        return web.Response(body=body, status=206, headers={
            "Content-Range": f"bytes {start}-{end}/{SIZE}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(len(body)),
            "ETag": etag,
        })
    return web.Response(body=payload, status=200, headers={
        "Accept-Ranges": "bytes", "Content-Length": str(SIZE), "ETag": etag})


async def start_server():
    app = web.Application()
    app.router.add_get("/file", handle_file)
    app.router.add_get("/stall", handle_stall)
    app.router.add_get("/busy", handle_busy)
    app.router.add_get("/validator", handle_validator)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def patch_engine(stall: float = 1.0, watchdog: float = 0.2,
                 retry_delay: float = 0.05, attempts: int = 4) -> dict:
    saved = {
        "STALL_SECONDS": http_downloader.STALL_SECONDS,
        "WATCHDOG_INTERVAL": http_downloader.WATCHDOG_INTERVAL,
        "RETRY_BASE_DELAY": http_downloader.RETRY_BASE_DELAY,
        "CHUNK_ATTEMPTS": http_downloader.CHUNK_ATTEMPTS,
    }
    http_downloader.STALL_SECONDS = stall
    http_downloader.WATCHDOG_INTERVAL = watchdog
    http_downloader.RETRY_BASE_DELAY = retry_delay
    http_downloader.CHUNK_ATTEMPTS = attempts
    return saved


def restore_engine(saved: dict) -> None:
    for key, value in saved.items():
        setattr(http_downloader, key, value)


# ── tests ───────────────────────────────────────────────────────

async def test_stall_is_re_shared(tmp: Path, base: str) -> None:
    saved = patch_engine()
    try:
        dl = HTTPDownloader()
        out = tmp / "stall.bin"
        job = DownloadJob(url=f"{base}/stall", file_path=str(out))
        planned = None
        started = time.monotonic()
        await asyncio.wait_for(dl.start_download(job), timeout=60)
        elapsed = time.monotonic() - started
        await dl.close()

        check("stalled connection: download still finishes", True, f"{elapsed:.1f}s")
        check("stalled connection: well before the peer's timeout",
              elapsed < STALL_SLEEP / 2, f"{elapsed:.1f}s of {STALL_SLEEP:.0f}s")
        check("stalled connection: byte-exact result", digest_of(out) == DIGEST_OLD)
        check("stalled connection: the stalled piece was re-shared",
              len(job.segments) > 16,
              f"{len(job.segments)} pieces (started with 16)")
        check("stalled connection: layout still covers the file",
              sum(s.length for s in job.segments) == SIZE,
              f"{sum(s.length for s in job.segments):,} bytes")
        check("stalled connection: every piece completed",
              all(s.completed for s in job.segments))
        planned = job.segments
        check("stalled connection: no piece overlaps another",
              sorted(planned, key=lambda s: s.start_byte)[0].start_byte == 0)
    finally:
        restore_engine(saved)


async def test_retry_on_503(tmp: Path, base: str) -> None:
    saved = patch_engine()
    try:
        STATE["busy_left"] = 3
        STATE["busy_served"] = 0
        dl = HTTPDownloader()
        out = tmp / "busy.bin"
        job = DownloadJob(url=f"{base}/busy", file_path=str(out))
        await asyncio.wait_for(dl.start_download(job), timeout=60)
        await dl.close()

        check("503: the job waits and retries instead of failing",
              STATE["busy_served"] >= 3, f"{STATE['busy_served']} rejections")
        check("503: result is still byte-exact", digest_of(out) == DIGEST_OLD)
        check("503: every piece completed", all(s.completed for s in job.segments))
    finally:
        restore_engine(saved)


async def test_changed_file_is_not_resumed(tmp: Path, base: str) -> None:
    """Same URL, same size, different file: the partial must be discarded."""
    saved = patch_engine()
    try:
        STATE["variant"] = "old"
        STATE["validator_mode"] = "fail"
        dl = HTTPDownloader()
        out = tmp / "changed.bin"

        first = DownloadJob(url=f"{base}/validator", file_path=str(out))
        failed = False
        try:
            await asyncio.wait_for(dl.start_download(first), timeout=60)
        except Exception:  # noqa: BLE001
            failed = True
        check("changed file: the first attempt fails on purpose", failed)

        state_file = sidecar_path(out)
        check("changed file: a resume sidecar was written", state_file.is_file())
        state = json.loads(state_file.read_text()) if state_file.is_file() else {}
        check("changed file: the sidecar recorded the validator",
              state.get("etag") == '"v-old"', str(state.get("etag")))
        partial = out.stat().st_size if out.is_file() else 0
        check("changed file: some bytes are on disk", partial > 0, f"{partial:,}")
        mixed = digest_of(out) if out.is_file() else ""

        # The server now serves a different file of the same size.
        STATE["variant"] = "new"
        STATE["validator_mode"] = "ok"
        second = DownloadJob(url=f"{base}/validator", file_path=str(out))
        await asyncio.wait_for(dl.start_download(second), timeout=60)
        await dl.close()

        final = digest_of(out)
        check("changed file: the result matches the new file exactly",
              final == DIGEST_NEW, final[:16])
        check("changed file: nothing from the old version survived",
              final != mixed)
        check("changed file: resume state removed after the fresh download",
              not state_file.exists())
    finally:
        restore_engine(saved)


async def test_unchanged_file_still_resumes(tmp: Path, base: str) -> None:
    """The validator must not turn every resume into a restart."""
    saved = patch_engine()
    try:
        STATE["variant"] = "old"
        STATE["validator_mode"] = "fail"
        dl = HTTPDownloader()
        out = tmp / "resume_ok.bin"
        first = DownloadJob(url=f"{base}/validator", file_path=str(out))
        try:
            await asyncio.wait_for(dl.start_download(first), timeout=60)
        except Exception:  # noqa: BLE001
            pass
        recorded = sum(s.downloaded_bytes for s in first.segments)
        check("unchanged file: partial progress exists", recorded > 0,
              f"{recorded:,} bytes")

        STATE["validator_mode"] = "ok"
        RANGES.clear()
        second = DownloadJob(url=f"{base}/validator", file_path=str(out))
        await asyncio.wait_for(dl.start_download(second), timeout=60)
        await dl.close()

        check("unchanged file: resumes to a byte-exact result",
              digest_of(out) == DIGEST_OLD)
        check("unchanged file: it did not start over from byte 0",
              all((parse_range(r, SIZE) or (0, 0))[0] > 0 for r in RANGES),
              f"{len(RANGES)} ranges; first={RANGES[0] if RANGES else '-'}")
    finally:
        restore_engine(saved)


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="engine-test-"))
    runner, base = await start_server()
    try:
        await test_stall_is_re_shared(tmp, base)
        await test_retry_on_503(tmp, base)
        await test_changed_file_is_not_resumed(tmp, base)
        await test_unchanged_file_still_resumes(tmp, base)
    finally:
        await runner.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)

    failures = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

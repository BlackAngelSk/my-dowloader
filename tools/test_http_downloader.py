#!/usr/bin/env python3
"""Byte-exactness tests for HTTPDownloader's segmented (multi-connection) path.

Serves files from an in-process aiohttp server with three behaviours:
  /file    — honours Range, 206 + Content-Range
  /norange — ignores Range, always 200 with the whole body
  /short   — honours Range but sends a truncated body

The old code wrote every segment through one shared append-only ring buffer,
so any multi-segment download produced a scrambled file; these tests compare
the result byte-for-byte against the source.

Usage: .venv/bin/python tools/test_http_downloader.py
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web  # noqa: E402

from omnidownloader.core.bandwidth_limiter import BandwidthManager, TokenBucket  # noqa: E402
from omnidownloader.core.disk_utils import sidecar_path  # noqa: E402
from omnidownloader.core.models import DownloadJob, DownloadState  # noqa: E402
from omnidownloader.core.streaming_buffer import StreamingBuffer  # noqa: E402
from omnidownloader.modules.http_downloader import HTTPDownloader  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
SIZE = 3 * 1024 * 1024 + 12345     # forces several segments, not power-of-two
PAYLOAD = bytes(random.Random(1234).randbytes(SIZE))
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def digest_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── server ──────────────────────────────────────────────────────

def parse_range(header: str, size: int):
    unit, _, spec = header.partition("=")
    if unit.strip() != "bytes" or "," in spec:
        return None
    start_s, _, end_s = spec.partition("-")
    if not start_s:                       # suffix range: bytes=-N
        n = int(end_s)
        return max(0, size - n), size - 1
    start = int(start_s)
    end = int(end_s) if end_s else size - 1
    return start, min(end, size - 1)


#: Toggled by the resume test: when True, range responses are truncated.
SERVER_MODE = {"truncate": False}
#: Range headers observed by the file handler (used to prove resumption).
RANGES: list[str] = []


async def handle_file(request):
    rng = request.headers.get("Range")
    if rng:
        RANGES.append(rng)
        parsed = parse_range(rng, SIZE)
        if parsed:
            start, end = parsed
            body = PAYLOAD[start:end + 1]
            return web.Response(
                body=body, status=206,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{SIZE}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(len(body)),
                },
            )
    return web.Response(body=PAYLOAD, status=200,
                        headers={"Accept-Ranges": "bytes",
                                 "Content-Length": str(SIZE)})


async def handle_flaky(request):
    """Healthy, unless SERVER_MODE says to truncate range responses."""
    if SERVER_MODE["truncate"]:
        return await handle_short(request)
    return await handle_file(request)


async def handle_norange(request):
    """Ignores Range entirely — the server that broke the old code."""
    return web.Response(body=PAYLOAD, status=200,
                        headers={"Content-Length": str(SIZE)})


async def handle_short(request):
    """Advertise the true size, then truncate the body mid-transfer.

    The probe must see a healthy, range-capable file (3 MiB, ``Accept-Ranges:
    bytes``) while the actual range request dies half-way — that is the case
    where a downloader can silently ship a corrupt file.
    """
    rng = request.headers.get("Range")
    parsed = parse_range(rng, SIZE) if rng else None
    if not parsed:
        return web.Response(body=PAYLOAD, status=200,
                            headers={"Accept-Ranges": "bytes",
                                     "Content-Length": str(SIZE)})

    start, end = parsed
    expected = end - start + 1
    resp = web.StreamResponse(status=206, headers={
        "Content-Range": f"bytes {start}-{end}/{SIZE}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(expected),
    })
    await resp.prepare(request)
    # Promise `expected` bytes, deliver half, then vanish.
    await resp.write(PAYLOAD[start:start + max(1, expected // 2)])
    await resp.write_eof()
    return resp


async def handle_slow(request):
    """Dribble the body out slowly so pause/cancel tests can interrupt it."""
    rng = request.headers.get("Range")
    parsed = parse_range(rng, SIZE) if rng else None
    if parsed:
        start, end = parsed
        status = 206
        extra = {"Content-Range": f"bytes {start}-{end}/{SIZE}"}
    else:
        start, end = 0, SIZE - 1
        status = 200
        extra = {}
    body = PAYLOAD[start:end + 1]
    resp = web.StreamResponse(status=status, headers={
        "Accept-Ranges": "bytes",
        "Content-Length": str(len(body)),
        **extra,
    })
    await resp.prepare(request)
    step = 64 * 1024
    for i in range(0, len(body), step):
        await resp.write(body[i:i + step])
        await asyncio.sleep(0.05)
    await resp.write_eof()
    return resp


async def start_server():
    app = web.Application()
    app.router.add_get("/file", handle_file)
    app.router.add_get("/norange", handle_norange)
    app.router.add_get("/short", handle_short)
    app.router.add_get("/slow", handle_slow)
    app.router.add_get("/flaky", handle_flaky)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


# ── tests ───────────────────────────────────────────────────────

async def test_segmented(tmp: Path, base: str) -> None:
    dl = HTTPDownloader()
    out = tmp / "segmented.bin"
    job = DownloadJob(url=f"{base}/file", file_path=str(out))
    await dl.start_download(job)
    check("segmented: multiple connections were used", job.thread_count > 1,
          f"thread_count={job.thread_count}, segments={len(job.segments)}")
    check("segmented: every segment completed",
          all(s.completed for s in job.segments),
          f"{sum(1 for s in job.segments if s.completed)}/{len(job.segments)}")
    check("segmented: byte-exact file", out.is_file() and digest_of(out) == DIGEST,
          f"{out.stat().st_size if out.is_file() else 0} of {SIZE} bytes")
    check("segmented: size matches", out.is_file() and out.stat().st_size == SIZE)
    await dl.close()


async def test_server_ignoring_range(tmp: Path, base: str) -> None:
    """A 200 answer to a Range request must fall back, not corrupt the file."""
    dl = HTTPDownloader()
    out = tmp / "norange.bin"
    job = DownloadJob(url=f"{base}/norange", file_path=str(out))
    await dl.start_download(job)
    check("range-ignoring server: byte-exact file",
          out.is_file() and digest_of(out) == DIGEST,
          f"{out.stat().st_size if out.is_file() else 0} of {SIZE} bytes")
    check("range-ignoring server: fell back to one connection",
          job.thread_count == 1, f"thread_count={job.thread_count}")
    await dl.close()


async def test_truncated_segment(tmp: Path, base: str) -> None:
    """A short body must fail the job and leave a *resumable* partial file."""
    dl = HTTPDownloader()
    out = tmp / "short.bin"
    job = DownloadJob(url=f"{base}/short", file_path=str(out))
    failed = False
    reason = "no error raised"
    try:
        await dl.start_download(job)
    except Exception as exc:  # noqa: BLE001
        failed = True
        reason = str(exc)[:80]
    check("truncated server: job fails loudly", failed, reason)
    check("truncated server: partial file kept for resume", out.exists(),
          f"{out.stat().st_size if out.exists() else 0} bytes on disk")
    check("truncated server: resume state written",
          sidecar_path(out).is_file(),
          str(sidecar_path(out).name))
    await dl.close()


async def test_pause_resume_cancel(tmp: Path, base: str) -> None:
    dl = HTTPDownloader()
    # /slow dribbles 64 KiB every 50 ms, so pause/cancel have something to
    # interrupt instead of racing a localhost transfer that finishes instantly.
    out = tmp / "pause.bin"
    job = DownloadJob(url=f"{base}/slow", file_path=str(out))

    task = asyncio.create_task(dl.start_download(job))
    await asyncio.sleep(0.5)
    await dl.pause(job)
    await asyncio.sleep(0.1)
    paused_at = job.downloaded_bytes
    await asyncio.sleep(0.4)
    check("pause: transfer actually stopped",
          job.downloaded_bytes == paused_at and paused_at > 0,
          f"{paused_at} -> {job.downloaded_bytes} bytes")
    await dl.resume(job)
    await asyncio.wait_for(task, timeout=120)
    check("resume: completes byte-exact after pause",
          out.is_file() and digest_of(out) == DIGEST)

    # Cancel mid-flight — a cancelled job must not look completed.
    out2 = tmp / "cancel.bin"
    job2 = DownloadJob(url=f"{base}/slow", file_path=str(out2))
    task2 = asyncio.create_task(dl.start_download(job2))
    await asyncio.sleep(0.5)
    await dl.cancel(job2)
    cancelled = False
    try:
        await asyncio.wait_for(task2, timeout=15)
    except asyncio.CancelledError:
        cancelled = True
    except asyncio.TimeoutError:
        pass
    check("cancel: surfaces as cancellation, not success", cancelled,
          "CancelledError propagated" if cancelled else "start_download returned normally")
    check("cancel: partial file removed", not out2.exists(), f"exists={out2.exists()}")
    await dl.close()


async def test_throttle_no_stall(tmp: Path, base: str) -> None:
    """A per-task cap smaller than one chunk must not deadlock the transfer."""
    bw = BandwidthManager(global_rate=0.0)
    bw.create_task_limiter("job", rate=20 * 1024)   # 20 KB/s vs 256 KB chunks
    dl = HTTPDownloader(bandwidth_manager=bw)
    out = tmp / "throttled.bin"
    job = DownloadJob(url=f"{base}/file", file_path=str(out))
    job.id = "job"
    try:
        await asyncio.wait_for(dl.start_download(job), timeout=30)
        stalled = False
    except asyncio.TimeoutError:
        stalled = True
    check("throttle: tiny cap does not stall the download", not stalled,
          f"{job.downloaded_bytes} bytes moved")
    check("throttle: file still byte-exact",
          out.is_file() and digest_of(out) == DIGEST)
    await dl.close()


async def test_oversized_token_bucket() -> None:
    bucket = TokenBucket(rate=10 * 1024)
    started = time.monotonic()
    try:
        await asyncio.wait_for(bucket.acquire(512 * 1024), timeout=5)
        ok = True
        detail = f"took {time.monotonic() - started:.2f}s"
    except asyncio.TimeoutError:
        ok = False
        detail = "acquire() never returned (stall)"
    check("token bucket: oversized request drains instead of hanging", ok, detail)


async def test_streaming_buffer_semantics() -> None:
    buf = StreamingBuffer("/nonexistent", 1000)
    buf.add_range(0, 100)
    buf.add_range(300, 400)          # gap → prefix stays 100
    check("streaming buffer: only contiguous prefix is readable",
          buf.available_bytes() == 100, f"available={buf.available_bytes()}")
    buf.add_range(100, 300)          # fills the gap → prefix becomes 400
    check("streaming buffer: merged ranges extend the prefix",
          buf.available_bytes() == 400, f"available={buf.available_bytes()}")
    buf.mark_complete()
    check("streaming buffer: complete file reports full size",
          buf.available_bytes() == 1000, f"available={buf.available_bytes()}")


async def test_job_reporting(tmp: Path, base: str) -> None:
    """Progress, speed and byte accounting must add up for segmented jobs."""
    dl = HTTPDownloader()
    out = tmp / "report.bin"
    job = DownloadJob(url=f"{base}/file", file_path=str(out))
    ticks: list[float] = []
    await dl.start_download(job, progress_callback=lambda j: ticks.append(j.progress_percent))
    check("reporting: downloaded_bytes equals file size",
          job.downloaded_bytes == SIZE, f"{job.downloaded_bytes} vs {SIZE}")
    check("reporting: per-segment bytes sum to the whole file",
          sum(s.downloaded_bytes for s in job.segments) == SIZE,
          f"sum={sum(s.downloaded_bytes for s in job.segments)}")
    check("reporting: progress reached 100%",
          bool(ticks) and max(ticks) >= 99.9, f"max={max(ticks, default=0):.1f}%")
    check("reporting: speed was measured", job.speed_bps > 0,
          f"{job.speed_bps / 1024:.0f} KB/s")
    await dl.close()


async def test_resume_after_failure(tmp: Path, base: str) -> None:
    """An interrupted download must continue, not start from zero."""
    import json as _json

    dl = HTTPDownloader()
    out = tmp / "resume.bin"

    # ── first attempt: the server truncates every range response ──
    SERVER_MODE["truncate"] = True
    job = DownloadJob(url=f"{base}/flaky", file_path=str(out))
    try:
        await dl.start_download(job)
        first_ok = True
    except Exception:  # noqa: BLE001
        first_ok = False
    check("interrupted attempt fails", not first_ok)

    state = _json.loads(sidecar_path(out).read_text())
    recorded = sum(s["downloaded_bytes"] for s in state["segments"])
    check("resume state records partial progress", 0 < recorded < SIZE,
          f"{recorded:,} of {SIZE:,} bytes")
    check("recorded progress covers every segment",
          len(state["segments"]) == len(job.segments),
          f"{len(state['segments'])} segments")
    check("no segment is marked complete yet",
          all(s["downloaded_bytes"] < s["end_byte"] - s["start_byte"] + 1
              for s in state["segments"]))

    # ── second attempt: server healthy, same file path ────────────
    SERVER_MODE["truncate"] = False
    RANGES.clear()
    job2 = DownloadJob(url=f"{base}/flaky", file_path=str(out))
    await dl.start_download(job2)

    check("resumed download is byte-exact", digest_of(out) == DIGEST,
          f"{out.stat().st_size:,} bytes")
    check("resumed download accounts for every byte",
          job2.downloaded_bytes == SIZE, f"{job2.downloaded_bytes:,}")
    check("no range was re-requested from the start of a segment",
          all(parse_range(r, SIZE)[0] > 0 for r in RANGES if parse_range(r, SIZE)),
          f"{len(RANGES)} ranges; first={RANGES[0] if RANGES else '-'}")
    check("resume state removed after success", not sidecar_path(out).exists())
    await dl.close()


async def test_cancel_discards_partial(tmp: Path, base: str) -> None:
    """Cancel is user intent to discard, so nothing is left behind."""
    dl = HTTPDownloader()
    out = tmp / "cancel_clean.bin"
    job = DownloadJob(url=f"{base}/slow", file_path=str(out))
    task = asyncio.create_task(dl.start_download(job))
    await asyncio.sleep(0.5)
    await dl.cancel(job)
    try:
        await asyncio.wait_for(task, timeout=15)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    check("cancel removes the partial file", not out.exists())
    check("cancel removes the resume state", not sidecar_path(out).exists())
    await dl.close()


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="httptest-"))
    runner, base = await start_server()
    try:
        await test_segmented(tmp, base)
        await test_server_ignoring_range(tmp, base)
        await test_truncated_segment(tmp, base)
        await test_resume_after_failure(tmp, base)
        await test_cancel_discards_partial(tmp, base)
        await test_pause_resume_cancel(tmp, base)
        await test_throttle_no_stall(tmp, base)
        await test_oversized_token_bucket()
        await test_streaming_buffer_semantics()
        await test_job_reporting(tmp, base)
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
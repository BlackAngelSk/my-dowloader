#!/usr/bin/env python3
"""End-to-end DownloadManager flow test against a local HTTP server.

Covers the dispatch path that the engine rewrite touched: priority heap →
_dispatch_job → module → terminal state, plus the "a module-reported FAILED
must not be overwritten as COMPLETED" rule and cancel handling.

Usage: QT_QPA_PLATFORM=offscreen .venv/bin/python tools/test_manager_flow.py
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aiohttp import web  # noqa: E402
from PyQt6.QtCore import QCoreApplication  # noqa: E402

from omnidownloader.core.download_manager import DownloadManager  # noqa: E402
from omnidownloader.core.models import DownloadJob, DownloadState, Priority  # noqa: E402
from omnidownloader.modules.http_downloader import HTTPDownloader  # noqa: E402
from omnidownloader.services.plugin_loader import PluginLoader  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
SIZE = 2 * 1024 * 1024 + 777
PAYLOAD = bytes(random.Random(99).randbytes(SIZE))
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def parse_range(header: str, size: int):
    unit, _, spec = header.partition("=")
    if unit.strip() != "bytes":
        return None
    start_s, _, end_s = spec.partition("-")
    try:
        start = int(start_s)
    except ValueError:
        return None
    end = int(end_s) if end_s else size - 1
    return start, min(end, size - 1)


async def handle(request):
    rng = request.headers.get("Range")
    parsed = parse_range(rng, SIZE) if rng else None
    if parsed:
        start, end = parsed
        body = PAYLOAD[start:end + 1]
        return web.Response(body=body, status=206, headers={
            "Content-Range": f"bytes {start}-{end}/{SIZE}",
            "Accept-Ranges": "bytes", "Content-Length": str(len(body)),
        })
    return web.Response(body=PAYLOAD, status=200,
                        headers={"Accept-Ranges": "bytes",
                                 "Content-Length": str(SIZE)})


async def wait_for(predicate, timeout: float = 60.0, poll: float = 0.05) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(poll)
    return False


async def main() -> int:
    app = QCoreApplication(sys.argv)
    assert app is not None
    tmp = Path(tempfile.mkdtemp(prefix="mgrtest-"))

    runner = web.AppRunner(web.Application())
    runner.app.router.add_get("/file.bin", handle)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/file.bin"

    dm = DownloadManager(max_concurrent=2)
    for mod in PluginLoader().load_all():
        dm.register_module(mod)
    some_job = DownloadJob(url=url)
    dm._jobs[some_job.id] = some_job   # so the dispatcher can see it
    dm._push_job(some_job.id)
    dm._jobs.pop(some_job.id)

    engine = asyncio.create_task(dm.run())
    try:
        # ── happy path ─────────────────────────────────────────
        out = tmp / "ok.bin"
        job = dm.enqueue(url, download_path=str(out))
        got_terminal = await wait_for(
            lambda: dm.get_job(job.id) is not None
            and job.state in (DownloadState.COMPLETED, DownloadState.FAILED)
        )
        check("job reaches a terminal state", got_terminal, job.state.value)
        check("job is COMPLETED", job.state == DownloadState.COMPLETED,
              f"{job.state.value} {job.error_message or ''}")
        check("produced a byte-exact file",
              out.is_file() and hashlib.sha256(out.read_bytes()).hexdigest() == DIGEST,
              f"{out.stat().st_size if out.is_file() else 0} of {SIZE}")
        check("multi-connection path was used", job.thread_count > 1,
              f"thread_count={job.thread_count}")
        check("completed_at was stamped", job.completed_at is not None)

        # ── disk-space failure must stay FAILED ────────────────
        blocked = DownloadJob(url=url, file_path=str(tmp / "blocked.bin"))
        original = dm._bw_manager

        class _FailModule(HTTPDownloader):
            async def start_download(self, job, progress_callback=None):
                job.state = DownloadState.FAILED
                job.error_message = "simulated module-level failure"

        dm2 = DownloadManager(max_concurrent=1)
        dm2._jobs[blocked.id] = blocked
        dm2.register_module(_FailModule())
        await dm2._dispatch_job(blocked)
        check("module-reported FAILED is not overwritten as COMPLETED",
              blocked.state == DownloadState.FAILED,
              f"{blocked.state.value}: {blocked.error_message}")
        await dm2.shutdown()
        assert original is not None

        # ── cancel ─────────────────────────────────────────────
        out2 = tmp / "cancelled.bin"
        job2 = dm.enqueue(url, download_path=str(out2))
        await asyncio.sleep(0.05)
        dm.cancel_job(job2.id)
        cancelled = await wait_for(
            lambda: job2.state == DownloadState.CANCELLED, timeout=10)
        check("cancel moves the job to CANCELLED", cancelled, job2.state.value)
    finally:
        engine.cancel()
        await dm.shutdown()
        await runner.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)

    failures = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
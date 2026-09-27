#!/usr/bin/env python3
"""Magnet link support: metadata reading, progress parsing, live download.

Deterministic checks run offline (synthetic .torrent files, synthetic aria2c
output).  The live part — resolving a real magnet and pulling bytes — only runs
when you pass a magnet, so CI stays offline:

    .venv/bin/python tools/test_magnet.py
    .venv/bin/python tools/test_magnet.py --live "magnet:?xt=urn:btih:..."

Why this exists: the aria2c backend never set the job's name/size/path, showed
no progress while resolving a magnet, and returned an empty stub for magnet
metadata when libtorrent was absent.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnidownloader.core import platform_utils, torrent_meta  # noqa: E402
from omnidownloader.core.download_manager import DownloadManager  # noqa: E402
from omnidownloader.core.models import (  # noqa: E402
    DownloadJob, DownloadModule, DownloadState,
)
from omnidownloader.core.torrent_meta import (  # noqa: E402
    BencodeError, TorrentFile, read_metadata,
)
from omnidownloader.modules.torrent_downloader import (  # noqa: E402
    HAS_ARIA2C, HAS_LIBTORRENT, TorrentDownloader, _aria2_failure, _size_to_bytes,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def bencode(obj) -> bytes:
    if isinstance(obj, int):
        return b"i%de" % obj
    if isinstance(obj, bytes):
        return b"%d:%s" % (len(obj), obj)
    if isinstance(obj, str):
        return bencode(obj.encode())
    if isinstance(obj, list):
        return b"l" + b"".join(bencode(i) for i in obj) + b"e"
    if isinstance(obj, dict):
        return (b"d" + b"".join(bencode(k) + bencode(v) for k, v in obj.items())
                + b"e")
    raise TypeError(type(obj))


def make_torrent(tmp: Path, multi: bool) -> Path:
    if multi:
        info = {
            b"name": b"Album", b"piece length": 262144, b"pieces": b"x" * 20,
            b"files": [
                {b"length": 1500, b"path": [b"disc1", b"01 - one.mp3"]},
                {b"length": 2500, b"path": [b"disc1", b"02 - two.mp3"]},
            ],
        }
    else:
        info = {b"name": b"single.iso", b"length": 4096,
                b"piece length": 262144, b"pieces": b"y" * 20}
    blob = bencode({b"announce": b"udp://tracker.example:1337/announce",
                    b"comment": b"test torrent", b"info": info})
    path = tmp / ("multi.torrent" if multi else "single.torrent")
    path.write_bytes(blob)
    return path


def test_metadata_reader(tmp: Path) -> None:
    single = read_metadata(make_torrent(tmp, multi=False))
    check("single-file torrent: name and size",
          single.name == "single.iso" and single.total_size == 4096,
          f"{single.name} {single.total_size}")
    check("single-file torrent lists its file",
          len(single.files) == 1 and single.files[0].length == 4096)
    check("single-file torrent is not marked multi",
          not single.is_multi_file)

    multi = read_metadata(make_torrent(tmp, multi=True))
    check("multi-file torrent sums its files",
          multi.total_size == 4000 and len(multi.files) == 2,
          f"{multi.total_size} bytes in {len(multi.files)} files")
    check("multi-file torrent keeps relative paths",
          multi.files[1].path == "disc1/02 - two.mp3", multi.files[1].path)
    check("multi-file torrent is marked multi", multi.is_multi_file)
    check("metadata converts to the UI dict shape",
          set(multi.as_dict()) >= {"name", "total_size", "files", "thumbnail"},
          str(sorted(multi.as_dict())))
    check("file list is exposed as path/length pairs",
          multi.as_dict()["files"][0]["path"].startswith("disc1/"))

    junk = tmp / "notatorrent.torrent"
    junk.write_bytes(b"<!doctype html><html>404</html>")
    try:
        read_metadata(junk)
        check("an HTML error page saved as .torrent is rejected", False, "accepted")
    except BencodeError as exc:
        check("an HTML error page saved as .torrent is rejected", True, str(exc)[:50])


def test_metadata_discovery(tmp: Path) -> None:
    """aria2c saves <infohash>.torrent; we must find the newest one."""
    work = tmp / "work"
    work.mkdir()
    (work / "random.torrent").write_bytes(b"d4:infod4:name1:xe e")
    check("a non-infohash .torrent is ignored",
          torrent_meta.find_saved_metadata(work) is None)
    good = work / ("a" * 40 + ".torrent")
    good.write_bytes(b"d4:infod4:name1:xe e")
    check("an infohash-named .torrent is found",
          torrent_meta.find_saved_metadata(work) == good)


def test_size_tokens() -> None:
    cases = {"756MiB": 756 * 1024 ** 2, "1.2GiB": int(1.2 * 1024 ** 3),
             "512KiB": 524288, "0B": 0, "3MiB/s": 0}
    ok = all(_size_to_bytes(k) == v for k, v in cases.items())
    check("aria2c size tokens parse", ok,
          ", ".join(f"{k}->{_size_to_bytes(k)}" for k in cases))


def test_progress_parsing() -> None:
    mod = TorrentDownloader()
    job = DownloadJob(url="magnet:?xt=urn:btih:" + "a" * 40,
                      module=DownloadModule.TORRENT)
    ticks: list[float] = []

    def cb(j):
        ticks.append(j.progress_percent)

    # aria2c's FILE: line announces the phase.  The metadata phase reports the
    # .torrent's own size (59 KiB) as "100%" — that must not be taken as the
    # payload, or a magnet looks instantly finished at 59 KiB.
    mod._parse_torrent_progress(
        job, "FILE: [MEMORY][METADATA]debian-13.7.0-amd64-netinst.iso", cb)
    check("the metadata phase is announced and named",
          job.state == DownloadState.EXTRACTING
          and job.file_name == "debian-13.7.0-amd64-netinst.iso",
          f"state={job.state.value} name={job.file_name}")
    mod._parse_torrent_progress(job, "[#3c603c 59KiB/59KiB(100%) CN:32 SD:0]", cb)
    check("metadata-phase size is not mistaken for the payload",
          job.file_size == 0 and job.progress_percent == 0.0,
          f"size={job.file_size} pct={job.progress_percent}")

    mod._parse_torrent_progress(
        job, "FILE: /tmp/dl/debian-13.7.0-amd64-netinst.iso", cb)
    mod._parse_torrent_progress(
        job, "[#1 756MiB/756MiB(50%) CN:4 SD:12 DL:1.2MiB ETA:3m]", cb)
    check("a transfer line updates percentage",
          abs(job.progress_percent - 50.0) < 0.01, f"{job.progress_percent}%")
    check("a transfer line updates size",
          job.file_size == 792723456, f"{job.file_size}")
    check("a transfer line updates downloaded bytes",
          job.downloaded_bytes == int(job.file_size * 0.5), str(job.downloaded_bytes))
    check("download speed is picked up", job.speed_bps > 0, f"{job.speed_bps:.0f} B/s")
    check("progress callback fired", len(ticks) >= 2, str(ticks[-3:]))
    check("the transfer phase follows the metadata phase",
          mod._phases[job.id] == "transfer", mod._phases[job.id])


def test_aria2_command() -> None:
    """The argv must prevent the exit-13 'file already exists' failure."""
    mod = TorrentDownloader()
    job = DownloadJob(url="magnet:?xt=urn:btih:" + "d" * 40,
                      module=DownloadModule.TORRENT)
    cmd = mod._aria2_command(job)
    check("aria2c allows overwriting an existing file (exit 13 fix)",
          "--allow-overwrite=true" in cmd,
          " ".join(f for f in cmd if "overwrite" in f) or "MISSING")
    check("aria2c integrity-checks existing data instead of re-downloading",
          "--check-integrity=true" in cmd,
          " ".join(f for f in cmd if "integrity" in f) or "MISSING")
    check("aria2c keeps progress output enabled",
          "--console-log-level=notice" in cmd)
    check("aria2c resumes partial downloads",
          "--continue=true" in cmd)
    check("aria2c has DHT entry points for tracker-less magnets",
          "--dht-entry-point=router.bittorrent.com:6881" in cmd
          and "--enable-dht=true" in cmd)
    check("the URL is the last argument", cmd[-1] == job.url, cmd[-1][:40])
    check("the download directory is passed", "--dir" in cmd)

    limited = TorrentDownloader(max_download_rate=512)
    check("a rate limit is applied when configured",
          "--max-overall-download-limit" in limited._aria2_command(job))


def test_exit_code_messages() -> None:
    check("exit 13 is explained",
          "already exists" in _aria2_failure(13), _aria2_failure(13)[:70])
    check("exit 9 mentions disk space",
          "disk space" in _aria2_failure(9), _aria2_failure(9)[:60])
    check("an unknown code still produces a readable message",
          _aria2_failure(99).startswith("aria2c exited with code 99"),
          _aria2_failure(99))
    check("the tracker's own message is included when available",
          "not authorized" in _aria2_failure(1, ["Tracker: not authorized"]),
          _aria2_failure(1, ["Tracker: not authorized"]))


def test_infohash(tmp: Path) -> None:
    """The info-hash identifies a torrent (used to spot duplicate downloads)."""
    import hashlib

    path = make_torrent(tmp, multi=False)
    blob = path.read_bytes()
    info = {b"name": b"single.iso", b"length": 4096,
            b"piece length": 262144, b"pieces": b"y" * 20}
    expected = hashlib.sha1(bencode(info)).hexdigest()
    check("a .torrent file's info-hash is computed correctly",
          torrent_meta.infohash(path) == expected,
          f"{torrent_meta.infohash(path)} vs {expected}")
    check("a magnet's info-hash is read from the link",
          torrent_meta.infohash_from_magnet(
              "magnet:?xt=urn:btih:6008CDF59CA7AE985834A5C8EE8FE853CF6DE81E&dn=x")
          == "6008cdf59ca7ae985834a5c8ee8fe853cf6de81e")
    check("a magnet without an xt has no info-hash",
          torrent_meta.infohash_from_magnet("magnet:?dn=only-a-name") == "")
    check("a non-torrent file has no info-hash",
          torrent_meta.infohash(tmp / "notatorrent.torrent") == "")


def test_no_peer_detection(tmp: Path) -> None:
    """A torrent nobody can serve must fail with an explanation, not hang.

    Two shapes of "dead" and both are invisible to the user otherwise:
      CN:0        — no peers at all
      CN:27 SD:0  — leechers connected, but no seeder has the data, so the
                    metadata phase sits at 0B/0B forever.
    """
    mod = TorrentDownloader()
    mod.no_peer_timeout = 0.05
    job = DownloadJob(url="magnet:?xt=urn:btih:" + "e" * 40,
                      module=DownloadModule.TORRENT)

    mod._parse_torrent_progress(job, "[#1 0B/0B CN:0 SD:0 DL:0B]", None)
    check("the peer count is recorded from aria2c",
          job.metadata.get("peers") == 0 and job.metadata.get("seeders") == 0,
          f"peers={job.metadata.get('peers')} seeders={job.metadata.get('seeders')}")
    mod._check_sources(job)
    check("peerless time starts counting", job.id in mod._no_source_since)
    check("the UI gets a status note while waiting",
          job.metadata.get("status_note") == "waiting for peers…",
          repr(job.metadata.get("status_note")))
    time.sleep(0.06)
    check("a peerless torrent is reported as dead", mod._source_starved(job))
    check("the no-peer message explains what to do",
          "no peers" in mod._dead_torrent_message(job),
          mod._dead_torrent_message(job)[:70])

    # Leechers connected but no seeder: the metadata never arrives.
    dead_swarm = DownloadJob(url="magnet:?xt=urn:btih:" + "a" * 40,
                             module=DownloadModule.TORRENT)
    mod._parse_torrent_progress(
        dead_swarm, "FILE: [MEMORY][METADATA]some.name.mkv", None)
    mod._parse_torrent_progress(dead_swarm, "[#1 0B/0B CN:27 SD:0 DL:0B]", None)
    mod._check_sources(dead_swarm)
    check("a leecher-only swarm starts the countdown",
          dead_swarm.id in mod._no_source_since)
    check("the status note explains the seeder problem",
          "no seeder" in (dead_swarm.metadata.get("status_note") or ""),
          repr(dead_swarm.metadata.get("status_note")))
    time.sleep(0.06)
    check("a leecher-only swarm is reported as dead",
          mod._source_starved(dead_swarm))
    check("the seeder message says it cannot finish",
          "no seeder" not in "" and "cannot" in mod._dead_torrent_message(dead_swarm),
          mod._dead_torrent_message(dead_swarm)[:90])

    # A healthy swarm clears everything.
    mod._parse_torrent_progress(job, "[#1 2MiB/756MiB(0%) CN:7 SD:3 DL:1MiB]", None)
    mod._check_sources(job)
    check("a healthy swarm clears the countdown",
          not mod._source_starved(job) and job.metadata["peers"] == 7
          and "status_note" not in job.metadata,
          f"peers={job.metadata.get('peers')} note={job.metadata.get('status_note')!r}")

    # A transfer that is actually moving is never aborted, even if the summary
    # momentarily reports zero peers (peers churn during a download).
    moving = DownloadJob(url="magnet:?xt=urn:btih:" + "f" * 40,
                         module=DownloadModule.TORRENT)
    moving.file_size = 1000
    moving.downloaded_bytes = 500
    mod._parse_torrent_progress(moving, "[#2 500B/1000B(50%) CN:0 SD:0 DL:0B]", None)
    mod._check_sources(moving)
    time.sleep(0.06)
    check("a moving download is never called dead", not mod._source_starved(moving))


def test_proxy_routing(tmp: Path) -> None:
    """Proxy handling must not break torrents, and must not leak by accident.

    aria2's --all-proxy takes http/https/ftp only — passing a socks5:// URL makes
    aria2 abort with "unrecognized protocol" (exit 28), which broke every torrent
    while Tor was enabled.
    """
    job = DownloadJob(url="magnet:?xt=urn:btih:" + "a" * 40,
                      module=DownloadModule.TORRENT)

    class HttpProxy:
        enabled = True

        def get_proxy_url(self):
            return "http://127.0.0.1:8118"

    cmd = TorrentDownloader(proxy_manager=HttpProxy())._aria2_command(job)
    check("an HTTP proxy is passed to aria2c",
          "--all-proxy" in cmd and "http://127.0.0.1:8118" in cmd,
          " ".join(cmd[cmd.index("--all-proxy"):cmd.index("--all-proxy") + 2]))
    check("DHT is disabled behind a proxy (UDP would expose the real IP)",
          "--enable-dht=false" in cmd,
          " ".join(f for f in cmd if "dht" in f.lower()))
    check("LPD is disabled behind a proxy", "--bt-enable-lpd=false" in cmd)

    class SocksProxy:
        enabled = True

        def get_proxy_url(self):
            return "socks5://127.0.0.1:9050"      # Tor

    import os

    tor_mod = TorrentDownloader(proxy_manager=SocksProxy())
    saved = os.environ.pop("OMNI_ALLOW_TORRENTS_DIRECT", None)
    try:
        try:
            tor_mod._aria2_command(job)
            refused, message = False, ""
        except RuntimeError as exc:
            refused, message = True, str(exc)
        check("a SOCKS proxy is refused instead of breaking aria2 (exit 28)",
              refused, message[:90])
        check("the refusal explains how to proceed",
              "turn Tor off" in message and "OMNI_ALLOW_TORRENTS_DIRECT" in message,
              message[:90])
        check("the actionable part survives the card's 80-char truncation",
              "turn Tor off" in message[:80], message[:80])
        check("the refusal never passes socks5 to aria2",
              "socks5" not in " ".join(
                  tor_mod._aria2_command(job)) if not refused else True)

        os.environ["OMNI_ALLOW_TORRENTS_DIRECT"] = "1"
        direct = tor_mod._aria2_command(job)
        check("the opt-out allows a direct transfer, without --all-proxy",
              "--all-proxy" not in direct, " ".join(direct[-3:]))
    finally:
        os.environ.pop("OMNI_ALLOW_TORRENTS_DIRECT", None)
        if saved is not None:
            os.environ["OMNI_ALLOW_TORRENTS_DIRECT"] = saved

    plain_cmd = TorrentDownloader()._aria2_command(job)
    check("without a proxy DHT stays enabled",
          "--enable-dht=true" in plain_cmd and "--all-proxy" not in plain_cmd)


def test_no_unknown_options(tmp: Path) -> None:
    """Every flag passed to aria2c must exist in the installed build.

    Exit 28 ("aria2 rejected an option it was given") is what a bad flag looks
    like to the user, and it is invisible otherwise.
    """
    import subprocess

    if not HAS_ARIA2C:
        return
    mod = TorrentDownloader(save_path=str(tmp))
    job = DownloadJob(url="magnet:?xt=urn:btih:" + "c" * 40,
                      module=DownloadModule.TORRENT)
    argv = mod._aria2_command(job)
    flags = [a for a in argv if a.startswith("--") and "=" in a]
    # --dir and friends take a separate value; only check the self-contained ones.
    flags.append("--allow-overwrite=true")   # already present, kept explicit

    bad: list[str] = []
    for flag in dict.fromkeys(flags):
        result = subprocess.run(
            ["aria2c", flag, "--dry-run=true", "--file-allocation=none",
             "http://127.0.0.1:1/nothing"],
            capture_output=True, text=True, timeout=30,
            **platform_utils.subprocess_kwargs())
        output = (result.stdout + result.stderr).lower()
        if "unrecognized" in output or "unknown option" in output:
            bad.append(flag)
    check("no flag passed to aria2c is rejected by this build", not bad,
          "; ".join(bad) or f"{len(set(flags))} flags checked")


def test_duplicate_guard() -> None:
    """Two aria2c writers for one torrent truncate each other's progress."""
    import subprocess

    mod = TorrentDownloader()
    key = "a" * 40
    first = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             **platform_utils.subprocess_kwargs())
    mod._procs_by_hash[key] = first
    mod._reap_duplicate(key)
    try:
        first.wait(timeout=10)
        check("a duplicate download for the same torrent is stopped", True,
              f"exit={first.returncode}")
    except subprocess.TimeoutExpired:
        first.kill()
        check("a duplicate download for the same torrent is stopped", False,
              "still running")
    check("the stopped process is forgotten", key not in mod._procs_by_hash)

    # Shutdown must reap everything still running.
    second = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                              **platform_utils.subprocess_kwargs())
    mod._procs_by_hash["b" * 40] = second
    mod.stop_processes()
    try:
        second.wait(timeout=10)
        check("shutdown stops the aria2c processes", True, f"exit={second.returncode}")
    except subprocess.TimeoutExpired:
        second.kill()
        check("shutdown stops the aria2c processes", False, "still running")
    check("all bookkeeping is cleared on shutdown",
          not mod._procs_by_hash and not mod._phases and not mod._no_source_since)


def test_describe_result(tmp: Path) -> None:
    """After a download the job must point at the real file, not nothing."""
    mod = TorrentDownloader(save_path=str(tmp))
    before = mod._snapshot_dir(tmp)
    produced = tmp / "debian-13.7.0-amd64-netinst.iso"
    produced.write_bytes(b"\x00" * 2048)
    (tmp / (produced.name + ".aria2")).write_bytes(b"ctrl")
    job = DownloadJob(url="magnet:?xt=urn:btih:" + "b" * 40,
                      module=DownloadModule.TORRENT)
    mod._describe_result(job, before)
    check("the job gets the produced file path",
          job.file_path == str(produced), job.file_path)
    check("the job gets a display name", job.file_name == produced.name, job.file_name)
    check("the job gets a size", job.file_size == 2048, str(job.file_size))
    check("the aria2 control file is not mistaken for the result",
          not job.file_path.endswith(".aria2"))


async def test_routing() -> None:
    dm = DownloadManager()
    dm.register_module(TorrentDownloader())
    magnet = "magnet:?xt=urn:btih:" + "c" * 40 + "&dn=example"
    mod = await dm._resolve_module(DownloadJob(url=magnet, module=DownloadModule.UNKNOWN))
    check("a magnet link routes to the torrent module",
          mod is not None and getattr(mod, "MODULE_NAME", "") == "torrent",
          f"{mod.display_name() if mod else 'none'} "
          f"(libtorrent={HAS_LIBTORRENT} aria2c={HAS_ARIA2C})")
    check("can_handle accepts magnets",
          TorrentDownloader().can_handle(magnet))


async def test_live(magnet: str, budget: int = 90) -> None:
    """Resolve a real magnet and pull bytes, then cancel."""
    mod = TorrentDownloader()
    meta = await mod.extract_metadata(magnet)
    check("live: magnet metadata resolves to a name and size",
          bool(meta.get("name")) and meta.get("name") != "Torrent"
          and meta.get("total_size", -1) > 0,
          f"{meta.get('name')!r} {meta.get('total_size')} bytes "
          f"({len(meta.get('files') or [])} files)")

    tmp = Path(tempfile.mkdtemp(prefix="magnetlive-"))
    dl = TorrentDownloader(save_path=str(tmp))
    job = DownloadJob(url=magnet, module=DownloadModule.TORRENT)
    ticks: list[float] = []
    task = asyncio.create_task(dl.start_download(job, progress_callback=lambda j: ticks.append(j.progress_percent)))
    deadline = time.time() + budget
    while time.time() < deadline:
        await asyncio.sleep(2)
        if task.done():
            break
        # Drive on *progress*, not the file size on disk: aria2c preallocates
        # the full file immediately, so size never indicates real progress.
        if any(t >= 5.0 for t in ticks):
            break
    data = sum(p.stat().st_size for p in tmp.rglob("*") if p.is_file())
    check("live: the magnet downloads real bytes", data > 1024 ** 2,
          f"{data / 1024 ** 2:.1f} MiB")
    check("live: progress was reported to the UI", any(t > 0 for t in ticks),
          f"{len(ticks)} ticks, last={ticks[-1] if ticks else None}")
    check("live: the job knows its name and size",
          bool(job.file_name) and job.file_size > 1024 ** 2,
          f"{job.file_name!r} {job.file_size}")
    check("live: the file list is available for selective download",
          bool(job.metadata.get("torrent_files")),
          f"{len(job.metadata.get('torrent_files') or [])} entries")

    dl._cancel_flags[job.id] = True
    try:
        await asyncio.wait_for(task, timeout=30)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    except Exception as exc:  # noqa: BLE001
        print("   (cancel raised:", type(exc).__name__, ")")
    shutil.rmtree(tmp, ignore_errors=True)


async def test_live_twice(source: str) -> None:
    """The same torrent twice: the second run must not fail with exit 13.

    aria2c refuses to touch an existing file for a torrent unless told
    otherwise ("File ... exists, but a control file (*.aria2) does not exist,
    Download was canceled"), which is exactly what a user re-adding a finished
    torrent hits.
    """
    tmp = Path(tempfile.mkdtemp(prefix="magnettwice-"))
    mod = TorrentDownloader(save_path=str(tmp))
    try:
        for label in ("first run", "second run (file already exists)"):
            job = DownloadJob(url=source, module=DownloadModule.TORRENT)
            started = time.time()
            try:
                await mod.start_download(job)
                ok = True
                detail = ""
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"{type(exc).__name__}: {str(exc)[:160]}"
            elapsed = time.time() - started
            check(f"live: {label} succeeds", ok, detail or f"{elapsed:.1f}s")
            if ok:
                check(f"live: {label} reports the result",
                      bool(job.file_name) and job.file_size > 0
                      and bool(job.file_path),
                      f"{job.file_name!r} {job.file_size} "
                      f"{Path(job.file_path).name!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def main() -> int:
    live = ""
    live_twice = ""
    if "--live" in sys.argv:
        idx = sys.argv.index("--live")
        if idx + 1 < len(sys.argv):
            live = sys.argv[idx + 1]
    if "--live-twice" in sys.argv:
        idx = sys.argv.index("--live-twice")
        if idx + 1 < len(sys.argv):
            live_twice = sys.argv[idx + 1]

    tmp = Path(tempfile.mkdtemp(prefix="magnettest-"))
    try:
        test_metadata_reader(tmp)
        test_metadata_discovery(tmp)
        test_size_tokens()
        test_progress_parsing()
        test_aria2_command()
        test_exit_code_messages()
        test_infohash(tmp)
        test_no_peer_detection(tmp)
        test_proxy_routing(tmp)
        test_no_unknown_options(tmp)
        test_duplicate_guard()
        test_describe_result(tmp)
        await test_routing()
        if live:
            await test_live(live)
        elif not live_twice:
            print("[SKIP] live magnet download (pass --live <magnet> to run it)")
        if live_twice:
            await test_live_twice(live_twice)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failures = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

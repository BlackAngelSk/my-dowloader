"""Headless dependency installer: ``python -m omnidownloader --install-deps``.

The GUI installs yt-dlp and ffmpeg in the background *after* it opens, which
means the first few links a new user pastes fail while the download is still
running — and on Windows `start.bat` finishes long before any of that.  This
entry point does the same work synchronously, before the app starts, so the
launcher can say "everything is installed" and be telling the truth.

Exit code is 0 only when the required tools (yt-dlp, ffmpeg) are usable.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from omnidownloader.services.dependency_manager import DependencyManager


def _report(deps: DependencyManager) -> tuple[dict[str, bool], dict[str, str]]:
    """Current status of every managed tool, plus the paths where found."""
    status = deps.check_all()
    paths = {
        "yt-dlp": deps.ytdlp_path,
        "ffmpeg": deps.ffmpeg_path,
    }
    status.update(deps.optional_status())
    paths["aria2c"] = deps.aria2c_path
    return status, paths


async def _install(deps: DependencyManager, quiet: bool) -> int:
    for name, coro in (
        ("yt-dlp", deps.ensure_ytdlp),
        ("ffmpeg", deps.ensure_ffmpeg),
    ):
        print(f"{name}: checking…", flush=True)
        try:
            path = await coro()
            print(f"{name}: {path}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"{name}: FAILED — {exc}", flush=True)

    # aria2c only has a portable build on Windows; elsewhere it is a package
    # manager tool and its absence is not an error.
    from omnidownloader.core import platform_utils

    if platform_utils.system() == platform_utils.WINDOWS:
        print("aria2c: checking… (torrent backend)", flush=True)
        try:
            path = await deps.ensure_aria2c()
            print(f"aria2c: {path}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"aria2c: FAILED — {exc}", flush=True)

    status, paths = _report(deps)
    # aria2c is only managed on Windows; there it is the sole torrent backend,
    # so its absence is a real failure, while on Linux/macOS it is a package.
    required = ["yt-dlp", "ffmpeg"]
    if platform_utils.system() == platform_utils.WINDOWS:
        required.append("aria2c")

    required_missing = [n for n in required if not status.get(n)]
    if required_missing:
        print(
            "Dependencies incomplete: " + ", ".join(required_missing)
            + " — the app will retry in the background.",
            flush=True,
        )
        for name in required_missing:
            print(f"  {name}: {platform_utils.install_hint(name)}", flush=True)
        return 1

    if not quiet:
        for name, ok in status.items():
            mark = "ok" if ok else "missing"
            print(f"{name:8s} {mark:8s} {paths.get(name, '')}", flush=True)
            if not ok:
                print(f"         optional here — {platform_utils.install_hint(name)}",
                      flush=True)
    print("All dependencies are ready.", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="omnidownloader --install-deps",
        description="Download the tools OmniDownloader manages (yt-dlp, ffmpeg, "
                    "and aria2c on Windows).",
    )
    parser.add_argument("--check", action="store_true",
                        help="only report what is installed; download nothing")
    parser.add_argument("--quiet", action="store_true",
                        help="print one line on success")
    args = parser.parse_args(argv)

    deps = DependencyManager()

    if args.check:
        status, paths = _report(deps)
        for name, ok in status.items():
            print(f"{name:8s} {'ok' if ok else 'missing':8s} {paths.get(name, '')}")
        return 0 if all(status.get(n) for n in ("yt-dlp", "ffmpeg")) else 1

    try:
        return asyncio.run(_install(deps, args.quiet))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Startup auto-update: the git fast-forward path and the auto/manual decision.

The git tests build a real bare origin plus two clones in a temp dir, so they
exercise the actual ``git fetch/pull`` the app runs — no network access, no
mocks.  The UpdateService part stubs the network and the worker so the decision
("install silently" vs "ask in a dialog") can be checked offline.

Usage: .venv/bin/python tools/test_auto_update.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import QCoreApplication  # noqa: E402

from omnidownloader.services import source_updater  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def eq(name: str, got, expected) -> None:
    check(name, got == expected, f"got {got!r}, want {expected!r}")


# ── git plumbing helpers ────────────────────────────────────────────────────
def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, timeout=60,
        env=dict(os.environ,
                 GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="t@example.com",
                 GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="t@example.com"),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr}")
    return proc.stdout.strip()


def make_repos(base: Path) -> tuple[Path, Path, Path]:
    """bare origin, upstream clone (we push here), local clone (we update)."""
    origin = base / "origin.git"
    origin.mkdir(parents=True)
    git(origin, "init", "--bare", "-b", "main")

    upstream = base / "upstream"
    git(base, "clone", str(origin), str(upstream))
    (upstream / "requirements.txt").write_text("PyQt6>=6.6.0\n")
    (upstream / "omnidownloader").mkdir()
    (upstream / "omnidownloader" / "__init__.py").write_text("__version__='1.0.0'\n")
    git(upstream, "add", "-A")
    git(upstream, "commit", "-m", "initial")
    git(upstream, "push", "-u", "origin", "main")

    local = base / "local"
    git(base, "clone", str(origin), str(local))
    return origin, upstream, local


def push_commit(upstream: Path, files: dict[str, str], message: str) -> None:
    for rel, content in files.items():
        path = upstream / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(upstream, "add", "-A")
    git(upstream, "commit", "-m", message)
    git(upstream, "push", "origin", "main")


# ── tests ───────────────────────────────────────────────────────────────────
def test_repo_detection() -> None:
    check("this checkout is recognised as a source checkout",
          source_updater.is_source_checkout() is True)
    root = source_updater.repo_root()
    check("repo_root points at the project",
          root is not None and (root / "omnidownloader").is_dir(), str(root))
    check("launcher_for finds run.sh on this platform",
          source_updater.launcher_for(root) is not None
          if not source_updater.platform_utils.is_windows() else True)
    check("git is available here (the rest of the file needs it)",
          source_updater.git_available() is True)


def test_fast_forward(base: Path) -> None:
    _origin, upstream, local = make_repos(base / "ff")
    before = git(local, "rev-parse", "HEAD")

    res = source_updater.update(local)
    eq("an unchanged remote is up-to-date", res.status, "up-to-date")
    eq("up-to-date keeps the revision", res.new_revision, before)

    push_commit(upstream, {"requirements.txt": "PyQt6>=6.7.0\n"}, "bump deps")
    res2 = source_updater.update(local)
    eq("a newer commit is pulled", res2.status, "updated")
    check("HEAD actually moved",
          git(local, "rev-parse", "HEAD") != before,
          f"{res2.old_revision[:7]} -> {res2.new_revision[:7]}")
    eq("the pulled file is on disk",
       (local / "requirements.txt").read_text(), "PyQt6>=6.7.0\n")
    check("requirements.txt change is detected across the pull",
          source_updater.requirements_changed(res2.old_revision, res2.new_revision, local))

    res3 = source_updater.update(local)
    eq("the second run is a no-op", res3.status, "up-to-date")


def test_dirty_tree_refused(base: Path) -> None:
    _origin, upstream, local = make_repos(base / "dirty")
    (local / "requirements.txt").write_text("local edits\n")
    push_commit(upstream, {"requirements.txt": "PyQt6>=9\n"}, "remote moves on")

    res = source_updater.update(local)
    eq("a modified tracked file blocks the update", res.status, "skipped")
    check("the reason mentions local changes", "local changes" in res.message, res.message)
    eq("the local edit survives",
       (local / "requirements.txt").read_text(), "local edits\n")

    # An untracked file is not a reason to refuse — people drop notes in repos.
    (local / "requirements.txt").write_text("PyQt6>=6.6.0\n")
    (local / "scratch.txt").write_text("junk")
    res2 = source_updater.update(local)
    check("untracked files do not block the update",
          res2.status == "updated", f"{res2.status}: {res2.message}")


def test_diverged_history(base: Path) -> None:
    _origin, upstream, local = make_repos(base / "diverged")
    # A local commit that is not pushed: a fast-forward is impossible.
    (local / "local_only.txt").write_text("mine\n")
    git(local, "add", "-A")
    git(local, "commit", "-m", "local work")
    push_commit(upstream, {"upstream_only.txt": "theirs\n"}, "upstream work")

    res = source_updater.update(local)
    eq("diverged history refuses to merge", res.status, "failed")
    check("the message says fast-forward is impossible",
          "fast-forward" in res.message, res.message)
    check("the local commit is still there", (local / "local_only.txt").exists())


def test_detached_head(base: Path) -> None:
    _origin, _upstream, local = make_repos(base / "detached")
    git(local, "checkout", "--detach", "HEAD")
    res = source_updater.update(local)
    eq("a detached HEAD is not updated", res.status, "skipped")
    check("the reason mentions detached HEAD", "detached" in res.message, res.message)


def test_update_service_decisions() -> None:
    from omnidownloader.services import update_service as us

    info = us.UpdateInfo(version="9.9.9", tag="v9.9.9", html_url="x")

    def build(auto: bool):
        svc = us.UpdateService("1.0.0", auto_update=auto)
        seen = {"available": [], "auto": [], "finished": [], "downloaded": []}
        svc.update_available.connect(lambda i: seen["available"].append(i))
        svc.auto_update_started.connect(lambda i: seen["auto"].append(i))
        svc.auto_update_finished.connect(lambda s, m: seen["finished"].append((s, m)))
        svc.download_update = lambda i: seen["downloaded"].append(i)  # ty: ignore[invalid-assignment]
        return svc, seen

    # (a) auto disabled -> the dialog path is used, nothing installed silently
    svc, seen = build(auto=False)
    svc._auto_requested = True                       # what check_for_updates(auto=True) sets
    svc._on_update_found(info)
    check("auto-update off shows the dialog", len(seen["available"]) == 1)
    check("auto-update off downloads nothing", not seen["downloaded"])

    # (b) auto enabled, frozen build -> asset download + silent install
    svc2, seen2 = build(auto=True)
    real_checkout = source_updater.is_source_checkout
    real_git = source_updater.git_available
    source_updater.is_source_checkout = lambda: False    # ty: ignore[invalid-assignment]
    source_updater.git_available = lambda: True          # ty: ignore[invalid-assignment]
    try:
        svc2._auto_requested = True
        svc2._on_update_found(info)
    finally:
        source_updater.is_source_checkout = real_checkout
        source_updater.git_available = real_git
    check("auto-update on does not show the dialog", not seen2["available"])
    check("auto-update on announces the version", len(seen2["auto"]) == 1)
    check("packaged build downloads the release asset", len(seen2["downloaded"]) == 1)

    # (c) auto enabled, git checkout -> the source worker is used instead
    class _FakeWorker:
        instances: list["_FakeWorker"] = []

        def __init__(self, parent=None):
            self.started = False
            self.handlers = []
            _FakeWorker.instances.append(self)

        class _Sig:
            def __init__(self, owner):
                self._owner = owner

            def connect(self, fn):
                self._owner.handlers.append(fn)

        @property
        def done(self):
            return _FakeWorker._Sig(self)

        def start(self):
            self.started = True

    real_worker = us.SourceUpdateWorker
    real_checkout2 = source_updater.is_source_checkout
    us.SourceUpdateWorker = _FakeWorker                      # ty: ignore[invalid-assignment]
    source_updater.is_source_checkout = lambda: True          # ty: ignore[invalid-assignment]
    try:
        svc3, seen3 = build(auto=True)
        svc3._auto_requested = True
        svc3._on_update_found(info)
        worker = _FakeWorker.instances[-1]
        check("git checkout uses the source updater", worker.started is True)
        check("git checkout does not download a binary", not seen3["downloaded"])
        worker.handlers[0]("updated", "updated abc → def on main")
        eq("the result is reported to the UI",
           seen3["finished"], [("updated", "updated abc → def on main")])
    finally:
        us.SourceUpdateWorker = real_worker                  # ty: ignore[invalid-assignment]
        source_updater.is_source_checkout = real_checkout2


def test_settings_defaults() -> None:
    from omnidownloader.services.config_store import DEFAULTS

    eq("auto-update defaults to on", DEFAULTS.get("auto_update"), True)


def main() -> int:
    app = QCoreApplication(sys.argv)      # Qt signals need an app instance
    assert app is not None
    base = Path(tempfile.mkdtemp(prefix="autoupdate-"))
    try:
        test_repo_detection()
        test_fast_forward(base)
        test_dirty_tree_refused(base)
        test_diverged_history(base)
        test_detached_head(base)
        test_update_service_decisions()
        test_settings_defaults()
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)

    failures = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

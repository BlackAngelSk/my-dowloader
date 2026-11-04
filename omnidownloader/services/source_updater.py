"""Self-update for source checkouts: ``git pull`` instead of a new binary.

A PyInstaller build updates by downloading the release asset (see
``update_service``).  Someone running from a git checkout — which is what
``start.bat`` does on Windows — cannot install a frozen binary over their source
tree, so for them "update from GitHub" means fast-forwarding the checkout.

The rules are deliberately conservative; an auto-update must never be the reason
work disappears:

* never touch a checkout with modified tracked files (untracked files are fine);
* never run on a detached HEAD or a non-default branch;
* only ``--ff-only`` pulls, so local commits are never rebased or merged;
* failures are reported, never raised, and the app simply keeps running.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from omnidownloader.core import platform_utils

logger = logging.getLogger(__name__)

#: git can be slow on a cold Windows box; the network call has its own budget.
_GIT_TIMEOUT = 180


@dataclass
class UpdateResult:
    """Outcome of a source update attempt."""

    status: str          # "updated" | "up-to-date" | "skipped" | "failed"
    message: str = ""
    old_revision: str = ""
    new_revision: str = ""

    @property
    def changed(self) -> bool:
        return self.status == "updated"


def repo_root() -> Optional[Path]:
    """The checkout this code is running from, or ``None``.

    A frozen build has no repository and must go through the release-asset path.
    """
    if getattr(sys, "frozen", False):
        return None
    root = Path(__file__).resolve().parents[2]
    if (root / ".git").exists() and (root / "omnidownloader").is_dir():
        return root
    return None


def is_source_checkout() -> bool:
    return repo_root() is not None


def git_available() -> bool:
    return shutil.which("git") is not None


def _git(root: Path, *args: str, timeout: int = _GIT_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a git command in *root* with output captured."""
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, timeout=timeout,
        **platform_utils.subprocess_kwargs(),
    )


def current_branch(root: Path) -> str:
    """Branch name, or ``""`` for a detached HEAD."""
    proc = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if proc.returncode != 0:
        return ""
    branch = proc.stdout.strip()
    return "" if branch == "HEAD" else branch


def has_local_changes(root: Path) -> bool:
    """True when *tracked* files differ from HEAD (untracked files ignored)."""
    proc = _git(root, "status", "--porcelain", "--untracked-files=no")
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _rev(root: Path, ref: str = "HEAD") -> str:
    proc = _git(root, "rev-parse", ref)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _short(rev: str) -> str:
    return rev[:7] if rev else "?"


def launcher_for(root: Path) -> Optional[Path]:
    """The start script that knows how to reinstall dependencies and run the app."""
    name = "start.bat" if platform_utils.is_windows() else "run.sh"
    candidate = root / name
    return candidate if candidate.is_file() else None


def update(root: Optional[Path] = None, fetch_remote: str = "origin") -> UpdateResult:
    """Fast-forward *root* to its upstream branch.

    Returns an :class:`UpdateResult`; nothing is raised for the normal
    "cannot / must not update" cases.
    """
    root = root or repo_root()
    if root is None:
        return UpdateResult("skipped", "not a git checkout")
    if not git_available():
        return UpdateResult(
            "skipped",
            "git is not installed — install it to enable automatic updates "
            f"({platform_utils.install_hint('git')})",
        )

    branch = current_branch(root)
    if not branch:
        return UpdateResult("skipped", "detached HEAD — not updating automatically")
    if has_local_changes(root):
        return UpdateResult(
            "skipped",
            "local changes present — commit or stash them to enable automatic updates",
        )

    before = _rev(root)
    try:
        proc = _git(root, "fetch", "--prune", fetch_remote)
    except (OSError, subprocess.SubprocessError) as exc:
        return UpdateResult("failed", f"git fetch failed: {exc}")
    if proc.returncode != 0:
        return UpdateResult(
            "failed",
            "git fetch failed: " + (proc.stderr.strip().splitlines() or ["unknown error"])[0],
        )

    upstream = f"{fetch_remote}/{branch}"
    remote_rev = _rev(root, upstream)
    if not remote_rev:
        # Fresh clone without tracking? Fall back to the default branch name.
        remote_rev = _rev(root, f"{fetch_remote}/{_default_branch(root)}")
        upstream = f"{fetch_remote}/{_default_branch(root)}"
    if not remote_rev:
        return UpdateResult("failed", f"could not resolve {upstream}")
    if remote_rev == before:
        return UpdateResult("up-to-date", "already at the latest commit", before, before)

    try:
        proc = _git(root, "pull", "--ff-only", fetch_remote, branch)
    except (OSError, subprocess.SubprocessError) as exc:
        return UpdateResult("failed", f"git pull failed: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip() or "unknown error")
        return UpdateResult(
            "failed",
            "cannot fast-forward (diverged history) — update manually: " + detail.splitlines()[-1],
        )

    after = _rev(root)
    logger.info("Source update: %s -> %s", _short(before), _short(after))
    return UpdateResult(
        "updated",
        f"updated {_short(before)} → {_short(after)} on {branch}",
        before, after,
    )


def _default_branch(root: Path) -> str:
    """The remote's default branch (``main``/``master``); "main" as a guess."""
    proc = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().split("/")[-1]
    for candidate in ("main", "master"):
        if _rev(root, f"origin/{candidate}"):
            return candidate
    return "main"


def requirements_changed(old_rev: str, new_rev: str,
                         root: Optional[Path] = None) -> bool:
    """True when requirements.txt (or pyproject.toml) differs between revisions."""
    root = root or repo_root()
    if root is None or not (old_rev and new_rev):
        return False
    proc = _git(root, "diff", "--name-only", old_rev, new_rev)
    if proc.returncode != 0:
        return False
    changed = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    return bool(changed & {"requirements.txt", "pyproject.toml"})

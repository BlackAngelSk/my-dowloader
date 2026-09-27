"""Persistent store for settings, scheduler rules, proxy config and history.

Nothing was ever saved before: every restart reset the download directory, the
speed limits, the theme, scheduler rules and the whole history list, because
``SettingsPage.set_defaults()`` / ``SchedulerPage.set_rules()`` had no caller.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from omnidownloader.core import platform_utils

logger = logging.getLogger(__name__)

#: How many finished jobs the history keeps.
HISTORY_LIMIT = 200

DEFAULTS: dict[str, Any] = {
    "download_dir": "",
    "max_concurrent": 4,
    "speed_limit_kbs": "",
    "per_task_limit_kbs": "",
    "theme": "auto",
    "proxy": {},
    "kill_switch_enabled": False,
    "kill_switch_interval": 15,
    "tor_enabled": False,
    "scheduler_rules": [],
    "ytdlp_last_check": 0,
    "ytdlp_version": "",
}


class ConfigStore:
    """Reads and writes ``config.json`` and ``history.json`` (see platform_utils)."""

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self._config_path = (Path(config_path) if config_path
                             else platform_utils.config_path())
        self._history_path = self._config_path.parent / "history.json"
        self._data: dict[str, Any] = dict(DEFAULTS)

    # ── settings ────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        return self._config_path

    def load(self) -> dict[str, Any]:
        """Load settings, falling back to defaults for anything unreadable."""
        self._data = dict(DEFAULTS)
        try:
            if self._config_path.is_file():
                loaded = json.loads(self._config_path.read_text())
                if isinstance(loaded, dict):
                    self._data.update(loaded)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read %s (%s) — using defaults",
                           self._config_path, exc)
        return self._data

    def save(self, **changes: Any) -> dict[str, Any]:
        """Merge *changes* into the stored settings and write them out."""
        self._data.update({k: v for k, v in changes.items() if v is not None})
        self._write(self._config_path, self._data)
        return self._data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, DEFAULTS.get(key, default))

    def settings(self) -> dict[str, Any]:
        """The UI-facing subset (what SettingsPanel produces/consumes)."""
        return {
            "download_dir": self.get("download_dir", ""),
            "max_concurrent": self.get("max_concurrent", 4),
            "speed_limit_kbs": self.get("speed_limit_kbs", ""),
            "per_task_limit_kbs": self.get("per_task_limit_kbs", ""),
            "theme": self.get("theme", "auto"),
        }

    # ── history ─────────────────────────────────────────────────

    def load_history(self) -> list[dict[str, Any]]:
        """Return the stored finished jobs, newest first."""
        try:
            if not self._history_path.is_file():
                return []
            loaded = json.loads(self._history_path.read_text())
            if isinstance(loaded, list):
                return [item for item in loaded if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read %s: %s", self._history_path, exc)
        return []

    def save_history(self, jobs: list[dict[str, Any]]) -> None:
        """Persist finished jobs (capped, newest first)."""
        trimmed = list(jobs)[:HISTORY_LIMIT]
        self._write(self._history_path, trimmed)

    # ── internals ───────────────────────────────────────────────

    @staticmethod
    def _write(path: Path, payload: Any) -> None:
        """Atomic write: a crash mid-save must not truncate the file."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2, default=str))
            tmp.replace(path)
        except OSError as exc:
            logger.warning("Could not write %s: %s", path, exc)

    def note_ytdlp_check(self, version: str) -> None:
        """Record that yt-dlp was checked/updated today."""
        self.save(ytdlp_last_check=time.time(), ytdlp_version=version)

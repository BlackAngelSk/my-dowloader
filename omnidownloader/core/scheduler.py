"""Time-Based Bandwidth Scheduler.

Manages automated bandwidth profiles that switch based on the time of day.
Rules define speed caps for different time windows, and the scheduler
continuously evaluates which rule is active and adjusts the global limiter.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import time as dt_time
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)


@dataclass
class SchedulerRule:
    """A single time-based bandwidth rule."""

    name: str
    start_hour: int = 0
    start_minute: int = 0
    end_hour: int = 0
    end_minute: int = 0
    global_speed_limit: float = 0.0  # bytes/s, 0 = unlimited
    per_task_speed_limit: float = 0.0  # bytes/s, 0 = unlimited
    enabled: bool = True

    def matches(self, now: dt_time) -> bool:
        """Check if *now* falls within this rule's time window."""
        if not self.enabled:
            return False
        start = dt_time(self.start_hour, self.start_minute)
        end = dt_time(self.end_hour, self.end_minute)
        if start <= end:
            return start <= now <= end
        else:
            return now >= start or now <= end

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "start_hour": self.start_hour, "start_minute": self.start_minute,
            "end_hour": self.end_hour, "end_minute": self.end_minute,
            "global_speed_limit": self.global_speed_limit,
            "per_task_speed_limit": self.per_task_speed_limit,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SchedulerRule:
        return cls(
            name=data.get("name", "Unnamed"),
            start_hour=data.get("start_hour", 0),
            start_minute=data.get("start_minute", 0),
            end_hour=data.get("end_hour", 0),
            end_minute=data.get("end_minute", 0),
            global_speed_limit=data.get("global_speed_limit", 0.0),
            per_task_speed_limit=data.get("per_task_speed_limit", 0.0),
            enabled=data.get("enabled", True),
        )

    def __repr__(self) -> str:
        return (
            f"<SchedulerRule '{self.name}' "
            f"{self.start_hour:02d}:{self.start_minute:02d}-"
            f"{self.end_hour:02d}:{self.end_minute:02d}>"
        )


class BandwidthScheduler(QObject):
    """Continuously checks the time and applies the matching rule.

    Emits ``rule_changed`` when the active rule changes.
    """

    rule_changed = pyqtSignal(str)

    def __init__(self, bandwidth_manager, check_interval: float = 30.0, parent=None):
        super().__init__(parent)
        self._bw = bandwidth_manager
        self._rules: list[SchedulerRule] = []
        self._active_rule: Optional[SchedulerRule] = None
        self._check_interval = check_interval
        self._timer_task: Optional[asyncio.Task] = None

    @property
    def rules(self) -> list[SchedulerRule]:
        return list(self._rules)

    @property
    def active_rule(self) -> Optional[SchedulerRule]:
        return self._active_rule

    def add_rule(self, rule: SchedulerRule) -> None:
        self._rules.append(rule)
        logger.info("Scheduler rule added: %s", rule)

    def remove_rule(self, name: str) -> None:
        self._rules = [r for r in self._rules if r.name != name]

    def update_rule(self, name: str, updated: SchedulerRule) -> None:
        for i, r in enumerate(self._rules):
            if r.name == name:
                self._rules[i] = updated
                break

    def clear_rules(self) -> None:
        self._rules.clear()
        self._active_rule = None

    async def start(self) -> None:
        if self._timer_task is not None:
            return
        self._timer_task = asyncio.create_task(self._check_loop())
        logger.info("Bandwidth scheduler started (interval=%.0fs)", self._check_interval)

    async def stop(self) -> None:
        if self._timer_task:
            self._timer_task.cancel()
            try:
                await self._timer_task
            except asyncio.CancelledError:
                pass
            self._timer_task = None

    async def _check_loop(self) -> None:
        while True:
            try:
                self._evaluate()
            except Exception:
                logger.exception("Scheduler check failed")
            await asyncio.sleep(self._check_interval)

    def _evaluate(self) -> None:
        from datetime import datetime
        now = datetime.now().time()
        matched = None
        for rule in self._rules:
            if rule.matches(now):
                matched = rule
                break
        if matched is not None and matched is not self._active_rule:
            self._active_rule = matched
            self._bw.set_global_rate(matched.global_speed_limit)
            logger.info("Scheduler: activated rule '%s'", matched.name)
            self.rule_changed.emit(matched.name)
        elif matched is None and self._active_rule is not None:
            self._active_rule = None
            self._bw.set_global_rate(0.0)
            logger.info("Scheduler: no active rule, speed unlimited")
            self.rule_changed.emit("")

    def get_preset_night_day(self) -> list[SchedulerRule]:
        return [
            SchedulerRule(
                name="Night Mode (Unlimited)",
                start_hour=2, start_minute=0, end_hour=8, end_minute=0,
                global_speed_limit=0.0,
            ),
            SchedulerRule(
                name="Day Mode (2 MB/s cap)",
                start_hour=8, start_minute=0, end_hour=2, end_minute=0,
                global_speed_limit=2 * 1024 * 1024,
            ),
        ]

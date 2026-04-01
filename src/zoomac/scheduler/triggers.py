"""Scheduler trigger primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Trigger(Protocol):
    """Protocol for computing the next due time for a job."""

    def next_run_after(self, reference: datetime | None) -> datetime:
        """Return the next run time after the reference timestamp."""


@dataclass(frozen=True, slots=True)
class IntervalTrigger:
    """Run a job on a fixed second-based interval."""

    seconds: int

    def next_run_after(self, reference: datetime | None) -> datetime:
        base = reference or datetime.now(timezone.utc)
        return base + timedelta(seconds=self.seconds)

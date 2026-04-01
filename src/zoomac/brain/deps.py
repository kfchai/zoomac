"""Dependency objects shared across model runtimes."""

from __future__ import annotations

from dataclasses import dataclass

from zoomac.autonomy.policy import AutonomyManager
from zoomac.memory.integration import MemoryManager


@dataclass
class ZoomacDeps:
    """Dependencies injected into every conversation turn."""

    memgate: MemoryManager
    autonomy: AutonomyManager | None = None
    sandbox: object | None = None
    gateway: object | None = None
    goal_engine: object | None = None
    current_goal_id: str | None = None
    current_task_id: str | None = None

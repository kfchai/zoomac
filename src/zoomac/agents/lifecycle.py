"""Sub-agent lifecycle models."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class SubAgentStatus(str, Enum):
    """Lifecycle states for delegated sub-agents."""

    SPAWNED = "spawned"
    RUNNING = "running"
    WAITING = "waiting"
    BLOCKED = "blocked"
    FAILED = "failed"
    DONE = "done"


@dataclass(slots=True)
class SubAgentRecord:
    """Tracked state for one sub-agent run."""

    objective: str
    tool_scope: list[str] = field(default_factory=list)
    parent_goal_id: str | None = None
    parent_task_id: str | None = None
    memory_context: str | None = None
    timeout_seconds: int = 60
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: SubAgentStatus = SubAgentStatus.SPAWNED
    summary: str | None = None
    error: str | None = None
    heartbeat_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime | None = None

    def touch(self) -> None:
        now = datetime.now(timezone.utc)
        self.updated_at = now
        self.heartbeat_at = now

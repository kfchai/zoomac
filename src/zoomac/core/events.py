"""Event types for the Zoomac event queue."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class EventPriority(int, Enum):
    CRITICAL = 0  # User confirmations, system alerts
    HIGH = 10     # Direct user messages
    NORMAL = 20   # Scheduled tasks, background work
    LOW = 30      # Maintenance, consolidation


class EventSource(str, Enum):
    CLI = "cli"
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    DISCORD = "discord"
    SCHEDULER = "scheduler"
    VSCODE = "vscode"
    INTERNAL = "internal"
    SYSTEM = "system"


class Event(BaseModel):
    """Base event in the Zoomac event queue."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    source: EventSource
    priority: EventPriority = EventPriority.NORMAL
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MessageEvent(Event):
    """Inbound message from a user or platform."""

    channel: str = "cli"
    author: str = "user"
    content: str = ""
    attachments: list[str] = Field(default_factory=list)
    reply_to: str | None = None
    metadata: dict = Field(default_factory=dict)


class ScheduleEvent(Event):
    """Triggered by the scheduler."""

    source: EventSource = EventSource.SCHEDULER
    job_name: str = ""
    task: str = ""
    spawn_agent: bool = False
    metadata: dict = Field(default_factory=dict)


class SystemEvent(Event):
    """Internal system events (health, errors, alerts)."""

    source: EventSource = EventSource.SYSTEM
    event_type: str = ""  # "health_check", "error", "alert"
    detail: str = ""


class GoalEvent(Event):
    """Internal event for goal lifecycle updates."""

    source: EventSource = EventSource.INTERNAL
    goal_id: str = ""
    event_type: str = ""  # "task_completed", "task_blocked", "goal_done", "goal_failed"
    task_id: str | None = None
    detail: str = ""

"""In-memory event bus for sub-agent lifecycle updates."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(slots=True)
class SubAgentEvent:
    """One emitted lifecycle event for a sub-agent."""

    agent_id: str
    event_type: str
    message: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SubAgentBus:
    """Simple in-memory bus for publishing and inspecting sub-agent events."""

    def __init__(self) -> None:
        self._events: list[SubAgentEvent] = []

    def publish(self, agent_id: str, event_type: str, message: str) -> SubAgentEvent:
        event = SubAgentEvent(agent_id=agent_id, event_type=event_type, message=message)
        self._events.append(event)
        return event

    def events_for(self, agent_id: str) -> list[SubAgentEvent]:
        return [event for event in self._events if event.agent_id == agent_id]

"""Persistent, priority-ordered event queue backed by SQLite."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from zoomac.core.events import (
    Event,
    EventPriority,
    EventSource,
    MessageEvent,
    ScheduleEvent,
    SystemEvent,
)

# Maps event class names to classes for deserialization
_EVENT_TYPES: dict[str, type[Event]] = {
    "MessageEvent": MessageEvent,
    "ScheduleEvent": ScheduleEvent,
    "SystemEvent": SystemEvent,
    "Event": Event,
}

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS event_queue (
    id TEXT PRIMARY KEY,
    priority INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_queue_status_priority
    ON event_queue(status, priority, timestamp);

CREATE TABLE IF NOT EXISTS dead_letter_queue (
    id TEXT PRIMARY KEY,
    priority INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    last_error TEXT,
    moved_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class EventQueue:
    """SQLite-backed async event queue with priority ordering and dead letter support."""

    MAX_ATTEMPTS = 3

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None
        self._notify: asyncio.Event = asyncio.Event()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
        return self._conn

    def push(self, event: Event) -> None:
        """Add an event to the queue."""
        event_type = type(event).__name__
        payload = event.model_dump_json()
        self.conn.execute(
            "INSERT INTO event_queue (id, priority, timestamp, event_type, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (event.id, event.priority.value, event.timestamp.isoformat(), event_type, payload),
        )
        self.conn.commit()
        self._notify.set()

    async def push_async(self, event: Event) -> None:
        """Async wrapper around push (runs in executor to avoid blocking)."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.push, event)

    def pop(self) -> Event | None:
        """Pop the highest-priority pending event. Returns None if empty."""
        cursor = self.conn.execute(
            "SELECT id, event_type, payload FROM event_queue "
            "WHERE status = 'pending' "
            "ORDER BY priority ASC, timestamp ASC "
            "LIMIT 1"
        )
        row = cursor.fetchone()
        if row is None:
            return None

        event_id, event_type, payload = row["id"], row["event_type"], row["payload"]

        # Mark as processing
        self.conn.execute(
            "UPDATE event_queue SET status = 'processing', attempts = attempts + 1 "
            "WHERE id = ?",
            (event_id,),
        )
        self.conn.commit()

        return self._deserialize(event_type, payload)

    async def pop_async(self) -> Event:
        """Async pop — waits until an event is available."""
        loop = asyncio.get_event_loop()
        while True:
            event = await loop.run_in_executor(None, self.pop)
            if event is not None:
                return event
            # Wait for notification or poll periodically
            self._notify.clear()
            try:
                await asyncio.wait_for(self._notify.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    def complete(self, event_id: str) -> None:
        """Mark an event as successfully processed and remove it."""
        self.conn.execute("DELETE FROM event_queue WHERE id = ?", (event_id,))
        self.conn.commit()

    def fail(self, event_id: str, error: str) -> None:
        """Mark an event as failed. Moves to dead letter queue after MAX_ATTEMPTS."""
        cursor = self.conn.execute(
            "SELECT attempts, priority, timestamp, event_type, payload "
            "FROM event_queue WHERE id = ?",
            (event_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return

        if row["attempts"] >= self.MAX_ATTEMPTS:
            # Move to dead letter queue
            self.conn.execute(
                "INSERT INTO dead_letter_queue (id, priority, timestamp, event_type, payload, attempts, last_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_id, row["priority"], row["timestamp"], row["event_type"], row["payload"], row["attempts"], error),
            )
            self.conn.execute("DELETE FROM event_queue WHERE id = ?", (event_id,))
        else:
            # Re-queue for retry
            self.conn.execute(
                "UPDATE event_queue SET status = 'pending', last_error = ? WHERE id = ?",
                (error, event_id),
            )
        self.conn.commit()

    def pending_count(self) -> int:
        """Number of pending events."""
        cursor = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM event_queue WHERE status = 'pending'"
        )
        return cursor.fetchone()["cnt"]

    def dead_letter_count(self) -> int:
        """Number of events in the dead letter queue."""
        cursor = self.conn.execute("SELECT COUNT(*) as cnt FROM dead_letter_queue")
        return cursor.fetchone()["cnt"]

    def recover_stale(self) -> int:
        """Re-queue events stuck in 'processing' state (e.g., after a crash)."""
        cursor = self.conn.execute(
            "UPDATE event_queue SET status = 'pending' WHERE status = 'processing'"
        )
        self.conn.commit()
        count = cursor.rowcount
        if count > 0:
            self._notify.set()
        return count

    def replay_dead_letter(self, event_id: str) -> bool:
        """Move a dead letter event back to the main queue for retry."""
        cursor = self.conn.execute(
            "SELECT id, priority, timestamp, event_type, payload FROM dead_letter_queue WHERE id = ?",
            (event_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return False

        self.conn.execute(
            "INSERT INTO event_queue (id, priority, timestamp, event_type, payload, status, attempts) "
            "VALUES (?, ?, ?, ?, ?, 'pending', 0)",
            (row["id"], row["priority"], row["timestamp"], row["event_type"], row["payload"]),
        )
        self.conn.execute("DELETE FROM dead_letter_queue WHERE id = ?", (event_id,))
        self.conn.commit()
        self._notify.set()
        return True

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _deserialize(event_type: str, payload: str) -> Event:
        cls = _EVENT_TYPES.get(event_type, Event)
        return cls.model_validate_json(payload)

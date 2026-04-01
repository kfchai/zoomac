"""SQLite persistence for goals, tasks, and credentials."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from zoomac.planner.models import (
    CredentialEntry,
    Goal,
    GoalStatus,
    Task,
    TaskEvent,
    TaskStatus,
    TaskType,
)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    raw_instruction TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planning',
    context TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES goals(id),
    task_type TEXT NOT NULL,
    description TEXT NOT NULL,
    instruction TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    depends_on TEXT NOT NULL DEFAULT '[]',
    result TEXT,
    error TEXT,
    blocker_reason TEXT,
    output_summary TEXT,
    risk_level TEXT,
    approval_mode TEXT,
    approval_provenance TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_goal ON tasks(goal_id, status);

CREATE TABLE IF NOT EXISTS credentials (
    key TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    value BLOB,
    requested_at TEXT NOT NULL,
    provided_at TEXT
);

CREATE TABLE IF NOT EXISTS task_events (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES goals(id),
    task_id TEXT,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_events_goal ON task_events(goal_id, created_at);
"""


class GoalStore:
    """SQLite-backed persistence for goals, tasks, and credentials."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=MEMORY")
            self._conn.execute("PRAGMA synchronous=OFF")
            self._conn.executescript(_SCHEMA)
        return self._conn

    # --- Goals ---

    def save_goal(self, goal: Goal) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO goals (id, raw_instruction, status, context, source, channel, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                goal.id,
                goal.raw_instruction,
                goal.status.value,
                json.dumps(goal.context),
                goal.source,
                goal.channel,
                goal.created_at.isoformat(),
                goal.updated_at.isoformat() if goal.updated_at else None,
            ),
        )
        self.conn.commit()

    def load_goal(self, goal_id: str) -> Goal | None:
        row = self.conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if row is None:
            return None
        tasks = self._load_tasks_for_goal(goal_id)
        return Goal(
            id=row["id"],
            raw_instruction=row["raw_instruction"],
            status=GoalStatus(row["status"]),
            context=json.loads(row["context"]),
            source=row["source"],
            channel=row["channel"],
            tasks=tasks,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
        )

    def update_goal_status(self, goal_id: str, status: GoalStatus) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE goals SET status = ?, updated_at = ? WHERE id = ?",
            (status.value, now, goal_id),
        )
        self.conn.commit()

    def update_goal_context(self, goal_id: str, context: dict) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE goals SET context = ?, updated_at = ? WHERE id = ?",
            (json.dumps(context), now, goal_id),
        )
        self.conn.commit()

    def list_goals(self, status: GoalStatus | None = None) -> list[Goal]:
        if status:
            rows = self.conn.execute(
                "SELECT id FROM goals WHERE status = ? ORDER BY created_at DESC",
                (status.value,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id FROM goals ORDER BY created_at DESC"
            ).fetchall()
        return [self.load_goal(row["id"]) for row in rows]

    def find_blocked_goal(self, source: str, channel: str) -> Goal | None:
        row = self.conn.execute(
            "SELECT id FROM goals WHERE status = 'blocked' AND source = ? AND channel = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (source, channel),
        ).fetchone()
        if row is None:
            return None
        return self.load_goal(row["id"])

    # --- Tasks ---

    def save_task(self, task: Task) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO tasks "
            "(id, goal_id, task_type, description, instruction, status, depends_on, "
            "result, error, blocker_reason, output_summary, risk_level, approval_mode, approval_provenance, "
            "attempts, max_attempts, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task.id,
                task.goal_id,
                task.task_type.value,
                task.description,
                task.instruction,
                task.status.value,
                json.dumps(task.depends_on),
                task.result,
                task.error,
                task.blocker_reason,
                task.output_summary,
                task.risk_level,
                task.approval_mode,
                task.approval_provenance,
                task.attempts,
                task.max_attempts,
                task.created_at.isoformat(),
                task.updated_at.isoformat() if task.updated_at else None,
            ),
        )
        self.conn.commit()

    def update_task(self, task_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        # Serialize enums and lists
        for key, val in fields.items():
            if isinstance(val, (TaskStatus, TaskType)):
                fields[key] = val.value
            elif isinstance(val, list):
                fields[key] = json.dumps(val)
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [task_id]
        self.conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        self.conn.commit()

    def get_ready_tasks(self, goal_id: str) -> list[Task]:
        """Return tasks whose dependencies are all DONE and status is PENDING."""
        all_tasks = self._load_tasks_for_goal(goal_id)
        done_ids = {t.id for t in all_tasks if t.status == TaskStatus.DONE}
        ready = []
        for task in all_tasks:
            if task.status != TaskStatus.PENDING:
                continue
            if all(dep_id in done_ids for dep_id in task.depends_on):
                ready.append(task)
        return ready

    def _load_tasks_for_goal(self, goal_id: str) -> list[Task]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE goal_id = ? ORDER BY created_at ASC",
            (goal_id,),
        ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            goal_id=row["goal_id"],
            task_type=TaskType(row["task_type"]),
            description=row["description"],
            instruction=row["instruction"],
            status=TaskStatus(row["status"]),
            depends_on=json.loads(row["depends_on"]),
            result=row["result"],
            error=row["error"],
            blocker_reason=row["blocker_reason"],
            output_summary=row["output_summary"] if "output_summary" in row.keys() else None,
            risk_level=row["risk_level"],
            approval_mode=row["approval_mode"],
            approval_provenance=row["approval_provenance"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
        )

    def recover_running(self) -> int:
        """Reset tasks stuck in RUNNING back to PENDING (crash recovery)."""
        cursor = self.conn.execute(
            "UPDATE tasks SET status = 'pending' WHERE status = 'running'"
        )
        self.conn.commit()
        return cursor.rowcount

    # --- Task events ---

    def add_task_event(
        self,
        goal_id: str,
        event_type: str,
        message: str,
        task_id: str | None = None,
    ) -> TaskEvent:
        event = TaskEvent(
            goal_id=goal_id,
            task_id=task_id,
            event_type=event_type,
            message=message,
        )
        self.conn.execute(
            "INSERT INTO task_events (id, goal_id, task_id, event_type, message, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                event.id,
                event.goal_id,
                event.task_id,
                event.event_type,
                event.message,
                event.created_at.isoformat(),
            ),
        )
        self.conn.commit()
        return event

    def list_task_events(self, goal_id: str, limit: int = 100) -> list[TaskEvent]:
        rows = self.conn.execute(
            "SELECT * FROM task_events WHERE goal_id = ? ORDER BY created_at ASC LIMIT ?",
            (goal_id, limit),
        ).fetchall()
        return [
            TaskEvent(
                id=row["id"],
                goal_id=row["goal_id"],
                task_id=row["task_id"],
                event_type=row["event_type"],
                message=row["message"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    # --- Credentials ---

    def save_credential(self, entry: CredentialEntry) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO credentials (key, description, value, requested_at, provided_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                entry.key,
                entry.description,
                entry.value.encode() if entry.value else None,
                entry.requested_at.isoformat(),
                entry.provided_at.isoformat() if entry.provided_at else None,
            ),
        )
        self.conn.commit()

    def get_credential(self, key: str) -> CredentialEntry | None:
        row = self.conn.execute(
            "SELECT * FROM credentials WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return CredentialEntry(
            key=row["key"],
            description=row["description"],
            value=row["value"].decode() if row["value"] else None,
            requested_at=datetime.fromisoformat(row["requested_at"]),
            provided_at=datetime.fromisoformat(row["provided_at"]) if row["provided_at"] else None,
        )

    def list_pending_credentials(self) -> list[CredentialEntry]:
        rows = self.conn.execute(
            "SELECT * FROM credentials WHERE provided_at IS NULL"
        ).fetchall()
        return [
            CredentialEntry(
                key=row["key"],
                description=row["description"],
                value=None,
                requested_at=datetime.fromisoformat(row["requested_at"]),
            )
            for row in rows
        ]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

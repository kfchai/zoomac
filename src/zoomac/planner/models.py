"""Pydantic schemas for goal planning and task execution."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class TaskType(str, Enum):
    LLM_REASON = "llm_reason"
    READ_FILE = "read_file"
    WRITE_CODE = "write_code"
    SANDBOX_EXEC = "sandbox_exec"
    SEND_MESSAGE = "send_message"
    WAIT_INPUT = "wait_input"
    WEB_LOOKUP = "web_lookup"
    MEMORY_QUERY = "memory_query"
    CREDENTIAL_REQUEST = "credential_request"
    SUBAGENT_SPAWN = "subagent_spawn"


class TaskStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class GoalStatus(str, Enum):
    DRAFT = "draft"
    PLANNING = "planning"
    APPROVED = "approved"
    EXECUTING = "executing"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"


class Task(BaseModel):
    """A single executable task within a goal."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    goal_id: str
    task_type: TaskType
    description: str
    instruction: str
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = Field(default_factory=list)
    result: str | None = None
    error: str | None = None
    blocker_reason: str | None = None
    output_summary: str | None = None
    risk_level: str | None = None
    approval_mode: str | None = None
    approval_provenance: str | None = None
    attempts: int = 0
    max_attempts: int = 3
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime | None = None


class Goal(BaseModel):
    """A high-level goal decomposed into tasks."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    raw_instruction: str
    status: GoalStatus = GoalStatus.PLANNING
    tasks: list[Task] = Field(default_factory=list)
    context: dict = Field(default_factory=dict)
    source: str = "cli"
    channel: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime | None = None


class TaskEvent(BaseModel):
    """User-visible task progress event."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    goal_id: str
    task_id: str | None = None
    event_type: str
    message: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TaskSpec(BaseModel):
    """Single task specification produced by the LLM planner."""

    task_type: TaskType
    description: str
    instruction: str
    depends_on_indices: list[int] = Field(default_factory=list)


class PlanDecomposition(BaseModel):
    """LLM output for goal decomposition into tasks."""

    reasoning: str
    tasks: list[TaskSpec]


class ReplanDecision(BaseModel):
    """LLM output when deciding how to handle a failed task."""

    action: Literal["retry", "skip", "replace", "abort"]
    reasoning: str
    replacement_tasks: list[TaskSpec] | None = None


class CredentialEntry(BaseModel):
    """A credential requested or stored by the agent."""

    key: str
    description: str
    value: str | None = None
    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    provided_at: datetime | None = None

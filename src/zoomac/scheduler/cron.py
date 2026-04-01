"""Queue-backed scheduler service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from zoomac.core.events import EventPriority, ScheduleEvent
from zoomac.core.queue import EventQueue
from zoomac.scheduler.triggers import IntervalTrigger, Trigger


@dataclass(slots=True)
class ScheduledJob:
    """One scheduled job definition."""

    name: str
    task: str
    trigger: Trigger
    spawn_agent: bool = False
    metadata: dict = field(default_factory=dict)
    priority: EventPriority = EventPriority.NORMAL
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None

    def ensure_next_run(self) -> None:
        if self.next_run_at is None:
            self.next_run_at = self.trigger.next_run_after(None)


class SchedulerService:
    """Background scheduler that pushes ScheduleEvents into the event queue."""

    def __init__(self, queue: EventQueue, *, poll_interval: float = 1.0) -> None:
        self._queue = queue
        self._poll_interval = poll_interval
        self._jobs: dict[str, ScheduledJob] = {}
        self._task: asyncio.Task | None = None
        self._running = False

    def register(self, job: ScheduledJob) -> None:
        if job.name in self._jobs:
            raise ValueError(f"Scheduled job '{job.name}' is already registered.")
        job.ensure_next_run()
        self._jobs[job.name] = job

    def register_default_jobs(self) -> None:
        defaults = [
            ScheduledJob(
                name="memory_consolidate",
                task="memory_consolidate",
                trigger=IntervalTrigger(seconds=3600),
                priority=EventPriority.LOW,
            ),
            ScheduledJob(
                name="health_check",
                task="health_check",
                trigger=IntervalTrigger(seconds=300),
                priority=EventPriority.LOW,
            ),
            ScheduledJob(
                name="retry_dead_letters",
                task="retry_dead_letters",
                trigger=IntervalTrigger(seconds=600),
                priority=EventPriority.LOW,
            ),
        ]
        for job in defaults:
            if job.name not in self._jobs:
                self.register(job)

    def list_jobs(self) -> list[ScheduledJob]:
        return list(self._jobs.values())

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="zoomac-scheduler")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def tick(self, now: datetime | None = None) -> int:
        """Run one scheduler pass and enqueue any due jobs."""
        current = now or datetime.now(timezone.utc)
        emitted = 0
        for job in self._jobs.values():
            job.ensure_next_run()
            if job.next_run_at is None or job.next_run_at > current:
                continue
            self._queue.push(
                ScheduleEvent(
                    priority=job.priority,
                    job_name=job.name,
                    task=job.task,
                    spawn_agent=job.spawn_agent,
                    metadata=job.metadata,
                )
            )
            job.last_run_at = current
            job.next_run_at = job.trigger.next_run_after(current)
            emitted += 1
        return emitted

    async def _run_loop(self) -> None:
        while self._running:
            await self.tick()
            await asyncio.sleep(self._poll_interval)

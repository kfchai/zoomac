"""Tests for scheduler integration and scheduled background work."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memgate"))

from zoomac.core.config import ZoomacSettings
from zoomac.core.events import EventSource, ScheduleEvent
from zoomac.core.loop import CoreLoop
from zoomac.core.queue import EventQueue
from zoomac.scheduler import IntervalTrigger, ScheduledJob, SchedulerService
from zoomac.testing import FakeRuntimeProvider


@pytest.mark.asyncio
async def test_scheduler_service_enqueues_due_job(tmp_path):
    queue = EventQueue(tmp_path / "events.db")
    scheduler = SchedulerService(queue, poll_interval=0.01)
    scheduler.register(
        ScheduledJob(
            name="health_check",
            task="health_check",
            trigger=IntervalTrigger(seconds=60),
            metadata={"kind": "health"},
        )
    )

    due_time = datetime.now(timezone.utc).replace(microsecond=0)
    scheduler.list_jobs()[0].next_run_at = due_time
    emitted = await scheduler.tick(now=due_time)

    assert emitted == 1
    event = queue.pop()
    assert isinstance(event, ScheduleEvent)
    assert event.job_name == "health_check"
    assert event.metadata == {"kind": "health"}
    queue.close()


@pytest.mark.asyncio
async def test_core_loop_handles_memory_consolidation_schedule(tmp_path):
    settings = ZoomacSettings(project_dir=tmp_path)
    core = CoreLoop(settings, runtime_provider=FakeRuntimeProvider())
    try:
        result = await core.handle_schedule(
            ScheduleEvent(job_name="memory_consolidate", task="memory_consolidate")
        )
        assert "memory consolidated" in result
    finally:
        core.shutdown()


@pytest.mark.asyncio
async def test_core_loop_handles_dead_letter_retry_schedule(tmp_path):
    settings = ZoomacSettings(project_dir=tmp_path)
    core = CoreLoop(settings, runtime_provider=FakeRuntimeProvider())
    try:
        event = ScheduleEvent(
            job_name="retry_dead_letters",
            task="retry_dead_letters",
        )
        message = await core.handle_schedule(event)
        assert "replayed" in message
    finally:
        core.shutdown()


@pytest.mark.asyncio
async def test_core_loop_scheduled_goal_uses_goal_engine(tmp_path):
    settings = ZoomacSettings(project_dir=tmp_path)
    core = CoreLoop(settings, runtime_provider=FakeRuntimeProvider())
    try:
        event = ScheduleEvent(
            job_name="nightly_goal",
            task="build release notes",
            spawn_agent=True,
            metadata={"goal_instruction": "build release notes"},
        )
        message = await core.handle_schedule(event)
        assert "goal queued" in message
        assert core._goal_tasks
    finally:
        core.shutdown()

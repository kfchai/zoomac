"""Phase 2 tests — Event queue: persistence, priority ordering, dead letter, crash recovery."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memgate"))

from zoomac.core.events import (
    Event,
    EventPriority,
    EventSource,
    MessageEvent,
    ScheduleEvent,
    SystemEvent,
)
from zoomac.core.queue import EventQueue


@pytest.fixture
def queue(tmp_path):
    q = EventQueue(tmp_path / "test_events.db")
    yield q
    q.close()


def test_push_and_pop(queue):
    """Basic push and pop."""
    event = MessageEvent(
        source=EventSource.CLI,
        content="hello",
    )
    queue.push(event)
    assert queue.pending_count() == 1

    popped = queue.pop()
    assert popped is not None
    assert popped.id == event.id
    assert isinstance(popped, MessageEvent)
    assert popped.content == "hello"


def test_pop_empty_returns_none(queue):
    """Pop on empty queue returns None."""
    assert queue.pop() is None


def test_priority_ordering(queue):
    """Events are popped in priority order (lower value = higher priority)."""
    low = MessageEvent(source=EventSource.CLI, content="low", priority=EventPriority.LOW)
    high = MessageEvent(source=EventSource.CLI, content="high", priority=EventPriority.HIGH)
    critical = MessageEvent(source=EventSource.CLI, content="critical", priority=EventPriority.CRITICAL)
    normal = MessageEvent(source=EventSource.CLI, content="normal", priority=EventPriority.NORMAL)

    # Push in random order
    queue.push(low)
    queue.push(normal)
    queue.push(critical)
    queue.push(high)

    # Should come out in priority order
    e1 = queue.pop()
    e2 = queue.pop()
    e3 = queue.pop()
    e4 = queue.pop()

    assert e1.content == "critical"
    assert e2.content == "high"
    assert e3.content == "normal"
    assert e4.content == "low"


def test_fifo_within_same_priority(queue):
    """Events with same priority are popped FIFO by timestamp."""
    from datetime import datetime, timezone, timedelta

    t1 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)

    e1 = MessageEvent(source=EventSource.CLI, content="first", timestamp=t1)
    e2 = MessageEvent(source=EventSource.CLI, content="second", timestamp=t2)

    queue.push(e2)
    queue.push(e1)

    popped1 = queue.pop()
    popped2 = queue.pop()

    assert popped1.content == "first"
    assert popped2.content == "second"


def test_complete_removes_event(queue):
    """Completing an event removes it from the queue."""
    event = MessageEvent(source=EventSource.CLI, content="done")
    queue.push(event)
    popped = queue.pop()
    queue.complete(popped.id)

    assert queue.pending_count() == 0
    assert queue.pop() is None


def test_fail_requeues_event(queue):
    """Failing an event re-queues it for retry (under MAX_ATTEMPTS)."""
    event = MessageEvent(source=EventSource.CLI, content="retry me")
    queue.push(event)

    # First attempt
    popped = queue.pop()
    queue.fail(popped.id, "error 1")

    # Should be back in pending
    assert queue.pending_count() == 1

    # Second attempt
    popped = queue.pop()
    queue.fail(popped.id, "error 2")
    assert queue.pending_count() == 1


def test_fail_moves_to_dead_letter_after_max_attempts(queue):
    """After MAX_ATTEMPTS failures, event moves to dead letter queue."""
    event = MessageEvent(source=EventSource.CLI, content="doomed")
    queue.push(event)

    for i in range(EventQueue.MAX_ATTEMPTS):
        popped = queue.pop()
        assert popped is not None
        queue.fail(popped.id, f"error {i + 1}")

    # Should be in dead letter now
    assert queue.pending_count() == 0
    assert queue.dead_letter_count() == 1


def test_replay_dead_letter(queue):
    """Dead letter events can be replayed back to the main queue."""
    event = MessageEvent(source=EventSource.CLI, content="revive me")
    queue.push(event)

    for i in range(EventQueue.MAX_ATTEMPTS):
        popped = queue.pop()
        queue.fail(popped.id, f"error {i + 1}")

    assert queue.dead_letter_count() == 1
    assert queue.pending_count() == 0

    # Replay it
    result = queue.replay_dead_letter(event.id)
    assert result is True
    assert queue.dead_letter_count() == 0
    assert queue.pending_count() == 1

    # Can pop it again
    popped = queue.pop()
    assert popped.content == "revive me"


def test_recover_stale(queue):
    """Events stuck in 'processing' are recovered to 'pending'."""
    event = MessageEvent(source=EventSource.CLI, content="stuck")
    queue.push(event)
    queue.pop()  # Moves to 'processing'

    assert queue.pending_count() == 0

    # Simulate crash recovery
    recovered = queue.recover_stale()
    assert recovered == 1
    assert queue.pending_count() == 1


def test_persistence_across_connections(tmp_path):
    """Events survive database close and reopen."""
    db_path = tmp_path / "persist_test.db"

    # Push with first connection
    q1 = EventQueue(db_path)
    event = MessageEvent(source=EventSource.CLI, content="I persist")
    q1.push(event)
    q1.close()

    # Pop with second connection
    q2 = EventQueue(db_path)
    assert q2.pending_count() == 1
    popped = q2.pop()
    assert popped is not None
    assert popped.content == "I persist"
    q2.close()


def test_crash_recovery_scenario(tmp_path):
    """Simulate crash: push, pop (processing), close, reopen, recover."""
    db_path = tmp_path / "crash_test.db"

    # Session 1: push and start processing
    q1 = EventQueue(db_path)
    e1 = MessageEvent(source=EventSource.CLI, content="was processing")
    e2 = MessageEvent(source=EventSource.CLI, content="was pending")
    q1.push(e1)
    q1.push(e2)
    q1.pop()  # e1 moves to 'processing'
    q1.close()  # "crash"

    # Session 2: recover
    q2 = EventQueue(db_path)
    recovered = q2.recover_stale()
    assert recovered == 1  # e1 recovered
    assert q2.pending_count() == 2  # both available

    # Both can be popped
    popped1 = q2.pop()
    popped2 = q2.pop()
    assert {popped1.content, popped2.content} == {"was processing", "was pending"}
    q2.close()


def test_different_event_types(queue):
    """Queue handles all event types correctly."""
    msg = MessageEvent(source=EventSource.TELEGRAM, content="hi", channel="chat_123")
    sched = ScheduleEvent(job_name="daily", task="summarize")
    sys_evt = SystemEvent(event_type="health_check", detail="ok")

    queue.push(msg)
    queue.push(sched)
    queue.push(sys_evt)

    e1 = queue.pop()
    e2 = queue.pop()
    e3 = queue.pop()

    types = {type(e).__name__ for e in [e1, e2, e3]}
    assert "MessageEvent" in types
    assert "ScheduleEvent" in types
    assert "SystemEvent" in types


def test_schedule_event_preserved(queue):
    """ScheduleEvent fields survive serialization round-trip."""
    event = ScheduleEvent(job_name="consolidate", task="MemGate.consolidate()", spawn_agent=True)
    queue.push(event)
    popped = queue.pop()
    assert isinstance(popped, ScheduleEvent)
    assert popped.job_name == "consolidate"
    assert popped.task == "MemGate.consolidate()"
    assert popped.spawn_agent is True


def test_message_event_preserved(queue):
    """MessageEvent fields survive serialization round-trip."""
    event = MessageEvent(
        source=EventSource.DISCORD,
        channel="general",
        author="alice",
        content="test message",
        reply_to="msg_123",
        metadata={"role": "admin"},
    )
    queue.push(event)
    popped = queue.pop()
    assert isinstance(popped, MessageEvent)
    assert popped.source == EventSource.DISCORD
    assert popped.channel == "general"
    assert popped.author == "alice"
    assert popped.content == "test message"
    assert popped.reply_to == "msg_123"
    assert popped.metadata == {"role": "admin"}

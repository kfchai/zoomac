"""Phase 1 tests — Agent runtime, tools, and end-to-end core loop."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memgate"))

from zoomac.brain.deps import ZoomacDeps
from zoomac.brain.memory_extract import AgentResponse, MemoryExtract, EvolutionSignal
from zoomac.brain.runtime_agent import build_conversation_runtime
from zoomac.core.loop import CoreLoop
from zoomac.core.config import ZoomacSettings
from zoomac.core.events import EventSource, MessageEvent
from zoomac.memory.integration import MemoryManager
from zoomac.testing import FakeRuntimeProvider


def test_build_conversation_runtime():
    """Conversation runtime is created successfully."""
    runtime = build_conversation_runtime("claude-sonnet-4-20250514")
    assert runtime is not None


def test_runtime_has_tools():
    """Runtime exposes the built-in tool definitions."""
    runtime = build_conversation_runtime("claude-sonnet-4-20250514")
    tool_names = set(runtime.tool_handlers.keys())
    assert "search_memory" in tool_names
    assert "check_facts" in tool_names
    assert "memory_status" in tool_names
    assert "write_code" in tool_names
    assert "execute_in_sandbox" in tool_names


def test_zoomac_deps(tmp_path):
    """ZoomacDeps can be constructed with a MemoryManager."""
    mm = MemoryManager(project_dir=str(tmp_path))
    deps = ZoomacDeps(memgate=mm)
    assert deps.memgate is mm
    mm.close()


def test_core_loop_init(tmp_path):
    """CoreLoop initializes with a FakeRuntimeProvider."""
    settings = ZoomacSettings(project_dir=tmp_path)
    core = CoreLoop(settings, runtime_provider=FakeRuntimeProvider())
    assert core.conversation_runtime is not None
    assert core.runner is not None
    assert core.planner_runtime is not None
    assert core.memory is not None
    core.shutdown()


@pytest.mark.asyncio
async def test_handle_message_returns_response(tmp_path):
    """handle_message processes a MessageEvent and returns a string."""
    settings = ZoomacSettings(project_dir=tmp_path)
    core = CoreLoop(settings, runtime_provider=FakeRuntimeProvider())

    event = MessageEvent(
        source=EventSource.CLI,
        channel="cli",
        author="user",
        content="Hello Zoomac!",
    )

    response = await core.handle_message(event)
    assert isinstance(response, str)
    assert len(response) > 0
    core.shutdown()


@pytest.mark.asyncio
async def test_memory_ingestion_on_handle_message(tmp_path):
    """Memory is ingested when the runtime says worth_remembering=True."""
    settings = ZoomacSettings(project_dir=tmp_path)
    from zoomac.testing import FakeConversationRuntime

    runtime = FakeConversationRuntime(
        response=AgentResponse(
            message="Noted.",
            memory=MemoryExtract(
                worth_remembering=True,
                content="User's favorite color is blue.",
            ),
            evolution=EvolutionSignal(),
        )
    )
    provider = FakeRuntimeProvider(conversation=runtime)
    core = CoreLoop(settings, runtime_provider=provider)

    event = MessageEvent(
        source=EventSource.CLI,
        channel="cli",
        author="user",
        content="Remember that my favorite color is blue.",
    )
    await core.handle_message(event)
    core.shutdown()


def test_message_event_creation():
    """MessageEvent can be created for the core loop."""
    event = MessageEvent(
        source=EventSource.CLI,
        channel="cli",
        author="user",
        content="What is the meaning of life?",
    )
    assert event.content == "What is the meaning of life?"
    assert event.source == EventSource.CLI

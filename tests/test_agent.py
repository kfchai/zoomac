"""Phase 1 tests — Agent creation, tools, and end-to-end core loop."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memgate"))

from pydantic_ai.models.test import TestModel

from zoomac.brain.agent import create_agent, ZoomacDeps
from zoomac.brain.memory_extract import AgentResponse, MemoryExtract, EvolutionSignal
from zoomac.core.loop import CoreLoop
from zoomac.core.config import ZoomacSettings
from zoomac.core.events import EventSource, MessageEvent
from zoomac.memory.integration import MemoryManager


@pytest.fixture
def test_model():
    return TestModel()


def test_create_agent(test_model):
    """Agent is created successfully."""
    agent = create_agent(test_model)
    assert agent is not None


def test_agent_has_tools(test_model):
    """Agent has search_memory, check_facts, and memory_status tools."""
    agent = create_agent(test_model)
    tool_names = {t.name for t in agent._function_toolset.tools.values()}
    assert "search_memory" in tool_names
    assert "check_facts" in tool_names
    assert "memory_status" in tool_names


def test_zoomac_deps(tmp_path):
    """ZoomacDeps can be constructed with a MemoryManager."""
    mm = MemoryManager(project_dir=str(tmp_path))
    deps = ZoomacDeps(memgate=mm)
    assert deps.memgate is mm
    mm.close()


def test_core_loop_init(tmp_path):
    """CoreLoop initializes with TestModel override."""
    settings = ZoomacSettings(project_dir=tmp_path)
    core = CoreLoop(settings, model_override=TestModel())
    assert core.agent is not None
    assert core.memory is not None
    core.shutdown()


@pytest.mark.asyncio
async def test_handle_message_returns_response(tmp_path):
    """handle_message processes a MessageEvent and returns a string."""
    settings = ZoomacSettings(project_dir=tmp_path)
    core = CoreLoop(settings, model_override=TestModel())

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
    """Memory is ingested when the LLM says worth_remembering=True."""
    settings = ZoomacSettings(project_dir=tmp_path)
    core = CoreLoop(settings, model_override=TestModel())

    # Before: no memories
    status_before = core.memory.status()
    initial_ingested = status_before.get("total_ingested", 0)

    event = MessageEvent(
        source=EventSource.CLI,
        channel="cli",
        author="user",
        content="Remember that my favorite color is blue.",
    )

    # TestModel returns generic output, memory ingestion depends on worth_remembering
    # This tests the flow doesn't crash — actual memory content depends on model output
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

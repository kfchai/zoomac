"""Tests for the registry-backed tool layer."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memgate"))

from zoomac.tools import (
    ToolCall,
    ToolCapabilities,
    ToolDefinition,
    ToolExecutor,
    ToolRegistry,
    ToolSpec,
)
from zoomac.tools.memory import build_memory_tool_registry
from zoomac.tools.planning import build_planning_tool_registry


def test_memory_registry_exposes_expected_tools():
    registry = build_memory_tool_registry()
    assert registry.names() == ["search_memory", "check_facts", "memory_status"]


def test_planning_registry_exposes_expected_tools():
    registry = build_planning_tool_registry()
    assert registry.names() == [
        "write_code",
        "execute_in_sandbox",
        "request_credential",
        "store_goal_context",
        "send_user_message",
    ]


def test_registry_select_filters_by_capability():
    registry = build_planning_tool_registry()
    filtered = registry.select(lambda tool: tool.spec.capabilities.writes_project)
    assert filtered.names() == ["write_code"]


@pytest.mark.asyncio
async def test_executor_runs_registered_tool():
    async def echo(value: str) -> str:
        return value

    registry = ToolRegistry(name="test")
    registry.register(
        ToolDefinition(
            spec=ToolSpec(
                name="echo",
                description="Echo a value.",
                capabilities=ToolCapabilities(read_only=True, supports_parallel=True),
            ),
            handler=echo,
        )
    )

    result = await ToolExecutor(registry).execute(
        ToolCall(name="echo", kwargs={"value": "hello"})
    )
    assert result.name == "echo"
    assert result.output == "hello"

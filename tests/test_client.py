"""Tests for the Anthropic client wrapper."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memgate"))

import pytest

from zoomac.brain.client import tool_definitions_to_anthropic, tool_handlers_from_registry
from zoomac.brain.deps import ZoomacDeps
from zoomac.tools.base import ToolCapabilities, ToolDefinition, ToolSpec
from zoomac.tools.registry import ToolRegistry


async def _sample_tool(deps: ZoomacDeps, query: str) -> str:
    return f"result for {query}"


async def _tool_with_default(deps: ZoomacDeps, name: str, count: int = 5) -> str:
    return f"{name}:{count}"


def _build_sample_registry() -> ToolRegistry:
    reg = ToolRegistry(name="test")
    reg.register(
        ToolDefinition(
            spec=ToolSpec(
                name="sample",
                description="A sample tool.",
                capabilities=ToolCapabilities(read_only=True),
            ),
            handler=_sample_tool,
        )
    )
    reg.register(
        ToolDefinition(
            spec=ToolSpec(
                name="with_default",
                description="Tool with a default param.",
            ),
            handler=_tool_with_default,
        )
    )
    return reg


def test_tool_definitions_to_anthropic_format():
    registry = _build_sample_registry()
    defs = tool_definitions_to_anthropic(registry)

    assert len(defs) == 2

    sample = next(d for d in defs if d["name"] == "sample")
    assert sample["description"] == "A sample tool."
    assert sample["input_schema"]["type"] == "object"
    assert "query" in sample["input_schema"]["properties"]
    assert sample["input_schema"]["required"] == ["query"]

    with_default = next(d for d in defs if d["name"] == "with_default")
    assert "name" in with_default["input_schema"]["required"]
    assert "count" not in with_default["input_schema"]["required"]
    assert with_default["input_schema"]["properties"]["count"]["default"] == 5


def test_tool_handlers_from_registry():
    registry = _build_sample_registry()
    handlers = tool_handlers_from_registry(registry)

    assert "sample" in handlers
    assert "with_default" in handlers
    assert handlers["sample"] is _sample_tool

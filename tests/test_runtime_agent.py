"""Tests for conversation runtime construction."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memgate"))

import pytest

from zoomac.brain.runtime_agent import build_conversation_runtime


def test_build_conversation_runtime_registers_builtin_tools():
    runtime = build_conversation_runtime("claude-sonnet-4-20250514")
    tool_names = set(runtime.tool_handlers.keys())

    assert "search_memory" in tool_names
    assert "check_facts" in tool_names
    assert "memory_status" in tool_names
    assert "write_code" in tool_names
    assert "execute_in_sandbox" in tool_names


def test_build_conversation_runtime_has_tool_defs():
    runtime = build_conversation_runtime("claude-sonnet-4-20250514")
    # Anthropic format tool defs should be populated
    assert len(runtime.tool_defs) > 0
    for td in runtime.tool_defs:
        assert "name" in td
        assert "description" in td
        assert "input_schema" in td


def test_build_conversation_runtime_has_system_prompt():
    runtime = build_conversation_runtime("claude-sonnet-4-20250514")
    assert "Zoomac" in runtime.system_prompt

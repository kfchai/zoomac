"""Tests for model runtime composition."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memgate"))

from zoomac.brain.deps import ZoomacDeps
from zoomac.brain.memory_extract import AgentResponse, EvolutionSignal, MemoryExtract
from zoomac.brain.runtime import RuntimeBundle
from zoomac.testing import FakeConversationRuntime, FakePlannerRuntime


class _FakeMemory:
    def retrieve_context(self, *_args, **_kwargs):
        return ""

    def facts(self, *_args, **_kwargs):
        return []

    def status(self):
        return {}


@pytest.mark.asyncio
async def test_runtime_bundle_wires_conversation_and_planner():
    conv = FakeConversationRuntime()
    planner = FakePlannerRuntime()
    bundle = RuntimeBundle(
        conversation_runtime=conv,
        planner_runtime=planner,
    )

    response = await bundle.conversation_runtime.run(
        "hello",
        deps=ZoomacDeps(memgate=_FakeMemory()),  # type: ignore[arg-type]
    )

    assert bundle.planner_runtime is not None
    assert isinstance(response, AgentResponse)
    assert isinstance(response.memory, MemoryExtract)
    assert isinstance(response.evolution, EvolutionSignal)

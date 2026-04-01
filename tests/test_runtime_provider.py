"""Tests for runtime provider injection."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memgate"))

import pytest

from zoomac.brain.deps import ZoomacDeps
from zoomac.brain.memory_extract import AgentResponse, EvolutionSignal, MemoryExtract
from zoomac.brain.provider import DefaultRuntimeProvider
from zoomac.brain.runtime import RuntimeBundle
from zoomac.core.config import ZoomacSettings
from zoomac.core.events import EventSource, MessageEvent
from zoomac.core.loop import CoreLoop
from zoomac.planner.models import PlanDecomposition


class _FakeConversationRuntime:
    async def run(self, prompt, *, deps: ZoomacDeps):
        return AgentResponse(
            message=f"echo:{prompt}",
            memory=MemoryExtract(content="", worth_remembering=False),
            evolution=EvolutionSignal(),
        )


class _FakePlannerRuntime:
    async def decompose(self, prompt: str) -> PlanDecomposition:
        return PlanDecomposition(reasoning=prompt, tasks=[])

    async def reason(self, prompt: str) -> str:
        return prompt

    async def write_artifact(self, prompt: str) -> str:
        return prompt

    async def replan(self, prompt: str):
        return SimpleNamespace(action="retry", reasoning=prompt)


class _StubProvider:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def build(self, model, *, skill_registry=None):
        self.calls.append(model)
        return RuntimeBundle(
            conversation_runtime=_FakeConversationRuntime(),
            planner_runtime=_FakePlannerRuntime(),
        )


def test_default_runtime_provider_builds_bundle():
    provider = DefaultRuntimeProvider()
    bundle = provider.build("test")
    assert isinstance(bundle, RuntimeBundle)
    assert bundle.planner_runtime is not None


@pytest.mark.asyncio
async def test_core_loop_uses_injected_runtime_provider(tmp_path):
    provider = _StubProvider()
    settings = ZoomacSettings(project_dir=tmp_path)
    core = CoreLoop(settings, model_override="fake-model", runtime_provider=provider)
    try:
        assert provider.calls
        assert core.planner_runtime is not None
        response = await core.handle_message(
            MessageEvent(
                source=EventSource.CLI,
                channel="cli",
                author="user",
                content="hello",
            )
        )
        assert response == "echo:hello"
    finally:
        core.shutdown()

"""Tests for the conversation runner seam."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from zoomac.brain.deps import ZoomacDeps
from zoomac.brain.memory_extract import AgentResponse, EvolutionSignal, MemoryExtract
from zoomac.core.runner import ConversationRunner


class _FakeMemory:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def ingest_structured(self, payload: dict) -> None:
        self.payloads.append(payload)


class _FakeRuntime:
    async def run(self, content: str, *, deps: ZoomacDeps) -> AgentResponse:
        return AgentResponse(
            message=f"echo:{content}",
            memory=MemoryExtract(
                worth_remembering=True,
                content="remember this",
            ),
            evolution_signal=EvolutionSignal(),
        )


async def test_runner_returns_agent_response_and_ingests_memory():
    memory = _FakeMemory()
    runner = ConversationRunner(runtime=_FakeRuntime(), memory=memory)
    deps = ZoomacDeps(memgate=memory)  # type: ignore[arg-type]

    response = await runner.run_message("Hello Zoomac!", deps=deps)

    assert response.message == "echo:Hello Zoomac!"
    assert memory.payloads == [{"content": "remember this"}]


async def test_runner_tracks_conversation_state():
    memory = _FakeMemory()
    runner = ConversationRunner(runtime=_FakeRuntime(), memory=memory)
    deps = ZoomacDeps(memgate=memory)  # type: ignore[arg-type]

    await runner.run_message("first", deps=deps, conversation_id="cli:main")
    await runner.run_message("second", deps=deps, conversation_id="cli:main")

    state = runner.get_state("cli:main")
    assert state is not None
    assert len(state.turns) == 2
    assert state.turns[0].user_message == "first"
    assert state.turns[1].response_message == "echo:second"


async def test_runner_emits_progress_events():
    memory = _FakeMemory()
    runner = ConversationRunner(runtime=_FakeRuntime(), memory=memory)
    deps = ZoomacDeps(memgate=memory)  # type: ignore[arg-type]
    events: list[str] = []

    await runner.run_message(
        "progress",
        deps=deps,
        progress_callback=lambda event: events.append(event.stage),
    )

    assert events[0] == "started"
    assert "agent_call" in events
    assert "completed" in events


class _FlakyRuntime:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, content: str, *, deps: ZoomacDeps) -> AgentResponse:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary failure")
        return await _FakeRuntime().run(content, deps=deps)


async def test_runner_retries_once_and_recovers():
    memory = _FakeMemory()
    runner = ConversationRunner(runtime=_FlakyRuntime(), memory=memory, max_retries=1)
    deps = ZoomacDeps(memgate=memory)  # type: ignore[arg-type]

    response = await runner.run_message("retry me", deps=deps, conversation_id="cli:retry")

    state = runner.get_state("cli:retry")
    assert response.message == "echo:retry me"
    assert state is not None
    assert state.retry_count == 1
    assert state.last_error is None


class _AlwaysFailRuntime:
    async def run(self, content: str, *, deps: ZoomacDeps) -> AgentResponse:
        raise RuntimeError("boom")


async def test_runner_records_failure_after_retry_budget():
    memory = _FakeMemory()
    runner = ConversationRunner(runtime=_AlwaysFailRuntime(), memory=memory, max_retries=1)
    deps = ZoomacDeps(memgate=memory)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="boom"):
        await runner.run_message("fail", deps=deps, conversation_id="cli:fail")

    state = runner.get_state("cli:fail")
    assert state is not None
    assert state.last_error == "boom"
    assert state.turns[-1].error == "boom"

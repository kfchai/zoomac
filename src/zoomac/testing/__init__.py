"""Test doubles for Zoomac runtimes — replaces pydantic-ai TestModel."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from zoomac.brain.deps import ZoomacDeps
from zoomac.brain.memory_extract import (
    AgentResponse,
    EvolutionSignal,
    MemoryExtract,
)
from zoomac.brain.runtime import ConversationRuntime, RuntimeBundle
from zoomac.planner.models import (
    PlanDecomposition,
    ReplanDecision,
    TaskSpec,
    TaskType,
)
from zoomac.planner.runtime import PlannerRuntime


@dataclass
class FakeConversationRuntime:
    """Conversation runtime that returns a canned AgentResponse."""

    response: AgentResponse | None = None

    async def run(self, prompt: str, *, deps: ZoomacDeps) -> AgentResponse:
        if self.response is not None:
            return self.response
        return AgentResponse(
            message=f"Fake response to: {prompt[:80]}",
            memory=MemoryExtract(worth_remembering=False),
            evolution=EvolutionSignal(),
        )


@dataclass
class FakePlannerRuntime:
    """Planner runtime that returns configurable canned outputs."""

    decompose_result: PlanDecomposition | None = None
    reason_result: str = "Fake reasoning output."
    write_result: str = "FILE: fake.py\n```\nprint('hello')\n```"
    replan_result: ReplanDecision | None = None

    async def decompose(self, prompt: str) -> PlanDecomposition:
        if self.decompose_result is not None:
            return self.decompose_result
        return PlanDecomposition(
            reasoning="Fake plan.",
            tasks=[
                TaskSpec(
                    task_type=TaskType.LLM_REASON,
                    description="Fake task",
                    instruction="Do the fake thing",
                )
            ],
        )

    async def reason(self, prompt: str) -> str:
        return self.reason_result

    async def write_artifact(self, prompt: str) -> str:
        return self.write_result

    async def replan(self, prompt: str) -> ReplanDecision:
        if self.replan_result is not None:
            return self.replan_result
        return ReplanDecision(action="skip", reasoning="Fake skip.")


@dataclass
class FakeRuntimeProvider:
    """Runtime provider that returns fake runtimes — no LLM calls."""

    conversation: FakeConversationRuntime = field(default_factory=FakeConversationRuntime)
    planner: FakePlannerRuntime = field(default_factory=FakePlannerRuntime)

    def build(
        self,
        model: str | Any,
        *,
        skill_registry: Any = None,
    ) -> RuntimeBundle:
        return RuntimeBundle(
            conversation_runtime=self.conversation,
            planner_runtime=self.planner,
        )


__all__ = [
    "FakeConversationRuntime",
    "FakePlannerRuntime",
    "FakeRuntimeProvider",
]

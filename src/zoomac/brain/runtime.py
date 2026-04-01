"""Model runtime composition for Zoomac."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from zoomac.brain.deps import ZoomacDeps
from zoomac.brain.memory_extract import AgentResponse
from zoomac.brain.runtime_agent import build_conversation_runtime
from zoomac.planner.runtime import ModelPlannerRuntime, PlannerRuntime
from zoomac.skills.registry import SkillRegistry


class ConversationRuntime(Protocol):
    """Interface for conversation-oriented model calls."""

    async def run(self, prompt: str, *, deps: ZoomacDeps) -> AgentResponse:
        """Execute one conversation turn and return a parsed response."""


@dataclass(slots=True)
class RuntimeBundle:
    """Concrete model runtimes used by the Zoomac core loop."""

    conversation_runtime: ConversationRuntime
    planner_runtime: PlannerRuntime


def build_runtime_bundle(
    model: str | Any,
    *,
    skill_registry: SkillRegistry | None = None,
) -> RuntimeBundle:
    """Build the default runtime bundle for the current model setting."""
    return RuntimeBundle(
        conversation_runtime=build_conversation_runtime(
            model,
            skill_registry=skill_registry,
        ),
        planner_runtime=ModelPlannerRuntime(model),
    )

"""Tests for skill loading, registry behavior, and integration hooks."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memgate"))

from zoomac.brain.prompts import build_system_prompt
from zoomac.planner.engine import GoalEngine
from zoomac.planner.models import PlanDecomposition
from zoomac.planner.store import GoalStore
from zoomac.skills.loader import load_builtin_skill_registry, load_skill_registry
from zoomac.tools.builtin import build_builtin_tool_registry


def test_load_builtin_skill_registry():
    registry = load_builtin_skill_registry()
    assert "memory-first" in registry.names()
    assert "goal-execution" in registry.names()


def test_skill_registry_relevance_and_prompt_section(tmp_path):
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    (skill_dir / "facts.skill.yaml").write_text(
        "\n".join(
            [
                "name: facts",
                "description: Fact retrieval",
                "instructions: Use fact tools.",
                "triggers:",
                "  - fact",
                "tool_names:",
                "  - check_facts",
            ]
        ),
        encoding="utf-8",
    )
    registry = load_skill_registry(skill_dir)

    relevant = registry.relevant_for("check this fact")
    assert len(relevant) == 1
    assert relevant[0].name == "facts"
    assert "Available Skills" in registry.prompt_section("check this fact")


def test_build_system_prompt_includes_relevant_skills():
    registry = load_builtin_skill_registry()
    prompt = build_system_prompt(skill_registry=registry, user_text="/goal implement feature")
    assert "Available Skills" in prompt
    assert "goal-execution" in prompt


def test_skill_registry_filters_tools_for_query():
    registry = load_builtin_skill_registry()
    filtered = build_builtin_tool_registry(
        skill_registry=registry,
        user_text="remember this fact",
    )
    assert set(filtered.names()) == {"search_memory", "check_facts", "memory_status"}


@pytest.mark.asyncio
async def test_goal_engine_decompose_includes_skill_hints(tmp_path):
    class _CapturingPlanner:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def decompose(self, prompt: str):
            self.prompts.append(prompt)
            return PlanDecomposition(reasoning="ok", tasks=[])

        async def reason(self, prompt: str) -> str:
            return prompt

        async def write_artifact(self, prompt: str) -> str:
            return prompt

        async def replan(self, prompt: str):
            return None

    planner = _CapturingPlanner()
    store = GoalStore(tmp_path / "goals.db")
    registry = load_builtin_skill_registry()
    engine = GoalEngine(
        store=store,
        planner_runtime=planner,
        skill_registry=registry,
    )

    await engine.create_plan("/goal implement feature", source="cli", channel="cli")

    assert planner.prompts
    assert "Available Skills" in planner.prompts[0]
    assert "goal-execution" in planner.prompts[0]
    store.close()

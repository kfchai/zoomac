"""Tests for planner runtime implementations."""

from __future__ import annotations

import pytest

from zoomac.planner.models import PlanDecomposition, ReplanDecision, TaskSpec, TaskType
from zoomac.testing import FakePlannerRuntime


@pytest.mark.asyncio
async def test_fake_planner_runtime_decompose_default():
    runtime = FakePlannerRuntime()
    plan = await runtime.decompose("plan this")
    assert isinstance(plan, PlanDecomposition)
    assert len(plan.tasks) >= 1


@pytest.mark.asyncio
async def test_fake_planner_runtime_decompose_custom():
    custom = PlanDecomposition(
        reasoning="test",
        tasks=[
            TaskSpec(
                task_type=TaskType.LLM_REASON,
                description="reason",
                instruction="do it",
            )
        ],
    )
    runtime = FakePlannerRuntime(decompose_result=custom)

    plan = await runtime.decompose("plan this")
    assert plan.reasoning == "test"
    assert len(plan.tasks) == 1


@pytest.mark.asyncio
async def test_fake_planner_runtime_reason_and_replan():
    runtime = FakePlannerRuntime()
    reasoning = await runtime.reason("explain")
    assert isinstance(reasoning, str)

    decision = await runtime.replan("replan")
    assert isinstance(decision, ReplanDecision)
    assert decision.action == "skip"

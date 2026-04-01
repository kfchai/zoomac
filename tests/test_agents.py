"""Tests for sub-agent lifecycle and manager behavior."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memgate"))

from zoomac.agents import SubAgentManager, SubAgentStatus


class _FakePlannerRuntime:
    async def reason(self, prompt: str) -> str:
        return f"summary:{prompt.splitlines()[1]}"


class _SlowPlannerRuntime:
    async def reason(self, prompt: str) -> str:
        await __import__("asyncio").sleep(0.05)
        return prompt


@pytest.mark.asyncio
async def test_subagent_manager_runs_to_completion():
    manager = SubAgentManager(planner_runtime=_FakePlannerRuntime(), max_agents=2)
    result = await manager.run_subagent(
        objective="Inspect parser behavior",
        parent_goal_id="goal1",
        parent_task_id="task1",
        tool_scope=["memory_query"],
        memory_context="Parser bug report",
    )

    record = manager.get(result.agent_id)
    assert record is not None
    assert record.status == SubAgentStatus.DONE
    assert "Inspect parser behavior" in result.summary
    events = manager.bus.events_for(result.agent_id)
    assert [event.event_type for event in events] == ["spawned", "running", "done"]


@pytest.mark.asyncio
async def test_subagent_manager_enforces_timeout():
    manager = SubAgentManager(planner_runtime=_SlowPlannerRuntime(), max_agents=1)

    with pytest.raises(TimeoutError):
        await manager.run_subagent(
            objective="Take too long",
            timeout_seconds=0,
        )

    records = manager.list_agents()
    assert len(records) == 1
    assert records[0].status == SubAgentStatus.FAILED

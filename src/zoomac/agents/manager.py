"""Sub-agent manager and execution helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from zoomac.agents.bus import SubAgentBus
from zoomac.agents.lifecycle import SubAgentRecord, SubAgentStatus


@dataclass(slots=True)
class SubAgentResult:
    """Merged result returned from a completed sub-agent."""

    agent_id: str
    summary: str
    artifacts: list[str]
    memory_candidates: list[str]
    approval_outcomes: list[str]


class SubAgentManager:
    """Spawns and tracks bounded delegated sub-agents."""

    def __init__(
        self,
        *,
        planner_runtime: object,
        max_agents: int = 5,
        bus: SubAgentBus | None = None,
    ) -> None:
        self._planner = planner_runtime
        self._max_agents = max_agents
        self._bus = bus or SubAgentBus()
        self._agents: dict[str, SubAgentRecord] = {}
        self._tasks: dict[str, asyncio.Task[SubAgentResult]] = {}

    @property
    def bus(self) -> SubAgentBus:
        return self._bus

    def get(self, agent_id: str) -> SubAgentRecord | None:
        return self._agents.get(agent_id)

    def list_agents(self) -> list[SubAgentRecord]:
        return list(self._agents.values())

    async def run_subagent(
        self,
        *,
        objective: str,
        parent_goal_id: str | None = None,
        parent_task_id: str | None = None,
        tool_scope: list[str] | None = None,
        memory_context: str | None = None,
        timeout_seconds: int = 60,
    ) -> SubAgentResult:
        """Spawn a bounded sub-agent and wait for its merged result."""
        running = sum(1 for record in self._agents.values() if record.status == SubAgentStatus.RUNNING)
        if running >= self._max_agents:
            raise RuntimeError("Maximum concurrent sub-agents reached")

        record = SubAgentRecord(
            objective=objective,
            tool_scope=tool_scope or [],
            parent_goal_id=parent_goal_id,
            parent_task_id=parent_task_id,
            memory_context=memory_context,
            timeout_seconds=timeout_seconds,
        )
        self._agents[record.id] = record
        self._transition(record, SubAgentStatus.SPAWNED, "Sub-agent spawned")

        task = asyncio.create_task(self._run_record(record), name=f"subagent-{record.id}")
        self._tasks[record.id] = task
        try:
            return await task
        finally:
            self._tasks.pop(record.id, None)

    async def _run_record(self, record: SubAgentRecord) -> SubAgentResult:
        self._transition(record, SubAgentStatus.RUNNING, "Sub-agent running")
        prompt = self._build_prompt(record)
        try:
            result = await asyncio.wait_for(
                self._planner.reason(prompt),
                timeout=record.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            record.error = f"Timed out after {record.timeout_seconds}s"
            self._transition(record, SubAgentStatus.FAILED, record.error)
            raise TimeoutError(record.error) from exc
        except Exception as exc:
            record.error = str(exc)
            self._transition(record, SubAgentStatus.FAILED, f"Sub-agent failed: {exc}")
            raise

        summary = str(result).strip() or "Sub-agent completed without summary."
        record.summary = summary
        self._transition(record, SubAgentStatus.DONE, "Sub-agent completed")
        return SubAgentResult(
            agent_id=record.id,
            summary=summary,
            artifacts=[],
            memory_candidates=[],
            approval_outcomes=[],
        )

    def _transition(self, record: SubAgentRecord, status: SubAgentStatus, message: str) -> None:
        record.status = status
        record.touch()
        self._bus.publish(record.id, status.value, message)

    @staticmethod
    def _build_prompt(record: SubAgentRecord) -> str:
        lines = [
            "You are a bounded sub-agent working on one delegated objective.",
            f"Objective: {record.objective}",
            "Return a concise final summary focused on what you found or completed.",
        ]
        if record.tool_scope:
            lines.append(f"Allowed tool scope: {', '.join(record.tool_scope)}")
        if record.memory_context:
            lines.append(f"Context: {record.memory_context}")
        return "\n".join(lines)

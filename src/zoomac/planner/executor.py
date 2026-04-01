"""Task execution helpers for goal execution."""

from __future__ import annotations

import logging
from pathlib import Path

from zoomac.planner.approval import parse_write_code_output
from zoomac.planner.models import Goal, GoalStatus, Task, TaskStatus, TaskType

logger = logging.getLogger(__name__)


class TaskBlocked(Exception):
    """Raised when a task needs to pause for user input or credentials."""


class GoalTaskExecutor:
    """Runs goal tasks using the planner runtime and configured integrations."""

    def __init__(
        self,
        *,
        planner: object,
        store: object,
        agent_manager: object | None = None,
        sandbox: object | None = None,
        gateway: object | None = None,
        memory: object | None = None,
        credential_vault: object | None = None,
        project_dir: str | None = None,
    ) -> None:
        self._planner = planner
        self._store = store
        self._agent_manager = agent_manager
        self._sandbox = sandbox
        self._gateway = gateway
        self._memory = memory
        self._vault = credential_vault
        self._project_dir = project_dir

    async def execute_task(self, task: Task, goal: Goal, context_summary: str) -> str:
        """Execute a single task by type."""
        if task.task_type == TaskType.LLM_REASON:
            return await self.exec_llm_reason(task, context_summary)
        if task.task_type == TaskType.READ_FILE:
            return await self.exec_read_file(task)
        if task.task_type == TaskType.WRITE_CODE:
            return await self.exec_write_code(task, context_summary)
        if task.task_type == TaskType.SANDBOX_EXEC:
            return await self.exec_sandbox(task)
        if task.task_type == TaskType.SEND_MESSAGE:
            return await self.exec_send_message(task, goal)
        if task.task_type == TaskType.WEB_LOOKUP:
            return await self.exec_web_lookup(task)
        if task.task_type == TaskType.MEMORY_QUERY:
            return await self.exec_memory_query(task)
        if task.task_type == TaskType.WAIT_INPUT:
            self._store.update_task(
                task.id,
                status=TaskStatus.BLOCKED,
                blocker_reason=f"Waiting for user input: {task.description}",
            )
            raise TaskBlocked(f"Waiting for input: {task.description}")
        if task.task_type == TaskType.CREDENTIAL_REQUEST:
            return await self.exec_credential_request(task)
        if task.task_type == TaskType.SUBAGENT_SPAWN:
            return await self.exec_subagent_spawn(task)
        return f"Unknown task type: {task.task_type}"

    async def exec_llm_reason(self, task: Task, context: str) -> str:
        prompt = f"Context from previous tasks:\n{context}\n\nTask: {task.instruction}"
        return await self._planner.reason(prompt)

    async def exec_read_file(self, task: Task) -> str:
        base = Path(self._project_dir) if self._project_dir else Path.cwd()
        target = (base / task.instruction.strip()).resolve()
        if not str(target).startswith(str(base.resolve())):
            raise PermissionError(
                f"Read target '{target}' is outside project directory '{base}'"
            )
        return target.read_text(encoding="utf-8")

    async def exec_write_code(self, task: Task, context: str) -> str:
        prompt = (
            f"Context from previous tasks:\n{context}\n\n"
            f"Task: {task.instruction}\n\n"
            "Respond with the file path (relative to project root) on the first line, "
            "then the full file content. Format:\n"
            "FILE: path/to/file.py\n"
            "```\n<content>\n```"
        )
        output = await self._planner.write_artifact(prompt)
        file_path, content = parse_write_code_output(output)
        if not file_path:
            file_path, content = parse_write_code_output(task.instruction)
        if not file_path:
            return f"LLM output (no file parsed): {output[:500]}"

        base = Path(self._project_dir) if self._project_dir else Path.cwd()
        target = (base / file_path).resolve()
        if not str(target).startswith(str(base.resolve())):
            raise PermissionError(
                f"Write target '{target}' is outside project directory '{base}'"
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {file_path}"

    async def exec_sandbox(self, task: Task) -> str:
        if self._sandbox is None:
            return f"[sandbox unavailable] Would execute: {task.instruction}"
        policy = self._sandbox.resolve_execution_policy(
            task.instruction,
            requires_network=True,
            reads_project=True,
        )
        result = await self._sandbox.execute(
            task.instruction,
            profile_name=policy.profile_name,
        )
        if result.timed_out:
            raise TimeoutError(f"Sandbox command timed out after {result.duration_ms}ms")
        if result.exit_code != 0:
            raise RuntimeError(
                f"Sandbox command failed (exit {result.exit_code}): {result.stderr}"
            )
        return result.stdout

    async def exec_send_message(self, task: Task, goal: Goal) -> str:
        if self._gateway is None:
            return f"[gateway unavailable] Would send: {task.instruction}"
        from zoomac.core.events import EventSource
        from zoomac.gateway.adapter import OutboundMessage

        platform = EventSource(goal.source) if goal.source != "cli" else EventSource.CLI
        msg = OutboundMessage(
            platform=platform,
            channel=goal.channel,
            content=task.instruction,
        )
        success = await self._gateway.send(msg)
        return "Message sent" if success else "Failed to send message"

    async def exec_web_lookup(self, task: Task) -> str:
        return f"[web lookup placeholder] {task.instruction}"

    async def exec_memory_query(self, task: Task) -> str:
        if self._memory is None:
            return f"[memory unavailable] Would query: {task.instruction}"
        context = self._memory.retrieve_context(task.instruction)
        return context or "No relevant memories found."

    async def exec_credential_request(self, task: Task) -> str:
        if self._vault:
            existing = self._vault.retrieve(task.instruction)
            if existing:
                return f"Credential '{task.instruction}' available"
            self._vault.request(task.instruction, task.description)

        self._store.update_task(
            task.id,
            status=TaskStatus.BLOCKED,
            blocker_reason=f"Need credential: {task.description}",
        )
        raise TaskBlocked(f"Need credential: {task.description}")

    async def exec_subagent_spawn(self, task: Task) -> str:
        if self._agent_manager is None:
            return f"[subagent unavailable] Would delegate: {task.instruction}"
        result = await self._agent_manager.run_subagent(
            objective=task.instruction,
            parent_goal_id=task.goal_id,
            parent_task_id=task.id,
            tool_scope=["memory_query", "llm_reason"],
            memory_context=task.description,
        )
        return f"Sub-agent {result.agent_id} completed: {result.summary}"

    async def replan(self, goal: Goal, failed_task: Task, error: str) -> None:
        try:
            prompt = (
                f"A task in your plan failed.\n\n"
                f"Goal: {goal.raw_instruction}\n"
                f"Failed task: {failed_task.description}\n"
                f"Error: {error}\n\n"
                f"Decide: retry (same task), skip (mark skipped), replace (new tasks), or abort (give up)."
            )
            decision = await self._planner.replan(prompt)

            if hasattr(decision, "action"):
                if decision.action == "abort":
                    self._store.update_goal_status(goal.id, GoalStatus.FAILED)
                elif decision.action == "skip":
                    self._store.update_task(failed_task.id, status=TaskStatus.SKIPPED)
                elif (
                    decision.action == "replace"
                    and hasattr(decision, "replacement_tasks")
                    and decision.replacement_tasks
                ):
                    for spec in decision.replacement_tasks:
                        new_task = Task(
                            goal_id=goal.id,
                            task_type=spec.task_type,
                            description=spec.description,
                            instruction=spec.instruction,
                            depends_on=failed_task.depends_on,
                        )
                        self._store.save_task(new_task)
                    self._store.update_task(failed_task.id, status=TaskStatus.SKIPPED)
        except Exception:
            logger.exception("Replan failed for task %s", failed_task.id)

"""Goal engine — decomposes goals into task DAGs and orchestrates execution."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from zoomac.planner.approval import GoalApprovalHandler
from zoomac.planner.credentials import CredentialVault
from zoomac.planner.executor import GoalTaskExecutor
from zoomac.planner.models import (
    Goal,
    GoalStatus,
    PlanDecomposition,
    Task,
    TaskStatus,
    TaskType,
)
from zoomac.planner.runtime import PlannerRuntime
from zoomac.planner.store import GoalStore
from zoomac.skills.registry import SkillRegistry

if TYPE_CHECKING:
    from zoomac.autonomy.policy import AutonomyManager
    from zoomac.agents.manager import SubAgentManager
    from zoomac.gateway.gateway import Gateway
    from zoomac.memory.integration import MemoryManager
    from zoomac.sandbox.docker import SandboxManager

logger = logging.getLogger(__name__)


class GoalStepResult:
    """Result of a single engine step."""

    def __init__(
        self,
        executed: list[str] | None = None,
        blocked: list[str] | None = None,
        failed: list[str] | None = None,
        goal_done: bool = False,
        goal_failed: bool = False,
        goal_blocked: bool = False,
        messages: list[str] | None = None,
    ) -> None:
        self.executed = executed or []
        self.blocked = blocked or []
        self.failed = failed or []
        self.goal_done = goal_done
        self.goal_failed = goal_failed
        self.goal_blocked = goal_blocked
        self.messages = messages or []


class GoalEngine:
    """Decomposes high-level goals into task DAGs and orchestrates execution."""

    def __init__(
        self,
        store: GoalStore,
        planner_runtime: PlannerRuntime,
        autonomy: AutonomyManager | None = None,
        agent_manager: SubAgentManager | None = None,
        skill_registry: SkillRegistry | None = None,
        sandbox: SandboxManager | None = None,
        gateway: Gateway | None = None,
        memory: MemoryManager | None = None,
        credential_vault: CredentialVault | None = None,
        project_dir: str | None = None,
    ) -> None:
        self._store = store
        self._planner = planner_runtime
        self._autonomy = autonomy
        self._agent_manager = agent_manager
        self._skills = skill_registry
        self._sandbox = sandbox
        self._gateway = gateway
        self._memory = memory
        self._vault = credential_vault
        self._project_dir = project_dir
        self._approval_handler = GoalApprovalHandler(
            store=self._store,
            autonomy=self._autonomy,
            credential_vault=self._vault,
            project_dir=self._project_dir,
        )
        self._executor = GoalTaskExecutor(
            planner=self._planner,
            store=self._store,
            agent_manager=self._agent_manager,
            sandbox=self._sandbox,
            gateway=self._gateway,
            memory=self._memory,
            credential_vault=self._vault,
            project_dir=self._project_dir,
        )

    @property
    def store(self) -> GoalStore:
        return self._store

    async def create_plan(
        self,
        instruction: str,
        source: str = "cli",
        channel: str = "",
    ) -> Goal:
        """Decompose a natural language instruction into a draft goal plan."""
        goal = Goal(
            raw_instruction=instruction,
            source=source,
            channel=channel,
            status=GoalStatus.PLANNING,
        )
        self._store.save_goal(goal)
        self._store.add_task_event(goal.id, "goal_planning", f"Planning goal: {instruction}")

        # Ask the LLM to decompose the goal
        plan = await self._decompose(instruction)

        # Convert TaskSpecs into Tasks with resolved dependencies
        task_ids: list[str] = []
        for i, spec in enumerate(plan.tasks):
            task = Task(
                goal_id=goal.id,
                task_type=spec.task_type,
                description=spec.description,
                instruction=spec.instruction,
                depends_on=[task_ids[idx] for idx in spec.depends_on_indices if idx < len(task_ids)],
            )
            self._store.save_task(task)
            self._store.add_task_event(
                goal.id,
                "task_planned",
                f"Planned {task.task_type.value}: {task.description}",
                task_id=task.id,
            )
            task_ids.append(task.id)
            goal.tasks.append(task)

        goal.status = GoalStatus.DRAFT
        self._store.update_goal_status(goal.id, GoalStatus.DRAFT)
        self._store.add_task_event(
            goal.id,
            "plan_ready",
            f"Plan ready with {len(goal.tasks)} task(s) for review.",
        )
        return goal

    async def create_goal(self, instruction: str, source: str = "cli", channel: str = "") -> Goal:
        """Create and auto-approve a goal plan for immediate execution."""
        goal = await self.create_plan(instruction, source=source, channel=channel)
        return self.approve_goal(goal.id)

    def approve_goal(self, goal_id: str) -> Goal:
        """Approve a draft goal so it can execute."""
        goal = self._store.load_goal(goal_id)
        if goal is None:
            raise ValueError(f"Goal {goal_id} not found")
        if goal.status not in (GoalStatus.DRAFT, GoalStatus.APPROVED, GoalStatus.PLANNING):
            return goal
        self._store.update_goal_status(goal.id, GoalStatus.APPROVED)
        self._store.add_task_event(goal.id, "plan_approved", "Plan approved for execution.")
        self._store.update_goal_status(goal.id, GoalStatus.EXECUTING)
        self._store.add_task_event(goal.id, "goal_executing", "Goal execution started.")
        refreshed = self._store.load_goal(goal.id)
        if refreshed is None:
            raise ValueError(f"Goal {goal_id} disappeared after approval")
        return refreshed

    def goal_status_report(self, goal_id: str) -> str:
        """Return a compact user-visible plan and task status summary."""
        goal = self._store.load_goal(goal_id)
        if goal is None:
            return "Goal not found."
        counts: dict[str, int] = {}
        for task in goal.tasks:
            counts[task.status.value] = counts.get(task.status.value, 0) + 1
        parts = [f"Goal {goal.id}: {goal.status.value}"]
        if counts:
            parts.append(
                ", ".join(f"{status}={counts[status]}" for status in sorted(counts))
            )
        latest_events = self._store.list_task_events(goal.id, limit=3)
        if latest_events:
            parts.append(
                "recent: " + " | ".join(event.message for event in latest_events[-3:])
            )
        return " | ".join(parts)

    async def step(self, goal_id: str) -> GoalStepResult:
        """Execute the next batch of ready tasks for a goal."""
        goal = self._store.load_goal(goal_id)
        if goal is None:
            return GoalStepResult(goal_failed=True, messages=["Goal not found"])

        if goal.status == GoalStatus.DRAFT:
            return GoalStepResult(goal_blocked=True, messages=["Goal is waiting for plan approval"])

        if goal.status in (GoalStatus.DONE, GoalStatus.FAILED):
            return GoalStepResult(
                goal_done=goal.status == GoalStatus.DONE,
                goal_failed=goal.status == GoalStatus.FAILED,
            )

        ready_tasks = self._store.get_ready_tasks(goal_id)
        if not ready_tasks:
            return self._check_goal_completion(goal)

        result = GoalStepResult()

        for task in ready_tasks:
            approval = self._approval_handler.evaluate_task(goal, task)
            task.risk_level = approval.risk_level
            task.approval_mode = (
                approval.decision.mode.value if approval.decision is not None else None
            )
            task.approval_provenance = (
                approval.decision.provenance if approval.decision is not None else None
            )
            if approval.risk_level is not None:
                if approval.state == "deny":
                    self._store.update_task(
                        task.id,
                        status=TaskStatus.FAILED,
                        error=approval.blocker_reason
                        or f"Denied by approval policy: {task.description}",
                        blocker_reason=None,
                        risk_level=task.risk_level,
                        approval_mode=task.approval_mode,
                        approval_provenance=task.approval_provenance,
                    )
                    self._store.add_task_event(
                        goal.id,
                        "task_denied",
                        approval.blocker_reason
                        or f"Task denied by approval policy: {task.description}",
                        task_id=task.id,
                    )
                    result.failed.append(task.id)
                    result.messages.append(
                        approval.blocker_reason
                        or f"Task denied by approval policy: {task.description}"
                    )
                    continue

                if approval.state == "block":
                    self._store.update_task(
                        task.id,
                        status=TaskStatus.BLOCKED,
                        blocker_reason=approval.blocker_reason,
                        risk_level=task.risk_level,
                        approval_mode=task.approval_mode,
                        approval_provenance=task.approval_provenance,
                    )
                    self._store.add_task_event(
                        goal.id,
                        "task_blocked",
                        approval.blocker_reason
                        or f"Task blocked pending approval: {task.description}",
                        task_id=task.id,
                    )
                    result.blocked.append(task.id)
                    result.messages.append(
                        approval.blocker_reason
                        or f"Task blocked pending approval: {task.description}"
                    )
                    continue

            # Mark running
            self._store.update_task(
                task.id,
                status=TaskStatus.RUNNING,
                attempts=task.attempts + 1,
                approval_mode=task.approval_mode,
                approval_provenance=task.approval_provenance,
            )
            self._store.add_task_event(
                goal.id,
                "task_started",
                f"Started {task.task_type.value}: {task.description}",
                task_id=task.id,
            )

            try:
                task_result = await self._executor.execute_task(
                    task,
                    goal,
                    self._build_context_summary(goal),
                )
                self._store.update_task(
                    task.id,
                    status=TaskStatus.DONE,
                    result=task_result,
                    output_summary=self._summarize_task_output(task_result),
                )
                self._store.add_task_event(
                    goal.id,
                    "task_completed",
                    f"Completed {task.description}: {self._summarize_task_output(task_result)}",
                    task_id=task.id,
                )
                # Accumulate context
                goal.context[task.id] = task_result
                self._store.update_goal_context(goal.id, goal.context)
                result.executed.append(task.id)
            except Exception as e:
                logger.exception("Task %s failed: %s", task.id, e)
                if task.attempts + 1 >= task.max_attempts:
                    self._store.update_task(
                        task.id, status=TaskStatus.FAILED, error=str(e)
                    )
                    self._store.add_task_event(
                        goal.id,
                        "task_failed",
                        f"Task failed: {task.description} ({e})",
                        task_id=task.id,
                    )
                    result.failed.append(task.id)
                    await self._executor.replan(goal, task, str(e))
                else:
                    self._store.update_task(
                        task.id, status=TaskStatus.PENDING, error=str(e)
                    )
                    self._store.add_task_event(
                        goal.id,
                        "task_retrying",
                        f"Task will retry: {task.description} ({e})",
                        task_id=task.id,
                    )

        # Check overall goal status after this step
        completion = self._check_goal_completion(
            self._store.load_goal(goal_id)
        )
        result.goal_done = completion.goal_done
        result.goal_failed = completion.goal_failed
        result.goal_blocked = completion.goal_blocked
        return result

    async def run_goal(self, goal_id: str) -> Goal:
        """Run step() in a loop until done, blocked, or failed."""
        while True:
            result = await self.step(goal_id)
            if result.goal_done or result.goal_failed or result.goal_blocked:
                break
            if not result.executed:
                # Nothing was executed and goal isn't terminal — avoid busy loop
                break
        return self._store.load_goal(goal_id)

    async def handle_user_input(self, goal_id: str, input_text: str) -> str:
        """Route user-provided input to a blocked task."""
        goal = self._store.load_goal(goal_id)
        if goal is None:
            return "Goal not found."
        reply = await self._approval_handler.handle_user_input(goal, input_text)
        self._store.add_task_event(goal.id, "user_input", reply)
        return reply

    # --- Internal methods ---

    async def _decompose(self, instruction: str) -> PlanDecomposition:
        """Use the LLM to decompose a goal instruction into tasks."""
        prompt = (
            "You are a goal planner. Decompose this goal into concrete, executable tasks.\n\n"
            f"Goal: {instruction}\n\n"
            "For each task, specify:\n"
            "- task_type: one of llm_reason, write_code, sandbox_exec, send_message, wait_input, memory_query, credential_request\n"
            "- description: short human-readable description\n"
            "- instruction: detailed instruction for execution\n"
            "- depends_on_indices: list of task indices this depends on (0-based)\n\n"
            "Think about what credentials or resources are needed, what can run in parallel, "
            "and what order makes sense."
        )
        if self._skills is not None:
            skill_section = self._skills.prompt_section(instruction)
            if skill_section:
                prompt = f"{prompt}\n\n{skill_section}"
        return await self._planner.decompose(prompt)

    def _check_goal_completion(self, goal: Goal | None) -> GoalStepResult:
        """Determine if a goal is done, failed, or blocked."""
        if goal is None:
            return GoalStepResult(goal_failed=True)

        all_terminal = True
        all_blocked_or_terminal = True
        any_failed = False

        for task in goal.tasks:
            if task.status in (TaskStatus.DONE, TaskStatus.SKIPPED, TaskStatus.FAILED):
                if task.status == TaskStatus.FAILED:
                    any_failed = True
                continue
            all_terminal = False
            if task.status != TaskStatus.BLOCKED:
                all_blocked_or_terminal = False

        if all_terminal:
            new_status = GoalStatus.FAILED if any_failed else GoalStatus.DONE
            self._store.update_goal_status(goal.id, new_status)
            self._store.add_task_event(
                goal.id,
                "goal_finished",
                f"Goal finished with status: {new_status.value}",
            )
            return GoalStepResult(
                goal_done=new_status == GoalStatus.DONE,
                goal_failed=new_status == GoalStatus.FAILED,
            )

        if all_blocked_or_terminal:
            self._store.update_goal_status(goal.id, GoalStatus.BLOCKED)
            self._store.add_task_event(goal.id, "goal_blocked", "Goal is blocked pending input or approval.")
            return GoalStepResult(goal_blocked=True)

        return GoalStepResult()

    def _build_context_summary(self, goal: Goal) -> str:
        """Build a summary of completed task results for context injection."""
        lines = []
        for task in goal.tasks:
            if task.status == TaskStatus.DONE and task.result:
                lines.append(f"[{task.description}]: {task.result[:500]}")
        return "\n".join(lines) if lines else "No prior results."

    @staticmethod
    def _summarize_task_output(task_result: str | None) -> str:
        if not task_result:
            return "No output."
        compact = " ".join(task_result.strip().split())
        return compact[:160]

    @staticmethod
    def _is_approval_blocker(task: Task) -> bool:
        return GoalApprovalHandler.is_approval_blocker(task)

    @staticmethod
    def _planned_file_path(task: Task) -> str | None:
        return GoalApprovalHandler.planned_file_path(task)

    @staticmethod
    def _approval_session_id(goal: Goal) -> str:
        return GoalApprovalHandler.approval_session_id(goal)


class _TaskBlocked(Exception):
    """Raised when a task needs to block for user input."""

"""Approval and blocked-input helpers for goal execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from zoomac.autonomy.classifier import ActionType
from zoomac.autonomy.pipeline import ApprovalDecision, ApprovalOutcome
from zoomac.planner.models import Goal, GoalStatus, Task, TaskStatus, TaskType
from zoomac.planner.store import GoalStore


_TASK_ACTION_MAP: dict[TaskType, ActionType] = {
    TaskType.LLM_REASON: ActionType.MEMORY_QUERY,
    TaskType.READ_FILE: ActionType.READ_FILE,
    TaskType.WRITE_CODE: ActionType.WRITE_FILE,
    TaskType.SANDBOX_EXEC: ActionType.RUN_COMMAND,
    TaskType.SEND_MESSAGE: ActionType.SEND_MESSAGE,
    TaskType.WAIT_INPUT: ActionType.MEMORY_QUERY,
    TaskType.WEB_LOOKUP: ActionType.WEB_SEARCH,
    TaskType.MEMORY_QUERY: ActionType.MEMORY_QUERY,
    TaskType.CREDENTIAL_REQUEST: ActionType.MEMORY_QUERY,
    TaskType.SUBAGENT_SPAWN: ActionType.SPAWN_AGENT,
}


@dataclass(slots=True)
class GoalApprovalHandler:
    """Applies approval decisions and resolves blocked user input."""

    store: GoalStore
    autonomy: object | None = None
    credential_vault: object | None = None
    project_dir: str | None = None

    def evaluate_task(self, goal: Goal, task: Task) -> "TaskApprovalResult":
        """Evaluate a task through the approval pipeline."""
        if self.autonomy is None:
            return TaskApprovalResult(state="allow")

        action_type = self.action_type_for_task(task)
        decision = self.autonomy.evaluate_action(
            action_type=action_type,
            detail=task.description,
            session_id=self.approval_session_id(goal),
            command_text=task.instruction if task.task_type == TaskType.SANDBOX_EXEC else None,
            file_path=self.planned_file_path(task, self.project_dir),
        )
        result = TaskApprovalResult(
            state="allow",
            risk_level=decision.classification.risk.value,
            decision=decision,
        )

        if decision.outcome == ApprovalOutcome.DENY:
            result.state = "deny"
            result.blocker_reason = self.approval_blocker_reason(action_type, task, decision)
            return result
        if decision.requires_user_input:
            result.state = "block"
            result.blocker_reason = self.approval_blocker_reason(action_type, task, decision)
            return result
        return result

    async def handle_user_input(self, goal: Goal, input_text: str) -> str:
        """Resolve the first blocked task in a goal with user input."""
        for task in goal.tasks:
            if task.status != TaskStatus.BLOCKED:
                continue

            if task.task_type == TaskType.CREDENTIAL_REQUEST:
                if self.credential_vault:
                    self.credential_vault.store(
                        key=task.instruction,
                        value=input_text,
                        description=task.description,
                    )
                self.store.update_task(
                    task.id,
                    status=TaskStatus.DONE,
                    result=f"Credential '{task.instruction}' provided",
                    blocker_reason=None,
                )
                goal.context[task.id] = f"Credential '{task.instruction}' stored"
                self.store.update_goal_context(goal.id, goal.context)
                if goal.status == GoalStatus.BLOCKED:
                    self.store.update_goal_status(goal.id, GoalStatus.EXECUTING)
                return "Credential received. Resuming goal."

            if task.task_type == TaskType.WAIT_INPUT:
                self.store.update_task(
                    task.id,
                    status=TaskStatus.DONE,
                    result=input_text,
                    blocker_reason=None,
                )
                goal.context[task.id] = input_text
                self.store.update_goal_context(goal.id, goal.context)
                if goal.status == GoalStatus.BLOCKED:
                    self.store.update_goal_status(goal.id, GoalStatus.EXECUTING)
                return "Input received. Resuming goal."

            if self.is_approval_blocker(task):
                normalized = input_text.strip().lower()
                action_type = self.action_type_for_task(task)
                if normalized in {"no", "n", "deny", "reject"}:
                    self.store.update_task(
                        task.id,
                        status=TaskStatus.FAILED,
                        error="User denied approval",
                        blocker_reason=None,
                    )
                    return "Denied. Goal task marked as failed."

                if normalized in {
                    "allow command",
                    "allow command prefix",
                    "always for this command",
                }:
                    if self.autonomy is None or task.task_type != TaskType.SANDBOX_EXEC:
                        return self._resume_after_one_off_approval(goal, task, "Confirmed. Resuming goal.")
                    self.autonomy.allow_command_prefix(task.instruction.strip(), action_type)
                    return self._resume_after_one_off_approval(
                        goal,
                        task,
                        "Approved for matching commands. Resuming goal.",
                    )

                if normalized in {
                    "allow path",
                    "allow file",
                    "allow file path",
                    "always for this path",
                }:
                    path = self.planned_file_path(task, self.project_dir)
                    if self.autonomy is None or path is None:
                        return self._resume_after_one_off_approval(goal, task, "Confirmed. Resuming goal.")
                    self.autonomy.allow_path_prefix(path, action_type)
                    return self._resume_after_one_off_approval(
                        goal,
                        task,
                        "Approved for matching file paths. Resuming goal.",
                    )

                if normalized in {"allow session", "session", "always for session"}:
                    if self.autonomy is not None:
                        self.autonomy.allow_for_session(
                            self.approval_session_id(goal),
                            action_type,
                        )
                    return self._resume_after_one_off_approval(
                        goal,
                        task,
                        "Approved for this session. Resuming goal.",
                    )

                return self._resume_after_one_off_approval(goal, task, "Confirmed. Resuming goal.")

        return "No blocked task found matching this input."

    @staticmethod
    def action_type_for_task(task: Task) -> ActionType:
        return _TASK_ACTION_MAP.get(task.task_type, ActionType.RUN_COMMAND)

    @staticmethod
    def approval_blocker_reason(
        action_type: ActionType,
        task: Task,
        decision: ApprovalDecision,
    ) -> str:
        options = ["allow", "deny", "allow session"]
        if task.task_type == TaskType.SANDBOX_EXEC:
            options.append("allow command")
        if task.task_type == TaskType.WRITE_CODE and GoalApprovalHandler.planned_file_path(
            task
        ):
            options.append("allow path")
        options_text = ", ".join(options)
        return (
            f"Approval required [{action_type.value}] via {decision.provenance}: "
            f"{decision.reason}. Options: {options_text}."
        )

    @staticmethod
    def is_approval_blocker(task: Task) -> bool:
        blocker = (task.blocker_reason or "").lower()
        return "approval required" in blocker or "confirmation" in blocker

    @staticmethod
    def approval_session_id(goal: Goal) -> str:
        return f"{goal.source}:{goal.channel}"

    @staticmethod
    def planned_file_path(task: Task, project_dir: str | None = None) -> str | None:
        file_path, _ = parse_write_code_output(task.instruction)
        if file_path is None:
            return None
        if project_dir is None:
            return file_path
        return str((Path(project_dir) / file_path).resolve(strict=False))

    def _resume_after_one_off_approval(self, goal: Goal, task: Task, reply: str) -> str:
        self.store.update_task(
            task.id,
            status=TaskStatus.PENDING,
            blocker_reason=None,
        )
        if goal.status == GoalStatus.BLOCKED:
            self.store.update_goal_status(goal.id, GoalStatus.EXECUTING)
        return reply


@dataclass(slots=True)
class TaskApprovalResult:
    """Pipeline decision translated into planner task handling."""

    state: str
    risk_level: str | None = None
    decision: ApprovalDecision | None = None
    blocker_reason: str | None = None


def parse_write_code_output(text: str) -> tuple[str | None, str]:
    """Parse 'FILE: path' + content block from LLM output."""
    lines = text.strip().split("\n")
    file_path = None
    content_lines: list[str] = []
    in_code_block = False

    for line in lines:
        if file_path is None and line.strip().upper().startswith("FILE:"):
            file_path = line.split(":", 1)[1].strip()
            continue
        if line.strip().startswith("```"):
            if in_code_block:
                break
            in_code_block = True
            continue
        if in_code_block:
            content_lines.append(line)
        elif file_path and not in_code_block:
            content_lines.append(line)

    return file_path, "\n".join(content_lines)

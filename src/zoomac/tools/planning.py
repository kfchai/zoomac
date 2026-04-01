"""Built-in planning and task tools."""

from __future__ import annotations

from pathlib import Path

from zoomac.brain.deps import ZoomacDeps
from zoomac.autonomy.classifier import ActionType
from zoomac.core.events import EventSource
from zoomac.gateway.adapter import OutboundMessage
from zoomac.tools.base import ToolCapabilities, ToolDefinition, ToolSpec
from zoomac.tools.registry import ToolRegistry


async def write_code(deps: ZoomacDeps, file_path: str, content: str) -> str:
    """Write a file to the project directory."""
    goal_engine = getattr(deps, "goal_engine", None)
    project_dir = goal_engine._project_dir if goal_engine else None

    base = Path(project_dir) if project_dir else Path.cwd()
    target = (base / file_path).resolve()

    if not str(target).startswith(str(base.resolve())):
        return f"Error: path '{file_path}' resolves outside project directory"

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} bytes to {file_path}"


async def execute_in_sandbox(
    deps: ZoomacDeps, command: str, profile: str = "standard"
) -> str:
    """Execute a shell command in the sandbox."""
    sandbox = getattr(deps, "sandbox", None)
    if sandbox is None:
        return f"[sandbox unavailable] Would execute: {command}"

    result = await sandbox.execute(command, profile_name=profile)
    output = result.stdout
    if result.stderr:
        output += f"\nSTDERR: {result.stderr}"
    if result.timed_out:
        output += "\n[TIMED OUT]"
    return output or f"(exit code {result.exit_code})"


async def request_credential(
    deps: ZoomacDeps, key: str, description: str
) -> str:
    """Request a credential from the user."""
    goal_engine = getattr(deps, "goal_engine", None)
    if goal_engine is None or goal_engine._vault is None:
        return f"[credentials unavailable] Need: {key} - {description}"

    existing = goal_engine._vault.retrieve(key)
    if existing:
        return f"Credential '{key}' is available."

    goal_engine._vault.request(key, description)
    return f"Credential '{key}' requested. Waiting for user to provide: {description}"


async def store_goal_context(deps: ZoomacDeps, key: str, value: str) -> str:
    """Store a key-value pair in the current goal context."""
    goal_engine = getattr(deps, "goal_engine", None)
    current_goal_id = getattr(deps, "current_goal_id", None)
    if goal_engine is None or current_goal_id is None:
        return f"[goal context unavailable] Would store: {key}={value[:100]}"

    goal = goal_engine.store.load_goal(current_goal_id)
    if goal is None:
        return "Goal not found."

    goal.context[key] = value
    goal_engine.store.update_goal_context(current_goal_id, goal.context)
    return f"Stored '{key}' in goal context."


async def send_user_message(deps: ZoomacDeps, message: str) -> str:
    """Send a progress message back through the active gateway."""
    gateway = getattr(deps, "gateway", None)
    current_goal_id = getattr(deps, "current_goal_id", None)
    goal_engine = getattr(deps, "goal_engine", None)

    if gateway is None or goal_engine is None or current_goal_id is None:
        return f"[gateway unavailable] Would send: {message}"

    goal = goal_engine.store.load_goal(current_goal_id)
    if goal is None:
        return "Goal not found."

    platform = EventSource(goal.source) if goal.source != "cli" else EventSource.CLI
    outbound = OutboundMessage(
        platform=platform,
        channel=goal.channel,
        content=message,
    )
    success = await gateway.send(outbound)
    return "Message sent." if success else "Failed to send message."


def build_planning_tool_registry() -> ToolRegistry:
    """Create the registry for planning and task tools."""
    registry = ToolRegistry(name="planning")
    registry.extend(
        [
            ToolDefinition(
                spec=ToolSpec(
                    name="write_code",
                    description=(
                        "Write a file to the host project directory. Use relative paths "
                        "from the project root."
                    ),
                    capabilities=ToolCapabilities(writes_project=True),
                    approval_action_type=ActionType.WRITE_FILE.value,
                ),
                handler=write_code,
            ),
            ToolDefinition(
                spec=ToolSpec(
                    name="execute_in_sandbox",
                    description="Execute a shell command in the sandbox.",
                    capabilities=ToolCapabilities(requires_network=True),
                    approval_action_type=ActionType.RUN_COMMAND.value,
                ),
                handler=execute_in_sandbox,
            ),
            ToolDefinition(
                spec=ToolSpec(
                    name="request_credential",
                    description="Request a credential or secret from the user.",
                    approval_action_type=ActionType.MEMORY_QUERY.value,
                ),
                handler=request_credential,
            ),
            ToolDefinition(
                spec=ToolSpec(
                    name="store_goal_context",
                    description="Store a value in the active goal context.",
                    approval_action_type=ActionType.MEMORY_QUERY.value,
                ),
                handler=store_goal_context,
            ),
            ToolDefinition(
                spec=ToolSpec(
                    name="send_user_message",
                    description="Send a progress message to the active user channel.",
                    approval_action_type=ActionType.SEND_MESSAGE.value,
                ),
                handler=send_user_message,
            ),
        ]
    )
    return registry

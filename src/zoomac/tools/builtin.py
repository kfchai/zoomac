"""Built-in tool registry composition."""

from __future__ import annotations

from zoomac.skills.registry import SkillRegistry
from zoomac.tools.coding import build_coding_tool_registry
from zoomac.tools.memory import build_memory_tool_registry
from zoomac.tools.planning import build_planning_tool_registry
from zoomac.tools.registry import ToolRegistry


def build_builtin_tool_registry(
    include_planning: bool = True,
    include_coding: bool = True,
    *,
    skill_registry: SkillRegistry | None = None,
    user_text: str | None = None,
) -> ToolRegistry:
    """Create the default built-in tool registry for the agent runtime."""
    registry = ToolRegistry(name="builtin")
    registry.extend(build_memory_tool_registry().list())
    if include_coding:
        registry.extend(build_coding_tool_registry().list())
    if include_planning:
        registry.extend(build_planning_tool_registry().list())
    if skill_registry is not None:
        return skill_registry.filter_tools(registry, user_text)
    return registry

"""Conversation runtime backed by the Anthropic Messages API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from anthropic import AsyncAnthropic

from zoomac.brain.client import (
    DEFAULT_MAX_TOKENS,
    run_with_tools,
    tool_definitions_to_anthropic,
    tool_handlers_from_registry,
)
from zoomac.brain.deps import ZoomacDeps
from zoomac.brain.memory_extract import AgentResponse
from zoomac.brain.prompts import build_system_prompt
from zoomac.skills.registry import SkillRegistry
from zoomac.tools.builtin import build_builtin_tool_registry
from zoomac.tools.registry import ToolRegistry


@dataclass(slots=True)
class AnthropicConversationRuntime:
    """Conversation runtime that calls the Anthropic Messages API directly."""

    client: AsyncAnthropic
    model: str
    system_prompt: str
    tool_defs: list[dict[str, Any]]
    tool_handlers: dict[str, Any]
    tool_registry: ToolRegistry
    max_tokens: int = DEFAULT_MAX_TOKENS

    async def run(self, prompt: str, *, deps: ZoomacDeps) -> AgentResponse:
        text = await run_with_tools(
            self.client,
            self.model,
            system=self.system_prompt,
            messages=[{"role": "user", "content": prompt}],
            tools=self.tool_defs or None,
            tool_handlers=self.tool_handlers,
            deps=deps,
            max_tokens=self.max_tokens,
        )
        return AgentResponse.model_validate_json(text)


def build_conversation_runtime(
    model: str | Any,
    *,
    include_planning_tools: bool = True,
    skill_registry: SkillRegistry | None = None,
    client: AsyncAnthropic | None = None,
) -> AnthropicConversationRuntime:
    """Build the default conversation runtime for Zoomac."""
    registry = build_builtin_tool_registry(
        include_planning=include_planning_tools,
        skill_registry=skill_registry,
    )
    return AnthropicConversationRuntime(
        client=client or AsyncAnthropic(),
        model=str(model),
        system_prompt=build_system_prompt(skill_registry=skill_registry),
        tool_defs=tool_definitions_to_anthropic(registry),
        tool_handlers=tool_handlers_from_registry(registry),
        tool_registry=registry,
    )

"""Execution helpers for registry-backed tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from zoomac.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A single direct tool invocation."""

    name: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """Result of a direct tool invocation."""

    name: str
    output: str


class ToolExecutor:
    """Runs tools from a registry outside the model loop."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def execute(self, call: ToolCall) -> ToolExecutionResult:
        tool = self._registry.get(call.name)
        output = await tool.execute(*call.args, **call.kwargs)
        return ToolExecutionResult(name=call.name, output=output)

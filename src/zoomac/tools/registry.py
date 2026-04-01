"""Registries for Zoomac tool definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from zoomac.tools.base import ToolDefinition


@dataclass(slots=True)
class ToolRegistry:
    """Stores and registers tool definitions."""

    name: str
    _tools: dict[str, ToolDefinition] = field(default_factory=dict)

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered in '{self.name}'.")
        self._tools[tool.name] = tool

    def extend(self, tools: list[ToolDefinition]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> ToolDefinition:
        return self._tools[name]

    def list(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools)

    def select(self, predicate: Callable[[ToolDefinition], bool]) -> "ToolRegistry":
        subset = ToolRegistry(name=f"{self.name}:filtered")
        for tool in self._tools.values():
            if predicate(tool):
                subset.register(tool)
        return subset

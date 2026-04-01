"""Core tool metadata and registration primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

ToolHandler = Callable[..., Awaitable[str]]


@dataclass(frozen=True, slots=True)
class ToolCapabilities:
    """Static capability flags for a tool."""

    read_only: bool = False
    writes_project: bool = False
    requires_network: bool = False
    supports_parallel: bool = False


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Metadata used for registration, filtering, and policy decisions."""

    name: str
    description: str
    capabilities: ToolCapabilities = field(default_factory=ToolCapabilities)
    approval_action_type: str | None = None


@dataclass(slots=True)
class ToolDefinition:
    """A tool and its Python implementation."""

    spec: ToolSpec
    handler: ToolHandler

    @property
    def name(self) -> str:
        return self.spec.name

    async def execute(self, *args: Any, **kwargs: Any) -> str:
        """Run the tool implementation directly."""
        return await self.handler(*args, **kwargs)

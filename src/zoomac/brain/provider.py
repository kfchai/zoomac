"""Runtime provider abstractions for Zoomac."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from zoomac.brain.runtime import RuntimeBundle, build_runtime_bundle
from zoomac.skills.registry import SkillRegistry


class RuntimeProvider(Protocol):
    """Builds model runtimes for the application."""

    def build(
        self,
        model: str | Any,
        *,
        skill_registry: SkillRegistry | None = None,
    ) -> RuntimeBundle:
        """Create the runtime bundle for a model setting."""


@dataclass(slots=True)
class DefaultRuntimeProvider:
    """Default runtime provider backed by the local model runtime bundle."""

    def build(
        self,
        model: str | Any,
        *,
        skill_registry: SkillRegistry | None = None,
    ) -> RuntimeBundle:
        return build_runtime_bundle(model, skill_registry=skill_registry)

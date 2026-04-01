"""Planner runtime abstractions and Anthropic implementation."""

from __future__ import annotations

from typing import Any, Protocol

from anthropic import AsyncAnthropic

from zoomac.brain.client import DEFAULT_MAX_TOKENS, run_structured
from zoomac.planner.models import PlanDecomposition, ReplanDecision


class PlannerRuntime(Protocol):
    """Interface for planner-oriented model calls."""

    async def decompose(self, prompt: str) -> PlanDecomposition:
        """Produce a structured task plan."""

    async def reason(self, prompt: str) -> str:
        """Produce free-form reasoning text."""

    async def write_artifact(self, prompt: str) -> str:
        """Produce file-oriented output."""

    async def replan(self, prompt: str) -> ReplanDecision | Any:
        """Produce a replan decision."""


class ModelPlannerRuntime:
    """Planner runtime backed by direct Anthropic API calls."""

    def __init__(
        self,
        model: str | Any,
        *,
        client: AsyncAnthropic | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._client = client or AsyncAnthropic()
        self._model = str(model)
        self._max_tokens = max_tokens

    async def decompose(self, prompt: str) -> PlanDecomposition:
        return await run_structured(
            self._client, self._model, prompt, PlanDecomposition,
            max_tokens=self._max_tokens,
        )

    async def reason(self, prompt: str) -> str:
        response = await self._client.messages.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=self._max_tokens,
        )
        return response.content[0].text

    async def write_artifact(self, prompt: str) -> str:
        response = await self._client.messages.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=self._max_tokens,
        )
        return response.content[0].text

    async def replan(self, prompt: str) -> ReplanDecision:
        return await run_structured(
            self._client, self._model, prompt, ReplanDecision,
            max_tokens=self._max_tokens,
        )

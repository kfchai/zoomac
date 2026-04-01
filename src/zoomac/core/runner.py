"""Conversation execution helpers for Zoomac."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from zoomac.brain.deps import ZoomacDeps
from zoomac.brain.memory_extract import AgentResponse
from zoomac.brain.runtime import ConversationRuntime
from zoomac.memory.integration import MemoryManager


@dataclass(slots=True)
class RunnerProgress:
    """A progress event emitted while executing a conversation turn."""

    conversation_id: str
    stage: str
    attempt: int
    detail: str | None = None


@dataclass(slots=True)
class ConversationTurn:
    """One completed or failed conversation turn."""

    user_message: str
    response_message: str | None = None
    attempt_count: int = 1
    memory_payload: dict | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True)
class ConversationState:
    """Conversation-local runtime state tracked by the runner."""

    conversation_id: str
    turns: list[ConversationTurn] = field(default_factory=list)
    retry_count: int = 0
    last_error: str | None = None
    last_memory_payload: dict | None = None
    last_response: str | None = None


@dataclass(slots=True)
class ConversationRunner:
    """Runs one conversation turn against the configured model runtime."""

    runtime: ConversationRuntime
    memory: MemoryManager
    max_retries: int = 1
    _sessions: dict[str, ConversationState] = field(default_factory=dict)

    async def run_message(
        self,
        content: str,
        deps: ZoomacDeps,
        *,
        conversation_id: str = "default",
        progress_callback: Callable[[RunnerProgress], None] | None = None,
    ) -> AgentResponse:
        """Execute a single user message and persist any extracted memory."""
        state = self._sessions.setdefault(
            conversation_id, ConversationState(conversation_id=conversation_id)
        )
        self._emit(progress_callback, conversation_id, "started", 1, content)

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 2):
            self._emit(progress_callback, conversation_id, "agent_call", attempt)
            try:
                response = await self.runtime.run(content, deps=deps)
                payload = None

                if response.memory.worth_remembering and response.memory.content:
                    payload = response.memory.to_memgate_payload()
                    self.memory.ingest_structured(payload)
                    state.last_memory_payload = payload
                    self._emit(progress_callback, conversation_id, "memory_ingested", attempt)

                turn = ConversationTurn(
                    user_message=content,
                    response_message=response.message,
                    attempt_count=attempt,
                    memory_payload=payload,
                )
                state.turns.append(turn)
                state.last_response = response.message
                state.last_error = None
                self._emit(progress_callback, conversation_id, "completed", attempt)
                return response
            except Exception as exc:
                last_error = exc
                state.retry_count += 1
                state.last_error = str(exc)
                self._emit(progress_callback, conversation_id, "retrying", attempt, str(exc))
                if attempt > self.max_retries:
                    state.turns.append(
                        ConversationTurn(
                            user_message=content,
                            attempt_count=attempt,
                            error=str(exc),
                        )
                    )
                    self._emit(progress_callback, conversation_id, "failed", attempt, str(exc))
                    raise

        raise RuntimeError(f"Conversation runner failed unexpectedly: {last_error}")

    def get_state(self, conversation_id: str = "default") -> ConversationState | None:
        """Return the tracked state for a conversation, if any."""
        return self._sessions.get(conversation_id)

    @staticmethod
    def _emit(
        callback: Callable[[RunnerProgress], None] | None,
        conversation_id: str,
        stage: str,
        attempt: int,
        detail: str | None = None,
    ) -> None:
        if callback is None:
            return
        callback(
            RunnerProgress(
                conversation_id=conversation_id,
                stage=stage,
                attempt=attempt,
                detail=detail,
            )
        )

"""Zoomac core event loop."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

from zoomac.brain.agent import ZoomacDeps, create_agent
from zoomac.brain.memory_extract import AgentResponse
from zoomac.core.config import ZoomacSettings
from zoomac.core.events import Event, EventSource, MessageEvent, ScheduleEvent, SystemEvent
from zoomac.core.queue import EventQueue
from zoomac.memory.integration import MemoryManager

if TYPE_CHECKING:
    from pydantic_ai import Agent


class CoreLoop:
    """Main event loop for the Zoomac agent."""

    def __init__(self, settings: ZoomacSettings, model_override=None) -> None:
        self.settings = settings
        self.memory = MemoryManager(
            project_dir=str(settings.project_dir),
            max_tokens=settings.memory_max_tokens,
            top_k=settings.memory_top_k,
        )
        self.agent: Agent[ZoomacDeps, AgentResponse] = create_agent(
            model_override or settings.model
        )
        self.deps = ZoomacDeps(memgate=self.memory)
        self.queue = EventQueue(settings.project_dir / ".zoomac_events.db")
        self._running = False

    async def handle_message(self, event: MessageEvent) -> str:
        """Process a single message event through the agent."""
        result = await self.agent.run(event.content, deps=self.deps)
        response: AgentResponse = result.output

        # Ingest memory if worth remembering
        if response.memory.worth_remembering and response.memory.content:
            payload = response.memory.to_memgate_payload()
            self.memory.ingest_structured(payload)

        return response.message

    async def handle_event(self, event: Event) -> str | None:
        """Route an event to the appropriate handler."""
        if isinstance(event, MessageEvent):
            return await self.handle_message(event)
        elif isinstance(event, ScheduleEvent):
            # Placeholder — will be implemented in Phase 9
            return f"[scheduler] {event.job_name}: {event.task}"
        elif isinstance(event, SystemEvent):
            return f"[system] {event.event_type}: {event.detail}"
        return None

    async def process_queue(self) -> None:
        """Process events from the queue continuously."""
        self._running = True

        # Recover any events stuck in 'processing' from a previous crash
        recovered = self.queue.recover_stale()
        if recovered > 0:
            print(f"  Recovered {recovered} stale event(s) from previous session.")

        while self._running:
            event = await self.queue.pop_async()
            try:
                result = await self.handle_event(event)
                self.queue.complete(event.id)
                if result and isinstance(event, MessageEvent):
                    yield event, result
            except Exception as e:
                self.queue.fail(event.id, str(e))
                yield event, f"[error] {e}"

    async def run_cli(self) -> None:
        """Run the agent in interactive CLI mode with event queue."""
        self._running = True
        print("Zoomac Agent v0.1.0")
        print(f"Model: {self.settings.model}")
        print(f"Memory: {self.settings.memgate_db_path}")

        # Recover stale events
        recovered = self.queue.recover_stale()
        if recovered > 0:
            print(f"  Recovered {recovered} stale event(s).")

        pending = self.queue.pending_count()
        if pending > 0:
            print(f"  {pending} pending event(s) in queue.")

        print("Type 'quit' or 'exit' to stop.\n")

        loop = asyncio.get_event_loop()

        # Start background queue processor
        processor_task = asyncio.create_task(self._process_background())

        try:
            while self._running:
                try:
                    user_input = await loop.run_in_executor(
                        None, lambda: input("you> ")
                    )
                except (EOFError, KeyboardInterrupt):
                    print("\nGoodbye.")
                    break

                user_input = user_input.strip()
                if not user_input:
                    continue
                if user_input.lower() in ("quit", "exit"):
                    print("Goodbye.")
                    break

                # Handle special commands
                if user_input == "/status":
                    mem_status = self.memory.status()
                    print(f"  Memories: {mem_status.get('n_memories', 0)}")
                    print(f"  Ingested: {mem_status.get('total_ingested', 0)}")
                    print(f"  Filter rate: {mem_status.get('filter_rate', 0):.1%}")
                    print(f"  Clusters: {mem_status.get('n_clusters', 0)}")
                    print(f"  Queue pending: {self.queue.pending_count()}")
                    print(f"  Dead letters: {self.queue.dead_letter_count()}")
                    continue

                # Push to event queue
                event = MessageEvent(
                    source=EventSource.CLI,
                    channel="cli",
                    author="user",
                    content=user_input,
                )
                self.queue.push(event)

                # Wait for this specific event to be processed
                response = await self._wait_for_response(event.id)
                print(f"\nzoomac> {response}\n")
        finally:
            self._running = False
            processor_task.cancel()
            try:
                await processor_task
            except asyncio.CancelledError:
                pass

    async def _process_background(self) -> None:
        """Background task that processes events from the queue."""
        while self._running:
            event = await self.queue.pop_async()
            if event is None:
                continue
            try:
                result = await self.handle_event(event)
                self.queue.complete(event.id)
                # Store result for CLI retrieval
                self._last_results[event.id] = result or ""
            except Exception as e:
                self.queue.fail(event.id, str(e))
                self._last_results[event.id] = f"[error] {e}"
            self._response_ready.set()

    async def _wait_for_response(self, event_id: str, timeout: float = 120.0) -> str:
        """Wait for a specific event's response."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if event_id in self._last_results:
                return self._last_results.pop(event_id)
            self._response_ready.clear()
            try:
                await asyncio.wait_for(self._response_ready.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
        return "[timeout] No response within timeout."

    @property
    def _last_results(self) -> dict[str, str]:
        if not hasattr(self, "_results_cache"):
            self._results_cache: dict[str, str] = {}
        return self._results_cache

    @property
    def _response_ready(self) -> asyncio.Event:
        if not hasattr(self, "_response_event"):
            self._response_event = asyncio.Event()
        return self._response_event

    def shutdown(self) -> None:
        """Graceful shutdown."""
        self._running = False
        self.queue.close()
        self.memory.close()

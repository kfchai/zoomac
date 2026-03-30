"""Zoomac core event loop."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

from zoomac.brain.agent import ZoomacDeps, create_agent
from zoomac.brain.memory_extract import AgentResponse
from zoomac.core.config import ZoomacSettings
from zoomac.core.events import EventSource, MessageEvent
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
        self._running = False

    async def handle_message(self, event: MessageEvent) -> str:
        """Process a single message event through the agent."""
        # Run the agent
        result = await self.agent.run(event.content, deps=self.deps)
        response: AgentResponse = result.output

        # Ingest memory if worth remembering
        if response.memory.worth_remembering and response.memory.content:
            payload = response.memory.to_memgate_payload()
            self.memory.ingest_structured(payload)

        return response.message

    async def run_cli(self) -> None:
        """Run the agent in interactive CLI mode."""
        self._running = True
        print("Zoomac Agent v0.1.0")
        print(f"Model: {self.settings.model}")
        print(f"Memory: {self.settings.memgate_db_path}")
        print("Type 'quit' or 'exit' to stop.\n")

        loop = asyncio.get_event_loop()

        while self._running:
            try:
                # Read input without blocking the event loop
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
                status = self.memory.status()
                print(f"  Memories: {status.get('n_memories', 0)}")
                print(f"  Ingested: {status.get('total_ingested', 0)}")
                print(f"  Filter rate: {status.get('filter_rate', 0):.1%}")
                print(f"  Clusters: {status.get('n_clusters', 0)}")
                continue

            # Create message event
            event = MessageEvent(
                source=EventSource.CLI,
                channel="cli",
                author="user",
                content=user_input,
            )

            try:
                response = await self.handle_message(event)
                print(f"\nzoomac> {response}\n")
            except Exception as e:
                print(f"\n[error] {e}\n", file=sys.stderr)

    def shutdown(self) -> None:
        """Graceful shutdown."""
        self._running = False
        self.memory.close()

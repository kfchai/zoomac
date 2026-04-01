"""Zoomac core event loop."""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from zoomac.autonomy.policy import AutonomyManager
from zoomac.agents.manager import SubAgentManager
from zoomac.brain.deps import ZoomacDeps
from zoomac.brain.provider import DefaultRuntimeProvider, RuntimeProvider
from zoomac.core.config import ZoomacSettings
from zoomac.core.events import Event, EventSource, MessageEvent, ScheduleEvent, SystemEvent
from zoomac.core.queue import EventQueue
from zoomac.core.runner import ConversationRunner
from zoomac.memory.integration import MemoryManager
from zoomac.planner.credentials import CredentialVault
from zoomac.planner.engine import GoalEngine
from zoomac.planner.store import GoalStore
from zoomac.scheduler import SchedulerService
from zoomac.skills.loader import load_builtin_skill_registry

logger = logging.getLogger(__name__)

GOAL_PREFIX = "/goal "


def _is_goal_instruction(text: str) -> bool:
    """Check if a message is a goal instruction."""
    return text.strip().lower().startswith(GOAL_PREFIX)


class CoreLoop:
    """Main event loop for the Zoomac agent."""

    def __init__(
        self,
        settings: ZoomacSettings,
        model_override=None,
        runtime_provider: RuntimeProvider | None = None,
    ) -> None:
        self.settings = settings
        self.memory = MemoryManager(
            project_dir=str(settings.project_dir),
            max_tokens=settings.memory_max_tokens,
            top_k=settings.memory_top_k,
        )
        self.runtime_provider = runtime_provider or DefaultRuntimeProvider()
        self.skill_registry = load_builtin_skill_registry()
        runtime_bundle = self.runtime_provider.build(
            model_override or settings.model,
            skill_registry=self.skill_registry,
        )
        self.conversation_runtime = runtime_bundle.conversation_runtime
        self.runner = ConversationRunner(runtime=self.conversation_runtime, memory=self.memory)
        self.planner_runtime = runtime_bundle.planner_runtime
        self.agent_manager = SubAgentManager(
            planner_runtime=self.planner_runtime,
            max_agents=settings.max_sub_agents,
        )
        # Autonomy
        autonomy_path = settings.project_dir / settings.autonomy_config
        audit_db = settings.project_dir / ".zoomac_audit.db"
        self.autonomy = AutonomyManager(config_path=autonomy_path, db_path=audit_db)

        # Goal planner
        self._goal_store = GoalStore(settings.project_dir / ".zoomac_goals.db")
        self._vault = CredentialVault(self._goal_store, encryption_key=settings.secret_key)
        self._goal_engine = GoalEngine(
            store=self._goal_store,
            planner_runtime=self.planner_runtime,
            autonomy=self.autonomy,
            agent_manager=self.agent_manager,
            skill_registry=self.skill_registry,
            memory=self.memory,
            credential_vault=self._vault,
            project_dir=str(settings.project_dir),
        )

        self.deps = ZoomacDeps(
            memgate=self.memory,
            autonomy=self.autonomy,
            goal_engine=self._goal_engine,
        )
        self.queue = EventQueue(settings.project_dir / ".zoomac_events.db")
        self.scheduler = SchedulerService(self.queue)
        self.scheduler.register_default_jobs()
        self.gateway = None  # Set by run_server() for outbound routing
        self._running = False
        self._goal_tasks: dict[str, asyncio.Task] = {}
        self._scheduler_started = False

    async def handle_message(self, event: MessageEvent) -> str:
        """Process a single message event through the agent."""
        conversation_id = f"{event.source.value}:{event.channel}"
        response = await self.runner.run_message(
            event.content,
            deps=self.deps,
            conversation_id=conversation_id,
        )
        return response.message

    async def handle_event(self, event: Event) -> str | None:
        """Route an event to the appropriate handler."""
        if isinstance(event, MessageEvent):
            # Handle /status command from any platform
            if event.content.strip() == "/status":
                import json
                mem_status = self.memory.status()
                return json.dumps({
                    "memories": mem_status.get("n_memories", 0),
                    "ingested": mem_status.get("total_ingested", 0),
                    "pending": self.queue.pending_count(),
                    "dead_letters": self.queue.dead_letter_count(),
                    "active_goals": len(self._goal_tasks),
                })

            # Check if this is input for a blocked goal
            blocked = self._goal_store.find_blocked_goal(
                source=event.source.value, channel=event.channel
            )
            if blocked:
                reply = await self._goal_engine.handle_user_input(blocked.id, event.content)
                # Resume goal execution in background
                if blocked.id not in self._goal_tasks:
                    self._goal_tasks[blocked.id] = asyncio.create_task(
                        self._run_goal_background(blocked.id)
                    )
                return reply

            # Check if this is a goal instruction
            if _is_goal_instruction(event.content):
                instruction = event.content[len(GOAL_PREFIX):].strip()
                return await self._start_goal(instruction, event.source.value, event.channel)

            return await self.handle_message(event)
        elif isinstance(event, ScheduleEvent):
            return await self.handle_schedule(event)
        elif isinstance(event, SystemEvent):
            return f"[system] {event.event_type}: {event.detail}"
        return None

    async def handle_schedule(self, event: ScheduleEvent) -> str:
        """Process a scheduled maintenance or delegated background job."""
        if event.spawn_agent or event.metadata.get("goal_instruction"):
            instruction = str(event.metadata.get("goal_instruction") or event.task).strip()
            goal = await self._goal_engine.create_goal(
                instruction,
                source=EventSource.SCHEDULER.value,
                channel=event.job_name,
            )
            self._goal_tasks[goal.id] = asyncio.create_task(
                self._run_goal_background(goal.id)
            )
            return f"[scheduler] goal queued: {goal.id}"

        if event.task == "memory_consolidate":
            consolidated = self.memory.consolidate()
            return f"[scheduler] memory consolidated: {len(consolidated)} item(s)"

        if event.task == "health_check":
            mem_status = self.memory.status()
            return json.dumps(
                {
                    "job": event.job_name,
                    "pending": self.queue.pending_count(),
                    "dead_letters": self.queue.dead_letter_count(),
                    "active_goals": len(self._goal_tasks),
                    "memories": mem_status.get("n_memories", 0),
                }
            )

        if event.task == "retry_dead_letters":
            replayed = self.queue.replay_all_dead_letters()
            return f"[scheduler] replayed {replayed} dead-letter event(s)"

        return f"[scheduler] {event.job_name}: {event.task}"

    async def _start_goal(self, instruction: str, source: str, channel: str) -> str:
        """Create and start executing a goal."""
        try:
            goal = await self._goal_engine.create_goal(instruction, source, channel)
            # Run in background
            self._goal_tasks[goal.id] = asyncio.create_task(
                self._run_goal_background(goal.id)
            )
            return f"Goal accepted ({len(goal.tasks)} tasks planned). Working on: {instruction[:80]}"
        except Exception as e:
            logger.exception("Failed to create goal")
            return f"Failed to plan goal: {e}"

    async def _run_goal_background(self, goal_id: str) -> None:
        """Run a goal to completion in the background."""
        try:
            goal = await self._goal_engine.run_goal(goal_id)
            if goal:
                logger.info("Goal %s finished with status: %s", goal_id, goal.status.value)
        except Exception:
            logger.exception("Background goal %s failed", goal_id)
        finally:
            self._goal_tasks.pop(goal_id, None)

    async def process_queue(self) -> None:
        """Process events from the queue continuously."""
        self._running = True
        await self._start_scheduler()

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
        await self._start_scheduler()
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
        self.autonomy.close()
        self.memory.close()
        self._goal_store.close()

    async def _start_scheduler(self) -> None:
        if self._scheduler_started:
            return
        await self.scheduler.start()
        self._scheduler_started = True

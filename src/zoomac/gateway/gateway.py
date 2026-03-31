"""Gateway — multiplexes platform adapters into the core event queue."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from zoomac.core.events import EventPriority, EventSource
from zoomac.core.queue import EventQueue
from zoomac.gateway.adapter import (
    AdapterHealth,
    NormalizedMessage,
    OutboundMessage,
    PlatformAdapter,
)

logger = logging.getLogger(__name__)


class Gateway:
    """Multiplexes multiple platform adapters into a single event queue.

    Each adapter's listen() runs as an independent async task.
    Outbound messages are routed to the correct adapter by platform.
    """

    def __init__(self, queue: EventQueue) -> None:
        self._queue = queue
        self._adapters: dict[EventSource, PlatformAdapter] = {}
        self._tasks: dict[EventSource, asyncio.Task] = {}
        self._running = False

    def register(self, adapter: PlatformAdapter) -> None:
        """Register a platform adapter."""
        self._adapters[adapter.platform] = adapter
        logger.info("Registered adapter: %s", adapter.platform.value)

    async def start(self) -> None:
        """Connect all adapters and start listening."""
        self._running = True
        for platform, adapter in self._adapters.items():
            try:
                await adapter.connect()
                task = asyncio.create_task(
                    self._listen_loop(adapter),
                    name=f"gateway-{platform.value}",
                )
                self._tasks[platform] = task
                logger.info("Started listener: %s", platform.value)
            except Exception as e:
                logger.error("Failed to connect %s: %s", platform.value, e)

    async def stop(self) -> None:
        """Disconnect all adapters and cancel listener tasks."""
        self._running = False
        for platform, task in self._tasks.items():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

        for adapter in self._adapters.values():
            try:
                await adapter.disconnect()
            except Exception as e:
                logger.error("Error disconnecting %s: %s", adapter.platform.value, e)

    async def send(self, message: OutboundMessage) -> bool:
        """Route an outbound message to the correct adapter."""
        adapter = self._adapters.get(message.platform)
        if adapter is None:
            logger.warning("No adapter for platform: %s", message.platform.value)
            return False
        try:
            return await adapter.send(message)
        except Exception as e:
            logger.error("Send failed on %s: %s", message.platform.value, e)
            return False

    async def health(self) -> dict[str, AdapterHealth]:
        """Get health status of all adapters."""
        result = {}
        for platform, adapter in self._adapters.items():
            try:
                result[platform.value] = await adapter.health()
            except Exception as e:
                result[platform.value] = AdapterHealth(
                    platform=platform,
                    connected=False,
                    error=str(e),
                )
        return result

    @property
    def platforms(self) -> list[EventSource]:
        """List of registered platforms."""
        return list(self._adapters.keys())

    async def _listen_loop(self, adapter: PlatformAdapter) -> None:
        """Listen for messages from an adapter and push to the event queue."""
        backoff = 1
        while self._running:
            try:
                async for msg in adapter.listen():
                    if not self._running:
                        break
                    event = adapter.normalized_to_event(msg)
                    event.priority = EventPriority.HIGH  # Direct user messages
                    self._queue.push(event)
                    logger.debug("Queued message from %s/%s", msg.platform.value, msg.channel)
                # listen() returned normally — reconnect
                backoff = 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Listener error on %s: %s (retry in %ds)", adapter.platform.value, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)  # Exponential backoff, cap at 60s

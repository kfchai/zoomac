"""Abstract adapter interface for messaging platform connectors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from pydantic import BaseModel, Field

from zoomac.core.events import EventSource, MessageEvent


class NormalizedMessage(BaseModel):
    """Platform-agnostic inbound message."""

    platform: EventSource
    channel: str
    author: str
    content: str
    attachments: list[str] = Field(default_factory=list)
    reply_to: str | None = None
    metadata: dict = Field(default_factory=dict)


class OutboundMessage(BaseModel):
    """Message to send back to a platform."""

    platform: EventSource
    channel: str
    content: str
    reply_to: str | None = None
    metadata: dict = Field(default_factory=dict)


class AdapterHealth(BaseModel):
    """Health status of a platform adapter."""

    platform: EventSource
    connected: bool
    latency_ms: int | None = None
    error: str | None = None


class PlatformAdapter(ABC):
    """Abstract base class for messaging platform adapters.

    Each platform (Telegram, WhatsApp, Discord) implements this interface.
    The gateway multiplexes multiple adapters into the core event queue.
    """

    @property
    @abstractmethod
    def platform(self) -> EventSource:
        """The platform this adapter serves."""
        ...

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the platform. Raise on failure."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully disconnect from the platform."""
        ...

    @abstractmethod
    async def listen(self) -> AsyncIterator[NormalizedMessage]:
        """Yield normalized inbound messages as they arrive.

        This is an async generator that runs indefinitely until disconnect.
        Implementations should handle reconnection internally.
        """
        ...
        # Make this an async generator
        if False:
            yield  # pragma: no cover

    @abstractmethod
    async def send(self, message: OutboundMessage) -> bool:
        """Send a message to the platform. Returns True on success."""
        ...

    @abstractmethod
    async def health(self) -> AdapterHealth:
        """Return the current health status of this adapter."""
        ...

    def normalized_to_event(self, msg: NormalizedMessage) -> MessageEvent:
        """Convert a NormalizedMessage to a MessageEvent for the queue."""
        return MessageEvent(
            source=msg.platform,
            channel=msg.channel,
            author=msg.author,
            content=msg.content,
            attachments=msg.attachments,
            reply_to=msg.reply_to,
            metadata=msg.metadata,
        )

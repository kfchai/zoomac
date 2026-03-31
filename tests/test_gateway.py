"""Phase 5 tests — Gateway, adapter interface, multiplexer, and Telegram adapter."""

import asyncio
import sys
from pathlib import Path
from typing import AsyncIterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memgate"))

from zoomac.core.events import EventPriority, EventSource, MessageEvent
from zoomac.core.queue import EventQueue
from zoomac.gateway.adapter import (
    AdapterHealth,
    NormalizedMessage,
    OutboundMessage,
    PlatformAdapter,
)
from zoomac.gateway.gateway import Gateway


# --- Mock adapter for testing ---


class MockAdapter(PlatformAdapter):
    """A mock adapter that yields pre-loaded messages."""

    def __init__(
        self,
        platform: EventSource = EventSource.TELEGRAM,
        messages: list[NormalizedMessage] | None = None,
    ):
        self._platform = platform
        self._messages = messages or []
        self._sent: list[OutboundMessage] = []
        self._connected = False
        self._connect_error: Exception | None = None
        self._send_fail = False

    @property
    def platform(self) -> EventSource:
        return self._platform

    async def connect(self) -> None:
        if self._connect_error:
            raise self._connect_error
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def listen(self) -> AsyncIterator[NormalizedMessage]:
        for msg in self._messages:
            yield msg
        # After yielding all messages, wait until disconnected
        while self._connected:
            await asyncio.sleep(0.05)

    async def send(self, message: OutboundMessage) -> bool:
        if self._send_fail:
            return False
        self._sent.append(message)
        return True

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            platform=self._platform,
            connected=self._connected,
        )


# --- Adapter interface tests ---


def test_normalized_message_creation():
    """NormalizedMessage can be created with all fields."""
    msg = NormalizedMessage(
        platform=EventSource.TELEGRAM,
        channel="12345",
        author="alice",
        content="Hello!",
        reply_to="999",
        metadata={"chat_type": "private"},
    )
    assert msg.platform == EventSource.TELEGRAM
    assert msg.content == "Hello!"


def test_outbound_message_creation():
    """OutboundMessage can be created with all fields."""
    msg = OutboundMessage(
        platform=EventSource.DISCORD,
        channel="general",
        content="Response here",
        reply_to="msg_42",
    )
    assert msg.platform == EventSource.DISCORD


def test_adapter_health_creation():
    """AdapterHealth model works."""
    h = AdapterHealth(platform=EventSource.WHATSAPP, connected=True)
    assert h.connected is True
    assert h.error is None


def test_normalized_to_event():
    """Adapter converts NormalizedMessage to MessageEvent."""
    adapter = MockAdapter()
    msg = NormalizedMessage(
        platform=EventSource.TELEGRAM,
        channel="12345",
        author="bob",
        content="Test message",
        metadata={"msg_id": 42},
    )
    event = adapter.normalized_to_event(msg)

    assert isinstance(event, MessageEvent)
    assert event.source == EventSource.TELEGRAM
    assert event.channel == "12345"
    assert event.author == "bob"
    assert event.content == "Test message"
    assert event.metadata == {"msg_id": 42}


# --- Mock adapter tests ---


@pytest.mark.asyncio
async def test_mock_adapter_connect_disconnect():
    """Mock adapter connects and disconnects."""
    adapter = MockAdapter()
    assert adapter._connected is False

    await adapter.connect()
    assert adapter._connected is True

    health = await adapter.health()
    assert health.connected is True

    await adapter.disconnect()
    assert adapter._connected is False


@pytest.mark.asyncio
async def test_mock_adapter_listen():
    """Mock adapter yields pre-loaded messages."""
    messages = [
        NormalizedMessage(platform=EventSource.TELEGRAM, channel="1", author="a", content="msg1"),
        NormalizedMessage(platform=EventSource.TELEGRAM, channel="1", author="a", content="msg2"),
    ]
    adapter = MockAdapter(messages=messages)
    await adapter.connect()

    received = []
    # Collect messages with a timeout
    async def collect():
        async for msg in adapter.listen():
            received.append(msg)
            if len(received) >= 2:
                await adapter.disconnect()

    await asyncio.wait_for(collect(), timeout=2.0)
    assert len(received) == 2
    assert received[0].content == "msg1"
    assert received[1].content == "msg2"


@pytest.mark.asyncio
async def test_mock_adapter_send():
    """Mock adapter records sent messages."""
    adapter = MockAdapter()
    await adapter.connect()

    msg = OutboundMessage(
        platform=EventSource.TELEGRAM,
        channel="12345",
        content="Reply",
    )
    result = await adapter.send(msg)
    assert result is True
    assert len(adapter._sent) == 1
    assert adapter._sent[0].content == "Reply"

    await adapter.disconnect()


# --- Gateway multiplexer tests ---


@pytest.fixture
def queue(tmp_path):
    q = EventQueue(tmp_path / "gateway_test.db")
    yield q
    q.close()


def test_gateway_register(queue):
    """Gateway registers adapters."""
    gw = Gateway(queue)
    adapter = MockAdapter(platform=EventSource.TELEGRAM)
    gw.register(adapter)
    assert EventSource.TELEGRAM in gw.platforms


def test_gateway_multiple_adapters(queue):
    """Gateway supports multiple adapters."""
    gw = Gateway(queue)
    gw.register(MockAdapter(platform=EventSource.TELEGRAM))
    gw.register(MockAdapter(platform=EventSource.DISCORD))
    assert len(gw.platforms) == 2


@pytest.mark.asyncio
async def test_gateway_start_stop(queue):
    """Gateway starts and stops cleanly."""
    gw = Gateway(queue)
    gw.register(MockAdapter(platform=EventSource.TELEGRAM))

    await gw.start()
    assert gw._running is True

    await gw.stop()
    assert gw._running is False


@pytest.mark.asyncio
async def test_gateway_routes_messages_to_queue(queue):
    """Gateway pushes adapter messages into the event queue."""
    messages = [
        NormalizedMessage(platform=EventSource.TELEGRAM, channel="1", author="alice", content="hello from tg"),
    ]
    adapter = MockAdapter(platform=EventSource.TELEGRAM, messages=messages)
    gw = Gateway(queue)
    gw.register(adapter)

    await gw.start()
    # Give the listener time to process
    await asyncio.sleep(0.3)
    await gw.stop()

    # Check the event queue
    assert queue.pending_count() >= 1
    event = queue.pop()
    assert isinstance(event, MessageEvent)
    assert event.source == EventSource.TELEGRAM
    assert event.content == "hello from tg"
    assert event.priority == EventPriority.HIGH


@pytest.mark.asyncio
async def test_gateway_routes_from_multiple_adapters(queue):
    """Messages from multiple adapters all reach the queue."""
    tg_msgs = [NormalizedMessage(platform=EventSource.TELEGRAM, channel="1", author="a", content="from tg")]
    dc_msgs = [NormalizedMessage(platform=EventSource.DISCORD, channel="2", author="b", content="from dc")]

    gw = Gateway(queue)
    gw.register(MockAdapter(platform=EventSource.TELEGRAM, messages=tg_msgs))
    gw.register(MockAdapter(platform=EventSource.DISCORD, messages=dc_msgs))

    await gw.start()
    await asyncio.sleep(0.3)
    await gw.stop()

    events = []
    while queue.pending_count() > 0:
        e = queue.pop()
        if e:
            events.append(e)

    sources = {e.source for e in events}
    assert EventSource.TELEGRAM in sources
    assert EventSource.DISCORD in sources


@pytest.mark.asyncio
async def test_gateway_send_routes_to_adapter(queue):
    """Gateway.send() routes to the correct adapter."""
    adapter = MockAdapter(platform=EventSource.TELEGRAM)
    gw = Gateway(queue)
    gw.register(adapter)
    await gw.start()

    msg = OutboundMessage(
        platform=EventSource.TELEGRAM,
        channel="12345",
        content="response",
    )
    result = await gw.send(msg)
    assert result is True
    assert len(adapter._sent) == 1

    await gw.stop()


@pytest.mark.asyncio
async def test_gateway_send_unknown_platform(queue):
    """Sending to unregistered platform returns False."""
    gw = Gateway(queue)
    msg = OutboundMessage(
        platform=EventSource.WHATSAPP,
        channel="1",
        content="no adapter",
    )
    result = await gw.send(msg)
    assert result is False


@pytest.mark.asyncio
async def test_gateway_health(queue):
    """Gateway reports health of all adapters."""
    adapter = MockAdapter(platform=EventSource.TELEGRAM)
    gw = Gateway(queue)
    gw.register(adapter)
    await gw.start()

    health = await gw.health()
    assert "telegram" in health
    assert health["telegram"].connected is True

    await gw.stop()


@pytest.mark.asyncio
async def test_gateway_handles_connect_failure(queue):
    """Gateway handles adapter connection failures gracefully."""
    adapter = MockAdapter(platform=EventSource.TELEGRAM)
    adapter._connect_error = RuntimeError("Connection refused")
    gw = Gateway(queue)
    gw.register(adapter)

    # Should not raise
    await gw.start()
    # Adapter should not be in running tasks
    assert EventSource.TELEGRAM not in gw._tasks
    await gw.stop()


# --- TelegramAdapter import/init test ---


def test_telegram_adapter_init():
    """TelegramAdapter initializes without connecting."""
    from zoomac.gateway.telegram import TelegramAdapter

    adapter = TelegramAdapter(token="test:token", allowed_users=[12345])
    assert adapter.platform == EventSource.TELEGRAM
    assert adapter._connected is False
    assert 12345 in adapter._allowed_users

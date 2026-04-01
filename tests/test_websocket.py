"""Tests for WebSocket adapter."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memgate"))

import websockets

from zoomac.core.events import EventSource
from zoomac.gateway.adapter import AdapterHealth, OutboundMessage
from zoomac.gateway.websocket import WebSocketAdapter


def test_websocket_adapter_init():
    """Adapter initializes with correct defaults."""
    adapter = WebSocketAdapter(host="127.0.0.1", port=9999)
    assert adapter.platform == EventSource.VSCODE
    assert adapter._connected is False
    assert adapter.client_count == 0
    assert adapter.session_count == 0


@pytest.mark.asyncio
async def test_connect_starts_server():
    """connect() starts a WebSocket server."""
    adapter = WebSocketAdapter(host="127.0.0.1", port=0)
    await adapter.connect()
    assert adapter._connected is True
    assert adapter._server is not None
    assert adapter.get_port() is not None
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_disconnect_stops_server():
    """disconnect() cleanly stops the server."""
    adapter = WebSocketAdapter(host="127.0.0.1", port=0)
    await adapter.connect()
    port = adapter.get_port()
    assert port is not None

    await adapter.disconnect()
    assert adapter._connected is False
    assert adapter._server is None
    assert adapter.client_count == 0


@pytest.mark.asyncio
async def test_welcome_on_connect():
    """Client receives a welcome message with channel on connect."""
    adapter = WebSocketAdapter(host="127.0.0.1", port=0)
    await adapter.connect()
    port = adapter.get_port()

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
        data = json.loads(raw)
        assert data["type"] == "connected"
        assert data["channel"].startswith("vscode-")
        assert data["session_id"].startswith("ws-")
        assert data["protocol_version"] == 1
        assert "Zoomac" in data["content"]

    await adapter.disconnect()


@pytest.mark.asyncio
async def test_client_message_roundtrip():
    """Client message appears in the adapter's message queue."""
    adapter = WebSocketAdapter(host="127.0.0.1", port=0)
    await adapter.connect()
    port = adapter.get_port()

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        # Consume welcome
        await asyncio.wait_for(ws.recv(), timeout=2.0)

        # Send a message
        await ws.send(json.dumps({"type": "message", "content": "hello agent"}))

        # Should appear in the queue
        msg = await asyncio.wait_for(adapter._message_queue.get(), timeout=2.0)
        assert msg.content == "hello agent"
        assert msg.platform == EventSource.VSCODE

    await adapter.disconnect()


@pytest.mark.asyncio
async def test_send_to_client():
    """send() delivers a message to the connected client."""
    adapter = WebSocketAdapter(host="127.0.0.1", port=0)
    await adapter.connect()
    port = adapter.get_port()

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        # Get welcome and channel
        welcome = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
        channel = welcome["channel"]

        # Send outbound message via adapter
        outbound = OutboundMessage(
            platform=EventSource.VSCODE,
            channel=channel,
            content="agent reply here",
        )
        result = await adapter.send(outbound)
        assert result is True

        # Client should receive it
        raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
        data = json.loads(raw)
        assert data["type"] == "response"
        assert data["content"] == "agent reply here"
        assert data["channel"] == channel
        assert data["session_id"] == welcome["session_id"]
        assert data["sequence"] == 1

    await adapter.disconnect()


@pytest.mark.asyncio
async def test_client_disconnect_handled():
    """Client disconnecting doesn't crash the adapter."""
    adapter = WebSocketAdapter(host="127.0.0.1", port=0)
    await adapter.connect()
    port = adapter.get_port()

    # Connect and immediately disconnect
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await asyncio.wait_for(ws.recv(), timeout=2.0)  # consume welcome
        assert adapter.client_count == 1

    # Give the handler time to clean up
    await asyncio.sleep(0.1)
    assert adapter.client_count == 0

    # Adapter should still be running fine
    assert adapter._connected is True
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_health_reports_correctly():
    """health() reflects server state."""
    adapter = WebSocketAdapter(host="127.0.0.1", port=0)

    # Not connected
    health = await adapter.health()
    assert health.connected is False
    assert health.error is not None

    # Connected
    await adapter.connect()
    health = await adapter.health()
    assert health.connected is True
    assert health.platform == EventSource.VSCODE

    await adapter.disconnect()

    # Disconnected again
    health = await adapter.health()
    assert health.connected is False


@pytest.mark.asyncio
async def test_reconnect_with_session_query_replays_history():
    """Reconnecting with the same session_id replays prior outbound events."""
    adapter = WebSocketAdapter(host="127.0.0.1", port=0)
    await adapter.connect()
    port = adapter.get_port()

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        welcome = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
        channel = welcome["channel"]
        session_id = welcome["session_id"]
        outbound = OutboundMessage(
            platform=EventSource.VSCODE,
            channel=channel,
            content="first reply",
        )
        assert await adapter.send(outbound) is True
        live = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
        assert live["sequence"] == 1
        assert live.get("replay") is None

    await asyncio.sleep(0.1)

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/?session_id={session_id}"
    ) as ws2:
        resumed = json.loads(await asyncio.wait_for(ws2.recv(), timeout=2.0))
        assert resumed["type"] == "session_resumed"
        assert resumed["session_id"] == session_id
        replayed = json.loads(await asyncio.wait_for(ws2.recv(), timeout=2.0))
        assert replayed["content"] == "first reply"
        assert replayed["replay"] is True
        assert replayed["sequence"] == 1

    await adapter.disconnect()


@pytest.mark.asyncio
async def test_resume_message_switches_to_existing_session():
    """A client can explicitly resume a known session after connect."""
    adapter = WebSocketAdapter(host="127.0.0.1", port=0)
    await adapter.connect()
    port = adapter.get_port()

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws1:
        welcome1 = json.loads(await asyncio.wait_for(ws1.recv(), timeout=2.0))
        session_id = welcome1["session_id"]
        channel = welcome1["channel"]
        assert await adapter.send(
            OutboundMessage(
                platform=EventSource.VSCODE,
                channel=channel,
                content="persist me",
            )
        )
        await asyncio.wait_for(ws1.recv(), timeout=2.0)

    await asyncio.sleep(0.1)

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws2:
        welcome2 = json.loads(await asyncio.wait_for(ws2.recv(), timeout=2.0))
        assert welcome2["session_id"] != session_id
        await ws2.send(json.dumps({"type": "resume", "session_id": session_id}))
        resumed = json.loads(await asyncio.wait_for(ws2.recv(), timeout=2.0))
        assert resumed["type"] == "session_resumed"
        assert resumed["session_id"] == session_id
        replayed = json.loads(await asyncio.wait_for(ws2.recv(), timeout=2.0))
        assert replayed["replay"] is True
        assert replayed["content"] == "persist me"

    await adapter.disconnect()

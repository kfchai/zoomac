"""WebSocket server adapter for VS Code extension communication."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse
from typing import AsyncIterator

from zoomac.core.events import EventSource
from zoomac.gateway.adapter import (
    AdapterHealth,
    NormalizedMessage,
    OutboundMessage,
    PlatformAdapter,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WebSocketSession:
    """Tracked client session for reconnect and replay support."""

    session_id: str
    channel: str
    websocket: object | None = None
    sequence: int = 0
    history: deque[dict] = field(default_factory=lambda: deque(maxlen=100))


class WebSocketAdapter(PlatformAdapter):
    """WebSocket server adapter.

    Unlike TelegramAdapter (a client that polls a remote server),
    this adapter IS a server — it binds a port and accepts connections
    from VS Code or other WebSocket clients.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8765, history_limit: int = 100) -> None:
        self._host = host
        self._port = port
        self._history_limit = history_limit
        self._server = None
        self._clients: dict[str, object] = {}  # channel -> websocket
        self._sessions: dict[str, WebSocketSession] = {}
        self._channel_to_session: dict[str, str] = {}
        self._message_queue: asyncio.Queue[NormalizedMessage] = asyncio.Queue()
        self._connected = False

    @property
    def platform(self) -> EventSource:
        return EventSource.VSCODE

    async def connect(self) -> None:
        """Start the WebSocket server."""
        import websockets

        self._server = await websockets.serve(
            self._handle_client, self._host, self._port
        )
        self._connected = True
        logger.info("WebSocket server listening on %s:%d", self._host, self._port)

    async def _handle_client(self, websocket, path=None) -> None:
        """Handle a single client connection lifecycle."""
        request = getattr(websocket, "request", None)
        resolved_path = path or getattr(websocket, "path", None) or getattr(request, "path", None)
        session = self._resolve_session(resolved_path)
        session.websocket = websocket
        self._clients[session.channel] = websocket
        self._channel_to_session[session.channel] = session.session_id

        welcome_type = "session_resumed" if session.history else "connected"
        await websocket.send(
            json.dumps(
                {
                    "type": welcome_type,
                    "protocol_version": 1,
                    "session_id": session.session_id,
                    "channel": session.channel,
                    "content": "Zoomac Agent v0.1.0",
                    "replay_supported": True,
                    "replay_count": len(session.history),
                }
            )
        )
        if session.history:
            await self._replay_session_history(session)
        logger.info("Client connected: %s (%s)", session.channel, session.session_id)

        try:
            async for raw in websocket:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "content": "Invalid JSON",
                    }))
                    continue

                if data.get("type") == "resume":
                    requested_session = str(data.get("session_id", "")).strip()
                    if requested_session and requested_session in self._sessions:
                        await self._switch_session(websocket, session, requested_session)
                        session = self._sessions[requested_session]
                    else:
                        await websocket.send(
                            json.dumps(
                                {
                                    "type": "error",
                                    "content": "Unknown session_id",
                                    "session_id": requested_session or None,
                                }
                            )
                        )
                    continue

                msg = NormalizedMessage(
                    platform=EventSource.VSCODE,
                    channel=data.get("channel", session.channel),
                    author=data.get("author", "user"),
                    content=data.get("content", ""),
                    metadata=data.get("metadata", {}),
                )
                msg.metadata.setdefault("session_id", session.session_id)
                msg.metadata.setdefault("protocol_type", data.get("type", "message"))
                await self._message_queue.put(msg)
        except Exception as e:
            logger.debug("Client %s disconnected: %s", session.channel, e)
        finally:
            self._clients.pop(session.channel, None)
            if session.websocket is websocket:
                session.websocket = None
            logger.info("Client disconnected: %s (%s)", session.channel, session.session_id)

    async def disconnect(self) -> None:
        """Stop the WebSocket server and close all connections."""
        self._connected = False

        # Close all client connections
        import websockets

        for channel, ws in list(self._clients.items()):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()

        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        logger.info("WebSocket server stopped")

    async def listen(self) -> AsyncIterator[NormalizedMessage]:
        """Yield messages from connected clients."""
        while self._connected:
            try:
                msg = await asyncio.wait_for(self._message_queue.get(), timeout=1.0)
                yield msg
            except asyncio.TimeoutError:
                continue

    async def send(self, message: OutboundMessage) -> bool:
        """Send a message to a specific client by channel, or broadcast."""
        session = self._session_for_channel(message.channel)
        if session is not None:
            payload = self._build_outbound_payload(message, session)
            session.history.append(payload)
            ws = self._clients.get(session.channel)
            if ws is not None:
                try:
                    await ws.send(json.dumps(payload))
                    return True
                except Exception as e:
                    logger.error("Send to %s failed: %s", message.channel, e)
                    return False

        # Fallback: broadcast to all connected clients (single-user scenario)
        if self._sessions:
            for candidate in self._sessions.values():
                payload = self._build_outbound_payload(message, candidate)
                candidate.history.append(payload)
                ws = self._clients.get(candidate.channel)
                if ws is None:
                    continue
                try:
                    await ws.send(json.dumps(payload))
                    return True
                except Exception:
                    continue
        return False

    async def health(self) -> AdapterHealth:
        """Report server health status."""
        return AdapterHealth(
            platform=EventSource.VSCODE,
            connected=self._connected and self._server is not None,
            latency_ms=0,
            error=None if self._connected else "Not running",
        )

    @property
    def client_count(self) -> int:
        """Number of connected clients."""
        return len(self._clients)

    def get_port(self) -> int | None:
        """Get the actual port the server is listening on (useful when port=0)."""
        if self._server and self._server.sockets:
            return self._server.sockets[0].getsockname()[1]
        return None

    @property
    def session_count(self) -> int:
        """Number of known sessions, including disconnected resumable sessions."""
        return len(self._sessions)

    def _resolve_session(self, path: str | None) -> WebSocketSession:
        requested_session: str | None = None
        if path:
            parsed = urlparse(path)
            requested_session = parse_qs(parsed.query).get("session_id", [None])[0]

        if requested_session and requested_session in self._sessions:
            return self._sessions[requested_session]

        session_id = requested_session or f"ws-{uuid.uuid4().hex}"
        channel = f"vscode-{uuid.uuid4().hex[:8]}"
        session = WebSocketSession(
            session_id=session_id,
            channel=channel,
            history=deque(maxlen=self._history_limit),
        )
        self._sessions[session_id] = session
        return session

    async def _replay_session_history(self, session: WebSocketSession) -> None:
        if session.websocket is None:
            return
        for payload in session.history:
            replay_payload = dict(payload)
            replay_payload["replay"] = True
            await session.websocket.send(json.dumps(replay_payload))

    async def _switch_session(
        self,
        websocket: object,
        current_session: WebSocketSession,
        requested_session: str,
    ) -> None:
        self._clients.pop(current_session.channel, None)
        if current_session.websocket is websocket:
            current_session.websocket = None

        session = self._sessions[requested_session]
        session.websocket = websocket
        self._clients[session.channel] = websocket
        self._channel_to_session[session.channel] = session.session_id
        await websocket.send(
            json.dumps(
                {
                    "type": "session_resumed",
                    "protocol_version": 1,
                    "session_id": session.session_id,
                    "channel": session.channel,
                    "replay_supported": True,
                    "replay_count": len(session.history),
                }
            )
        )
        await self._replay_session_history(session)

    def _session_for_channel(self, channel: str) -> WebSocketSession | None:
        session_id = self._channel_to_session.get(channel)
        if session_id is not None:
            return self._sessions.get(session_id)
        for session in self._sessions.values():
            if session.channel == channel:
                return session
        return None

    @staticmethod
    def _build_outbound_payload(message: OutboundMessage, session: WebSocketSession) -> dict:
        session.sequence += 1
        event_type = message.metadata.get("type", "response")
        payload = {
            "type": event_type,
            "channel": session.channel,
            "session_id": session.session_id,
            "sequence": session.sequence,
            "content": message.content,
            "reply_to": message.reply_to,
            "metadata": message.metadata,
        }
        return payload

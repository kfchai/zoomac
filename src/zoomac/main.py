"""Zoomac entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Add memgate submodule to path
_memgate_path = str(Path(__file__).resolve().parent.parent.parent / "memgate")
if _memgate_path not in sys.path:
    sys.path.insert(0, _memgate_path)

from zoomac.core.config import ZoomacSettings, load_settings
from zoomac.core.events import MessageEvent
from zoomac.core.loop import CoreLoop

logger = logging.getLogger(__name__)


async def run_server(core: CoreLoop, settings: ZoomacSettings) -> None:
    """Run agent with WebSocket gateway (for Docker/VS Code mode)."""
    from zoomac.gateway.adapter import OutboundMessage
    from zoomac.gateway.gateway import Gateway
    from zoomac.gateway.websocket import WebSocketAdapter

    adapter = WebSocketAdapter(host=settings.ws_host, port=settings.ws_port)
    gateway = Gateway(core.queue)
    gateway.register(adapter)

    # Wire gateway into core for outbound routing
    core.gateway = gateway

    await gateway.start()
    print(f"Zoomac Agent v0.1.0")
    print(f"Model: {settings.model}")
    print(f"WebSocket: ws://{settings.ws_host}:{settings.ws_port}")
    print("Waiting for connections...")

    # Recover stale events
    recovered = core.queue.recover_stale()
    if recovered > 0:
        print(f"  Recovered {recovered} stale event(s).")

    try:
        async for event, response in core.process_queue():
            if isinstance(event, MessageEvent):
                outbound = OutboundMessage(
                    platform=event.source,
                    channel=event.channel,
                    content=response,
                    reply_to=event.id,
                )
                await gateway.send(outbound)
    except asyncio.CancelledError:
        pass
    finally:
        await gateway.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Zoomac Agent")
    parser.add_argument(
        "--mode",
        choices=["cli", "server"],
        default="cli",
        help="Run mode: cli (interactive) or server (WebSocket gateway)",
    )
    args = parser.parse_args()

    settings = load_settings()
    core = CoreLoop(settings)

    try:
        if args.mode == "server":
            asyncio.run(run_server(core, settings))
        else:
            asyncio.run(core.run_cli())
    except KeyboardInterrupt:
        pass
    finally:
        core.shutdown()


if __name__ == "__main__":
    main()

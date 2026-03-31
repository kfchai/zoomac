"""Telegram adapter using aiogram 3.x."""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from zoomac.core.events import EventSource
from zoomac.gateway.adapter import (
    AdapterHealth,
    NormalizedMessage,
    OutboundMessage,
    PlatformAdapter,
)

logger = logging.getLogger(__name__)


class TelegramAdapter(PlatformAdapter):
    """Connects Zoomac to Telegram via the Bot API (aiogram 3.x).

    Requires:
        pip install aiogram
        ZOOMAC_TELEGRAM_TOKEN environment variable

    The adapter uses long-polling (getUpdates) for simplicity.
    Webhook mode can be added later for lower latency.
    """

    def __init__(self, token: str, allowed_users: list[int] | None = None) -> None:
        """
        Args:
            token: Telegram Bot API token.
            allowed_users: Optional list of Telegram user IDs allowed to interact.
                           If None, all users are allowed (single-user agent).
        """
        self._token = token
        self._allowed_users = set(allowed_users) if allowed_users else None
        self._bot = None  # aiogram.Bot
        self._dp = None   # aiogram.Dispatcher
        self._message_queue: asyncio.Queue[NormalizedMessage] = asyncio.Queue()
        self._connected = False
        self._polling_task: asyncio.Task | None = None

    @property
    def platform(self) -> EventSource:
        return EventSource.TELEGRAM

    async def connect(self) -> None:
        try:
            from aiogram import Bot, Dispatcher
            from aiogram.types import Message as TgMessage
        except ImportError:
            raise RuntimeError("aiogram not installed. Run: pip install aiogram")

        self._bot = Bot(token=self._token)
        self._dp = Dispatcher()

        # Register message handler
        @self._dp.message()
        async def _on_message(message: TgMessage) -> None:
            if message.text is None:
                return

            # Filter by allowed users if configured
            if self._allowed_users and message.from_user:
                if message.from_user.id not in self._allowed_users:
                    return

            author = ""
            if message.from_user:
                author = message.from_user.username or str(message.from_user.id)

            normalized = NormalizedMessage(
                platform=EventSource.TELEGRAM,
                channel=str(message.chat.id),
                author=author,
                content=message.text,
                reply_to=str(message.reply_to_message.message_id) if message.reply_to_message else None,
                metadata={
                    "message_id": message.message_id,
                    "chat_type": message.chat.type,
                    "user_id": message.from_user.id if message.from_user else None,
                },
            )
            await self._message_queue.put(normalized)

        self._connected = True
        # Start polling in the background
        self._polling_task = asyncio.create_task(self._poll())
        logger.info("Telegram adapter connected")

    async def _poll(self) -> None:
        """Run aiogram polling."""
        try:
            await self._dp.start_polling(self._bot)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Telegram polling error: %s", e)
            self._connected = False

    async def disconnect(self) -> None:
        self._connected = False
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None

        if self._dp:
            await self._dp.stop_polling()

        if self._bot:
            await self._bot.session.close()
            self._bot = None

        logger.info("Telegram adapter disconnected")

    async def listen(self) -> AsyncIterator[NormalizedMessage]:
        """Yield messages from the internal queue (fed by aiogram handler)."""
        while self._connected:
            try:
                msg = await asyncio.wait_for(self._message_queue.get(), timeout=1.0)
                yield msg
            except asyncio.TimeoutError:
                continue

    async def send(self, message: OutboundMessage) -> bool:
        if self._bot is None:
            return False
        try:
            kwargs = {}
            if message.reply_to:
                kwargs["reply_to_message_id"] = int(message.reply_to)

            await self._bot.send_message(
                chat_id=int(message.channel),
                text=message.content,
                **kwargs,
            )
            return True
        except Exception as e:
            logger.error("Telegram send failed: %s", e)
            return False

    async def health(self) -> AdapterHealth:
        if not self._connected or self._bot is None:
            return AdapterHealth(
                platform=EventSource.TELEGRAM,
                connected=False,
                error="Not connected",
            )
        try:
            me = await self._bot.get_me()
            return AdapterHealth(
                platform=EventSource.TELEGRAM,
                connected=True,
            )
        except Exception as e:
            return AdapterHealth(
                platform=EventSource.TELEGRAM,
                connected=False,
                error=str(e),
            )

# Gateway & Platform Adapters

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                     GATEWAY                          │
│                                                      │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌───────┐  │
│  │WhatsApp │  │Telegram │  │Discord  │  │ ...   │  │
│  │Adapter  │  │Adapter  │  │Adapter  │  │Future │  │
│  └────┬────┘  └────┬────┘  └────┬────┘  └───┬───┘  │
│       │            │            │            │       │
│       ▼            ▼            ▼            ▼       │
│  ┌─────────────────────────────────────────────┐    │
│  │          Adapter Interface (ABC)             │    │
│  │                                              │    │
│  │  connect()      → establish connection       │    │
│  │  disconnect()   → graceful shutdown          │    │
│  │  listen()       → async generator of events  │    │
│  │  send(msg)      → deliver outbound message   │    │
│  │  health()       → connection status          │    │
│  └──────────────────────┬──────────────────────┘    │
│                         ▼                            │
│                   EVENT QUEUE                        │
└─────────────────────────────────────────────────────┘
```

## Normalized Message Format

```json
{
  "id": "uuid",
  "platform": "whatsapp|telegram|discord",
  "channel": "chat_id / channel_id",
  "author": "sender identity",
  "content": "message text",
  "attachments": [],
  "timestamp": "ISO-8601",
  "reply_to": "original_msg_id | null",
  "metadata": { "platform-specific fields" }
}
```

## Design Decisions

- **Abstract adapter interface** — each platform implements `connect()`, `disconnect()`, `listen()`, `send()`, `health()`. Adding a new platform = one new adapter class, no core changes.
- **Normalized message format** — all platforms flattened into the same structure before hitting the event queue. The core loop never knows or cares which platform a message came from.
- **Adapters run as async tasks** — each adapter's `listen()` is an async generator that yields normalized messages. The gateway multiplexes them into the single event queue.
- **Platform-specific metadata preserved** — Discord roles, Telegram reply markup, WhatsApp read receipts live in `metadata` for skills that need them, but the core loop ignores them.
- **Reconnection handled per-adapter** — each adapter manages its own retry/backoff logic. The gateway monitors `health()` and logs/alerts on failures.

## Initial Platform Implementations

- **WhatsApp** — via Baileys (open-source WhatsApp Web API library)
- **Telegram** — via aiogram (async Telegram Bot API)
- **Discord** — via discord.py (async Discord API)

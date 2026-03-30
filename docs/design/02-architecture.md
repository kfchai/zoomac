# Architecture

## High-Level Diagram

```
┌──────────────────────────────────────────────────────────┐
│                      ZOOMAC AGENT                         │
│                      (Pure Python)                        │
│                                                          │
│  ┌──────────┐   ┌──────────┐   ┌───────────────────┐    │
│  │ Gateway   │──▶│  Core    │──▶│  Pydantic AI      │    │
│  │ (inbound) │   │  Loop    │   │  (LLM + tools)    │    │
│  └──────────┘   └────┬─────┘   └───────────────────┘    │
│       ▲               │                                   │
│       │          ┌────▼─────┐   ┌───────────────────┐    │
│  Platforms       │ Autonomy │   │  MemGate          │    │
│  (WA/TG/DC)     │ Manager  │   │  (direct import)   │    │
│                  └────┬─────┘   └───────────────────┘    │
│                       │                                   │
│                  ┌────▼─────┐   ┌───────────────────┐    │
│                  │ Agent    │   │  Sandbox           │    │
│                  │ Manager  │   │  (Docker)           │    │
│                  └────┬─────┘   └───────────────────┘    │
│                       │                                   │
│                  ┌────▼─────┐                             │
│                  │Scheduler │                             │
│                  └──────────┘                             │
└──────────────────────────────────────────────────────────┘
```

## Six Core Components

### 1. Core Loop

The main event loop. Receives events (messages, scheduled triggers, user input), orchestrates the agent cycle, dispatches to subsystems. You own this — Pydantic AI is called as a library, not as the host.

### 2. Gateway

Lightweight message router. Platform adapters (WhatsApp, Telegram, Discord) normalize incoming messages into a common format and push them into the Core Loop's event queue. Outbound responses go back through the same adapters.

### 3. Pydantic AI (Brain)

LLM orchestration layer. Handles model calls, tool execution, structured output validation. Model-agnostic — switch providers via config. Tools are typed Python functions with Pydantic models.

### 4. MemGate (Memory)

Long-term memory via direct Python import. Every conversation turn is evaluated by the LLM for memory-worthiness, then ingested via `ingest_structured()`. Before each LLM call, relevant memories are retrieved and injected into context.

### 5. Autonomy Manager

Classifies actions by risk level, enforces confirm/act-freely policy. Configurable per-skill and per-platform overrides via YAML.

### 6. Scheduler

Cron-like heartbeat for background tasks, inbox polling, periodic memory consolidation. Supports time-based, event-based, and condition-based triggers.

## Deployment

```
Local Machine (interactive):
├── Full Zoomac agent
├── CLI/terminal interface
├── Connects to shared MemGate DB on server
└── Docker for sandboxed execution

Server (background):
├── Headless Zoomac agent
├── Gateway (always-on platform connections)
├── MemGate DB (centralized)
├── Scheduler (cron jobs, polling)
└── Docker for sandboxed execution
```

## Dependencies

```
pydantic-ai          # LLM orchestration, tool definitions
pydantic              # Data models, validation, config
memgate               # Long-term memory (direct import)
asyncio + uvloop      # Event loop
aiogram               # Telegram adapter
whatsapp-api-client   # WhatsApp adapter
discord.py            # Discord adapter
docker (SDK)          # Sandbox management
apscheduler           # Cron/scheduler
pydantic-logfire      # Observability (optional)
```

# Sub-Agent System

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    MAIN AGENT                            │
│                                                          │
│  ┌────────────┐  ┌──────────────┐  ┌────────────────┐   │
│  │ Core Loop   │  │ Agent Manager │  │ MemGate (main) │   │
│  └──────┬─────┘  └──────┬───────┘  └───────┬────────┘   │
│         │               │                   │            │
└─────────┼───────────────┼───────────────────┼────────────┘
          │               │                   │
    ┌─────▼───────────────▼───────────────────▼──────┐
    │              AGENT MANAGER                      │
    │                                                 │
    │  ├── Spawn sub-agents with task + constraints   │
    │  ├── Monitor progress (heartbeat + status)      │
    │  ├── Terminate on timeout / completion / cancel  │
    │  ├── Merge sub-agent memory → main MemGate      │
    │  └── Route inter-agent messages                 │
    └────────┬──────────┬──────────┬─────────────────┘
             │          │          │
       ┌─────▼──┐ ┌────▼───┐ ┌───▼────┐
       │Agent A │ │Agent B │ │Agent C │
       │Pi SDK  │ │Pi SDK  │ │Pi SDK  │
       │MemGate │ │MemGate │ │MemGate │
       │(local) │ │(local) │ │(local) │
       │Sandbox │ │Sandbox │ │Sandbox │
       └────────┘ └────────┘ └────────┘
```

## Sub-Agent Anatomy

Each sub-agent is an isolated process with:

- Its own **Pydantic AI agent** (separate LLM context)
- Its own **MemGate instance** (standalone `.memgate.db`) — learns during the task without polluting main memory
- Its own **sandbox** (Docker container)
- A **task brief** from the main agent (goal, constraints, deadline, tools allowed)

## Lifecycle States

```
SPAWNED → RUNNING → DONE    → memory merge → cleanup
                  → FAILED  → report to main → cleanup
           ↕
         PAUSED   (main agent can pause/resume)
```

## Memory Merge on Completion

When a sub-agent finishes, the Agent Manager:

1. Asks the sub-agent to produce a **final summary** (structured JSON, same `ingest_structured` format)
2. Optionally bulk-exports the sub-agent's MemGate memories above a quality threshold
3. Ingests the summary + selected memories into **main MemGate** via `ingest_structured`
4. Sub-agent's `.memgate.db` is archived or deleted

The main agent gets the *distilled knowledge* without the noise of every intermediate step.

## Inter-Agent Communication Protocol

### Message Bus

```
┌─────────────────────────────────────────────┐
│           MESSAGE BUS (async channels)       │
│                                              │
│  Channel per sub-agent: main ↔ agent_{id}   │
└─────────────────────────────────────────────┘
```

### Message Format

```json
{
  "id": "uuid",
  "from": "main | agent_{id}",
  "to": "main | agent_{id}",
  "type": "directive|status|query|response|result",
  "payload": {},
  "timestamp": "ISO-8601"
}
```

### Message Types

| Type | Direction | Purpose |
|------|-----------|---------|
| `directive` | main → sub | Instructions, corrections, new constraints |
| `status` | sub → main | Progress updates, heartbeat |
| `query` | sub → main | Sub-agent needs info from main memory or another sub-agent |
| `response` | main → sub | Answer to a query (may include MemGate search results) |
| `result` | sub → main | Task complete — final output + memory summary |

### Key Behaviors

- **Sub-agents can query main memory** — sends a `query` message. The main agent runs `MemGate.search()` on its main DB and returns a `response`. Sub-agents never directly access main MemGate.
- **Main agent can steer mid-task** — `directive` messages adjust goals, add constraints, or redirect.
- **Sub-agents cannot talk to each other directly** — all communication routes through the main agent (single source of truth).
- **Heartbeat timeout** — if a sub-agent misses N heartbeats, the Agent Manager kills it and reports failure.

### Example Flow

```
Main: "Research current Bitcoin price trends for the last 7 days"
  → spawn agent_research with task brief + web_search tool

agent_research [status]: "Starting web search..."
agent_research [query]:  "Do I have any prior crypto preferences?"
Main [response]:         {memories from MemGate about crypto}
agent_research [status]: "Found 5 sources, analyzing..."
agent_research [result]: {
  summary: "BTC rose 12% this week...",
  memory_extract: {
    "content": "BTC 7-day trend: +12%, driven by...",
    "entities": [{"name":"Bitcoin","attribute":"7d_change","value":"+12%"}],
    "temporal": "2026-03-30"
  }
}
Main: merge memory → done → cleanup
```

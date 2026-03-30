# Core Loop & Event System

## Event Queue

```
┌─────────────────────────────────────────────┐
│                 EVENT QUEUE                   │
│  (async, priority-ordered)                   │
├─────────────────────────────────────────────┤
│  Sources:                                    │
│  ├── Gateway (inbound messages)              │
│  ├── Scheduler (cron triggers)               │
│  ├── CLI/Terminal (local interactive)        │
│  ├── Internal (skill-to-skill, callbacks)    │
│  └── System (health, errors, alerts)         │
└──────────────┬──────────────────────────────┘
               ▼
               Core Loop
```

## Processing Cycle

```
1. Dequeue event
2. Load session context
3. Retrieve memories (MemGate.context())
4. Assemble prompt + call Pydantic AI → LLM
5. LLM returns:
   ├── response (to user/platform)
   ├── actions (tool calls)
   └── memory_extract (structured JSON):
       {
         "worth_remembering": true/false,
         "content": "summary of what happened...",
         "entities": [
           {"name":"X", "attribute":"Y",
            "value":"Z", "previous":"old_value"}
         ],
         "relationships": [
           {"a":"X", "relation":"R", "b":"Y"}
         ],
         "temporal": "2026-03-29",
         "update": true/false
       }
6. Autonomy Manager → classify risk & execute
7. If worth_remembering:
   └── MemGate.ingest_structured(payload)
8. Route response back via Gateway
9. Loop
```

## LLM-Driven Memory Extraction

The LLM itself decides what's worth remembering and produces a structured JSON summary. MemGate then runs it through its novelty/quality gate as a second filter — two layers:

1. **LLM judgment** — "is this worth remembering?" + structured extraction
2. **MemGate gate** — novelty detection, deduplication, quality scoring

The agent doesn't blindly store everything. MemGate's gate can still reject redundant memories even if the LLM flags them. The `previous` field in entities enables automatic fact supersession (e.g., "Alice's role changed from engineer to manager").

## Pydantic AI Integration

```python
from pydantic_ai import Agent
from pydantic import BaseModel
from memgate import MemGate

class MemoryExtract(BaseModel):
    worth_remembering: bool
    content: str | None = None
    entities: list[dict] | None = None
    relationships: list[dict] | None = None
    temporal: str | None = None
    update: bool = False

class EvolutionSignal(BaseModel):
    new_skill: dict | None = None
    correction: dict | None = None
    behavior_note: str | None = None
    config_suggestion: dict | None = None

class AgentResponse(BaseModel):
    message: str
    memory: MemoryExtract
    sources: list[str]
    confidence: float
    needs_verification: bool
    evolution: EvolutionSignal

# Define the agent
agent = Agent(
    model="anthropic:claude-sonnet-4-20250514",
    result_type=AgentResponse,
    system_prompt="You are Zoomac...",
)

@agent.tool
async def search_memory(ctx, query: str) -> str:
    """Search long-term memory for relevant context."""
    mg: MemGate = ctx.deps.memgate
    return mg.context(query)["context"]

@agent.tool
async def check_facts(ctx, entity: str) -> str:
    """Retrieve known facts about an entity."""
    mg: MemGate = ctx.deps.memgate
    return str(mg.facts(entity=entity))

@agent.tool
async def spawn_sub_agent(ctx, task: str, tools: list[str]) -> str:
    """Spawn a sub-agent for a specific task."""
    manager = ctx.deps.agent_manager
    return await manager.spawn(task=task, tools=tools)

# Core loop usage
result = await agent.run(
    user_prompt=event.content,
    deps=ZoomacDeps(memgate=mg, agent_manager=am, ...),
)

if result.data.memory.worth_remembering:
    mg.ingest_structured({
        "content": result.data.memory.content,
        "entities": result.data.memory.entities,
        "relationships": result.data.memory.relationships,
        "temporal": result.data.memory.temporal,
        "update": result.data.memory.update,
    })
```

## Session Management

- **Session-per-conversation** — each platform conversation (WhatsApp chat, Discord channel) gets its own session
- **Short-term history** — recent turns in session context
- **Long-term memory** — shared across all sessions via MemGate
- **Event queue persisted to disk** (SQLite) — unprocessed events survive restarts

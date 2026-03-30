# Zoomac — AI Agent Project

## What This Is

Zoomac is a **hybrid AI agent** (coding + personal assistant) that connects to messaging platforms (WhatsApp, Telegram, Discord), runs autonomously, and learns/memorizes skills over time. Single user, personal tool.

## Tech Stack

- **Language:** Python 3.11+
- **LLM Orchestration:** Pydantic AI (model-agnostic, typed outputs)
- **Memory:** MemGate (git submodule at `./memgate/`, direct Python import)
- **Async Runtime:** asyncio + uvloop
- **Sandbox:** Docker containers (planned)
- **Scheduler:** APScheduler (planned)

## Project Structure

```
zoomac/
├── src/zoomac/
│   ├── main.py                 # Entry point
│   ├── core/
│   │   ├── config.py           # ZoomacSettings (Pydantic BaseSettings, ZOOMAC_ env prefix)
│   │   ├── events.py           # MessageEvent, ScheduleEvent, SystemEvent
│   │   ├── loop.py             # CoreLoop — async event loop with queue processing
│   │   └── queue.py            # EventQueue — SQLite-backed, priority-ordered, crash-safe
│   ├── brain/
│   │   ├── agent.py            # Pydantic AI agent + tools (search_memory, check_facts, memory_status)
│   │   ├── memory_extract.py   # AgentResponse, MemoryExtract, EvolutionSignal schemas
│   │   └── prompts.py          # System prompt (memory-first, source attribution)
│   ├── memory/
│   │   └── integration.py      # MemoryManager wrapping MemGate
│   ├── autonomy/
│   │   ├── classifier.py       # RiskClassifier (12 action types, cascade: skill > platform > default)
│   │   └── policy.py           # AutonomyManager (YAML config, SQLite audit log)
│   ├── sandbox/
│   │   ├── profiles.py         # 4 permission profiles (minimal/standard/project/full)
│   │   └── docker.py           # SandboxManager (container pool, execute, cleanup)
│   ├── gateway/                # Placeholder — Phase 5
│   ├── agents/                 # Placeholder — Phase 7
│   ├── sandbox/                # Placeholder — Phase 4
│   ├── scheduler/              # Placeholder — Phase 9
│   └── skills/                 # Placeholder — Phase 8
├── memgate/                    # Git submodule (https://github.com/kfchai/memgate.git)
├── config/
│   └── autonomy.yaml           # Risk/permission defaults
├── docs/design/                # 15 design documents (architecture, decisions, implementation plan)
├── Dockerfile                  # Sandbox base image (Python 3.11 + Node.js + tools)
├── tests/                      # 76 passing tests
├── pyproject.toml
└── .gitignore
```

## How to Run

```bash
# Install
pip install -e ".[dev]"

# Run (needs ANTHROPIC_API_KEY or ZOOMAC_MODEL set)
ZOOMAC_MODEL=anthropic:claude-sonnet-4-20250514 python -m zoomac.main

# Run tests (PYTHONPATH needed for memgate submodule)
PYTHONPATH=memgate:$PYTHONPATH python -m pytest tests/ -v
```

## Key Environment Variables

- `ZOOMAC_MODEL` — Pydantic AI model identifier (default: `anthropic:claude-sonnet-4-20250514`)
- `ZOOMAC_PROJECT_DIR` — Project root (default: cwd)
- `ZOOMAC_MEMORY_MAX_TOKENS` — Max tokens for memory context injection (default: 2000)
- `ZOOMAC_MEMORY_TOP_K` — Memories to retrieve per query (default: 10)
- `ANTHROPIC_API_KEY` — Required for Anthropic models

## Architecture Overview

```
Gateway (inbound) → Core Loop → Pydantic AI (LLM + tools)
                        │
                   Autonomy Manager ←→ MemGate (long-term memory)
                        │
                   Agent Manager ←→ Sandbox (Docker)
                        │
                    Scheduler
```

**Core loop per event:**
1. Dequeue event (priority-ordered)
2. Retrieve memories via MemGate
3. Call Pydantic AI agent (LLM)
4. LLM returns `AgentResponse` with structured `MemoryExtract`
5. Autonomy Manager classifies actions (low-risk → execute, high-risk → confirm)
6. If `worth_remembering=True` → `MemGate.ingest_structured(payload)`
7. Route response back

**LLM decides what to remember.** Each response includes:
- `memory.worth_remembering` — should this be stored?
- `memory.entities` — structured facts `[{name, attribute, value, previous}]`
- `memory.relationships` — `[{a, relation, b}]`
- `memory.temporal` — date reference
- `memory.update` — supersedes existing facts

## Implementation Progress

| Phase | Status | Tests | Description |
|-------|--------|-------|-------------|
| 0: Scaffolding | ✅ Done | 11 | Project skeleton, deps, config, schemas |
| 1: Memory Integration | ✅ Done | 17 | MemGate wrapper, search/facts tools, ingest flow |
| 2: Core Loop & Events | ✅ Done | 14 | SQLite event queue, priority ordering, crash recovery, dead letter |
| 3: Autonomy Manager | ✅ Done | 14 | Risk classifier, YAML config, audit log |
| 4: Sandbox (Docker) | ✅ Done | 20 | Container pool, 4 permission profiles, Dockerfile, resource limits |
| 5: Gateway — Telegram | ⏳ Next | — | Abstract adapter, Telegram via aiogram |
| 6: Gateway — WA + Discord | Planned | — | WhatsApp + Discord adapters |
| 7: Sub-Agents | Planned | — | Isolated memory, lifecycle, message bus |
| 8: Skill System | Planned | — | Registry, learning, cross-referencing |
| 9: Scheduler | Planned | — | APScheduler, cron/event/condition triggers |
| 10: Self-Evolution | Planned | — | Skill learning, behavior adaptation, guarded self-mod |
| 11: Anti-Hallucination | Planned | — | 6-layer defense in depth |
| 12: Resilience & Polish | Planned | — | Circuit breakers, fallback chains, health monitoring |

**Total: 76 tests passing**

## Branch

Development branch: `claude/ai-agent-planning-1PMGd`

## Design Documents

Full design in `docs/design/`:
- `01-overview.md` — Summary, stack, principles
- `02-architecture.md` — High-level diagram, 6 components
- `03-core-loop.md` — Event system, LLM-driven memory, Pydantic AI code examples
- `04-gateway.md` — Adapter interface, normalized messages
- `05-sub-agents.md` — Isolated memory, lifecycle, message bus protocol
- `06-skills.md` — Registry, MemGate storage, cross-referencing
- `07-autonomy.md` — Risk classification, confirmation routing
- `08-sandbox.md` — Docker profiles, resource limits
- `09-scheduler.md` — Cron, event/condition triggers
- `10-anti-hallucination.md` — 6-layer defense in depth
- `11-self-evolution.md` — Skill learning, behavior adaptation, guarded self-mod
- `12-resilience.md` — Circuit breakers, recovery strategies
- `13-project-structure.md` — Full directory layout
- `14-decision-log.md` — 19 documented decisions with rationale
- `15-implementation-plan.md` — 12 phases with detailed task checklists

## Testing Notes

- Tests use `FakeEmbedder` (in `tests/conftest.py`) to avoid downloading the sentence-transformers model
- Agent tests use Pydantic AI's `TestModel` (no API key needed)
- Run with: `PYTHONPATH=memgate:$PYTHONPATH python -m pytest tests/ -v`

## Key Design Decisions

1. **Python** over TypeScript — MemGate is Python, ML ecosystem is Python
2. **Pydantic AI** over Pi/LangGraph — lightweight SDK, typed outputs, model-agnostic
3. **Standalone agent** (Approach B) — you own the main loop, max freedom
4. **LLM-driven memory** — LLM decides what to remember, MemGate gates as 2nd filter
5. **Sub-agents with isolated memory** — merge on completion, prevent pollution
6. **Skills stored in MemGate** — learned, cross-referenced, composable
7. **Autonomy cascade** — skill override > platform override > action type default

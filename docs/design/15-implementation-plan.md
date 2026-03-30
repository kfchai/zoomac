# Implementation Plan

## Phased Approach

Build incrementally — each phase produces a working system that you can use daily.

---

## Phase 0: Scaffolding (Day 1)

**Goal:** Project skeleton, dependencies, basic "hello world" agent.

- [ ] Initialize Python project with `pyproject.toml` (uv or poetry)
- [ ] Create directory structure per `13-project-structure.md`
- [ ] Install core deps: `pydantic-ai`, `pydantic`, `memgate`, `asyncio`
- [ ] Create `src/zoomac/main.py` entry point
- [ ] Create `src/zoomac/core/config.py` with Pydantic BaseSettings
- [ ] Wire up a minimal Pydantic AI agent that responds to CLI input
- [ ] Verify: run agent, send a message, get a response

**Deliverable:** `python -m zoomac` starts an interactive CLI agent.

---

## Phase 1: Memory Integration (Days 2-3)

**Goal:** Agent remembers across sessions via MemGate.

- [ ] Create `src/zoomac/memory/integration.py` — MemGate wrapper
- [ ] Add `search_memory` and `check_facts` tools to the agent
- [ ] Create `src/zoomac/brain/memory_extract.py` — `MemoryExtract` schema
- [ ] Update `AgentResponse` to include `memory`, `sources`, `confidence`
- [ ] Wire core loop: retrieve memories before LLM call, ingest after
- [ ] Add `ingest_structured` call when `worth_remembering=True`
- [ ] Test: have a conversation, restart, verify agent recalls prior context

**Deliverable:** Agent with persistent long-term memory.

---

## Phase 2: Core Loop & Events (Days 4-5)

**Goal:** Proper async event loop replacing the simple CLI loop.

- [ ] Create `src/zoomac/core/events.py` — Pydantic event models (MessageEvent, ScheduleEvent, SystemEvent, etc.)
- [ ] Create `src/zoomac/core/loop.py` — async event queue with priority ordering
- [ ] Persist event queue to SQLite for crash recovery
- [ ] Wire CLI input as an event source
- [ ] Add dead letter queue for failed events
- [ ] Test: kill process mid-event, restart, verify event replays

**Deliverable:** Resilient async event-driven core loop.

---

## Phase 3: Autonomy Manager (Day 6)

**Goal:** Risk-classified action execution.

- [ ] Create `src/zoomac/autonomy/classifier.py` — rule-based risk classification
- [ ] Create `src/zoomac/autonomy/policy.py` — YAML config loader
- [ ] Create `config/autonomy.yaml` with defaults
- [ ] Wire into core loop: every tool call passes through classifier
- [ ] Implement async confirmation flow (CLI prompt for now)
- [ ] Add audit log (actions + decisions stored in SQLite)
- [ ] Test: trigger a high-risk action, verify confirmation prompt

**Deliverable:** Agent asks before dangerous actions.

---

## Phase 4: Sandbox (Days 7-8)

**Goal:** Tool execution in Docker containers.

- [ ] Create `Dockerfile` for `zoomac-sandbox` base image
- [ ] Create `src/zoomac/sandbox/profiles.py` — permission profiles (minimal/standard/project/full)
- [ ] Create `src/zoomac/sandbox/docker.py` — container pool (warm pool, spin-up, teardown)
- [ ] Add resource limits (CPU, memory, timeout, disk)
- [ ] Wire `bash` tool to execute inside sandbox
- [ ] Test: run a command in sandbox, verify isolation (no host filesystem access in minimal profile)

**Deliverable:** Sandboxed code execution.

---

## Phase 5: Gateway — First Platform (Days 9-11)

**Goal:** Telegram adapter as the first messaging platform.

- [ ] Create `src/zoomac/gateway/adapter.py` — abstract adapter ABC
- [ ] Create `src/zoomac/gateway/gateway.py` — multiplexer into event queue
- [ ] Create `src/zoomac/gateway/telegram.py` — aiogram-based adapter
- [ ] Implement normalized message format
- [ ] Wire gateway into core loop as event source
- [ ] Implement outbound: route responses back through adapter
- [ ] Handle reconnection with backoff
- [ ] Test: send message on Telegram, get response, verify memory stored

**Deliverable:** Agent responds on Telegram with full memory.

---

## Phase 6: More Platforms (Days 12-14)

**Goal:** WhatsApp and Discord adapters.

- [ ] Create `src/zoomac/gateway/whatsapp.py` — Baileys-based adapter
- [ ] Create `src/zoomac/gateway/discord.py` — discord.py adapter
- [ ] Wire both into gateway multiplexer
- [ ] Verify cross-platform memory: mention something on Discord, recall it on Telegram
- [ ] Test reconnection and health monitoring per adapter

**Deliverable:** Agent live on 3 platforms with shared memory.

---

## Phase 7: Sub-Agents (Days 15-18)

**Goal:** Spawn isolated sub-agents for tasks.

- [ ] Create `src/zoomac/agents/lifecycle.py` — state machine (SPAWNED/RUNNING/PAUSED/DONE/FAILED)
- [ ] Create `src/zoomac/agents/bus.py` — async message bus (directive/status/query/response/result)
- [ ] Create `src/zoomac/agents/manager.py` — spawn/kill/monitor/merge
- [ ] Each sub-agent gets: own Pydantic AI agent, own MemGate instance, own sandbox
- [ ] Implement memory merge on completion (summary + quality-filtered bulk export)
- [ ] Implement heartbeat timeout
- [ ] Add `spawn_sub_agent` tool to main agent
- [ ] Test: ask agent to research something, verify sub-agent spawns, runs, merges memory back

**Deliverable:** Working sub-agent system with isolated memory.

---

## Phase 8: Skill System (Days 19-21)

**Goal:** Static and learned skills, cross-referencing.

- [ ] Create `src/zoomac/skills/loader.py` — load static skills from `/skills/` directory
- [ ] Create `src/zoomac/skills/registry.py` — in-memory index + MemGate skill queries
- [ ] Define skill file format (YAML or Markdown)
- [ ] Wire skill injection into prompt assembly (available skills listed for LLM)
- [ ] Implement skill learning: `EvolutionSignal.new_skill` → MemGate ingest
- [ ] Implement usage tracking (increment `usage_count` on use)
- [ ] Implement cross-referencing via MemGate relationships
- [ ] Write 3-5 initial static skills (web search, file summary, git operations, etc.)
- [ ] Test: complete a novel task, verify skill extracted, verify recall on similar task

**Deliverable:** Self-learning skill system.

---

## Phase 9: Scheduler (Days 22-23)

**Goal:** Background tasks, cron, condition triggers.

- [ ] Create `src/zoomac/scheduler/cron.py` — APScheduler integration
- [ ] Create `src/zoomac/scheduler/triggers.py` — event and condition trigger evaluation
- [ ] Create `config/schedule.yaml` with initial jobs (inbox poll, memory consolidation)
- [ ] Wire scheduler output into event queue
- [ ] Implement condition triggers (MemGate fact queries with thresholds)
- [ ] Test: set a cron job, verify it fires and produces correct event

**Deliverable:** Automated background task execution.

---

## Phase 10: Self-Evolution (Days 24-26)

**Goal:** Agent improves itself over time.

- [ ] Wire `EvolutionSignal` processing in core loop
- [ ] Implement correction ingestion (user corrections → MemGate error patterns)
- [ ] Implement behavioral directive injection into system prompt
- [ ] Implement self-review scheduled job (weekly sub-agent analysis)
- [ ] Implement guarded code self-modification:
  - [ ] Agent writes to `/skills/` and `config/` only
  - [ ] Changes go to staging git branch
  - [ ] Require user confirmation
- [ ] Test: correct the agent 3 times on same topic, verify it adapts

**Deliverable:** Agent that learns from mistakes and evolves.

---

## Phase 11: Anti-Hallucination Hardening (Days 27-28)

**Goal:** All 6 layers active.

- [ ] Verify Layer 1: MemGate retrieval before every factual response
- [ ] Verify Layer 2: Pydantic validation catching malformed outputs
- [ ] Verify Layer 3: System prompt enforces tool-use for facts
- [ ] Implement Layer 4: `verify_claim` tool + confidence threshold routing
- [ ] Verify Layer 5: Correction learning working (from Phase 10)
- [ ] Implement Layer 6: Dual sub-agent verification for critical tasks
- [ ] Add integration tests: plant known facts, ask about them, verify grounding

**Deliverable:** Battle-tested anti-hallucination system.

---

## Phase 12: Resilience & Polish (Days 29-30)

**Goal:** Production-ready reliability.

- [ ] Implement circuit breakers per component (LLM, gateway, sandbox, MemGate)
- [ ] Implement LLM provider fallback chain
- [ ] Implement graceful degradation (memory-offline mode)
- [ ] Add health monitoring endpoint
- [ ] Create `docker-compose.yaml` for server deployment
- [ ] End-to-end stress test: 100+ messages across platforms, kill/restart mid-stream
- [ ] Write CLAUDE.md for the project

**Deliverable:** Production-ready Zoomac agent.

---

## Summary

| Phase | Component | Days | Cumulative |
|-------|-----------|------|------------|
| 0 | Scaffolding | 1 | 1 |
| 1 | Memory (MemGate) | 2 | 3 |
| 2 | Core Loop & Events | 2 | 5 |
| 3 | Autonomy Manager | 1 | 6 |
| 4 | Sandbox (Docker) | 2 | 8 |
| 5 | Gateway — Telegram | 3 | 11 |
| 6 | Gateway — WhatsApp + Discord | 3 | 14 |
| 7 | Sub-Agents | 4 | 18 |
| 8 | Skill System | 3 | 21 |
| 9 | Scheduler | 2 | 23 |
| 10 | Self-Evolution | 3 | 26 |
| 11 | Anti-Hallucination | 2 | 28 |
| 12 | Resilience & Polish | 2 | 30 |

Each phase builds on the previous one. After Phase 1, you have a usable agent with memory. After Phase 5, you're on Telegram. After Phase 7, you have sub-agents. By Phase 12, you have a fully autonomous, self-evolving, resilient hybrid agent.

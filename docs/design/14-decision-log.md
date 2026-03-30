# Decision Log

| # | Decision | Alternatives Considered | Rationale |
|---|----------|------------------------|-----------|
| 1 | **Hybrid agent** (coding + personal assistant) | Coding-only, assistant-only | User needs one agent for everything |
| 2 | **Single user, personal tool** | Multi-user, broad audience | Tailored to your workflows, no auth/multi-tenancy overhead |
| 3 | **WhatsApp + Telegram + Discord** (extensible) | All 12+ platforms like OpenClaw | Start focused, adapter pattern allows adding more later |
| 4 | **All task types** with learnable skills | Fixed task categories | Skills are memorized and cross-referenced via MemGate |
| 5 | **Model-agnostic** | Single provider | Freedom to switch, use best model per task |
| 6 | **Local + server, sandboxed** | Local-only, cloud-native | Interactive locally, background on server, isolated execution |
| 7 | **Act freely low-risk, confirm high-risk** | Always confirm, fully autonomous | Practical balance; configurable per-skill |
| 8 | **Heavy daily use** scale | Light use | Hundreds of interactions/day, months of memory growth |
| 9 | **Pragmatic privacy** (cloud LLM ok, state stays local) | Strict local-only, fully relaxed | Best models are cloud, but your data stays yours |
| 10 | **Resilient** (auto-recover, queue missed) | Best-effort, high-availability | Reliable without over-engineering redundancy |
| 11 | **Semi-automated maintenance** | Manual, set-and-forget | Self-update with approval, health monitoring |
| 12 | **Approach B: Standalone agent, LLM lib as dep** | A: Pi extension, C: OpenClaw fork | Maximum freedom, you own the main loop |
| 13 | **LLM-driven memory extraction** | Ingest everything, rule-based | LLM decides what to remember + MemGate gates as 2nd filter |
| 14 | **Sub-agents with isolated MemGate** | Shared memory, no sub-agents | Isolated memory prevents pollution; merge on completion |
| 15 | **Hub-and-spoke comms** (all through main) | Direct sub-agent-to-sub-agent | Main agent stays single source of truth |
| 16 | **Python** over TypeScript | TypeScript (Pi native) | MemGate is Python, ML ecosystem is Python, self-evolution easier |
| 17 | **Pydantic AI** as LLM backbone | Pi (TS), LangGraph, Aider | Lightweight SDK, typed outputs, model-agnostic, Python-native |
| 18 | **Anti-hallucination: defense in depth** | Single strategy | 6 layers from retrieval grounding to sub-agent consensus |
| 19 | **Self-evolution with guardrails** | No self-mod, unrestricted | Agent can modify skills/config, core loop read-only, changes staged + tested |

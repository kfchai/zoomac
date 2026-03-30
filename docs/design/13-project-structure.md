# Project Structure

```
zoomac/
├── src/
│   └── zoomac/
│       ├── __init__.py
│       ├── main.py                # Entry point
│       ├── core/
│       │   ├── loop.py            # Async event loop
│       │   ├── events.py          # Event types (Pydantic models)
│       │   └── config.py          # Settings (Pydantic BaseSettings)
│       ├── brain/
│       │   ├── agent.py           # Pydantic AI agent definition
│       │   ├── tools.py           # Tool definitions (Pydantic models)
│       │   ├── prompts.py         # System prompt assembly
│       │   └── memory_extract.py  # Memory extraction schema
│       ├── gateway/
│       │   ├── adapter.py         # ABC for platform adapters
│       │   ├── gateway.py         # Multiplexer
│       │   ├── whatsapp.py
│       │   ├── telegram.py
│       │   └── discord.py
│       ├── agents/
│       │   ├── manager.py         # Spawn/kill/merge sub-agents
│       │   ├── bus.py             # Inter-agent message bus
│       │   └── lifecycle.py       # State machine
│       ├── memory/
│       │   ├── integration.py     # MemGate wrapper for core loop
│       │   └── merge.py           # Sub-agent memory merge
│       ├── autonomy/
│       │   ├── classifier.py      # Risk classification
│       │   └── policy.py          # Rule engine + YAML config
│       ├── sandbox/
│       │   ├── docker.py          # Container pool management
│       │   └── profiles.py        # Permission profiles
│       ├── scheduler/
│       │   ├── cron.py            # APScheduler integration
│       │   └── triggers.py        # Event & condition triggers
│       └── skills/
│           ├── registry.py        # Skill index + MemGate query
│           └── loader.py          # Static skill file loader
├── skills/                        # Static skill definitions
├── config/
│   ├── autonomy.yaml
│   ├── schedule.yaml
│   └── settings.yaml
├── tests/
├── docs/
│   └── design/                    # This design document
├── Dockerfile                     # Sandbox base image
├── docker-compose.yaml
├── pyproject.toml
└── CLAUDE.md
```

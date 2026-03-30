# Skill Registry & Cross-Referencing

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  SKILL REGISTRY                      │
│                                                      │
│  ┌───────────────────────────────────────────────┐   │
│  │              In-Memory Index                   │   │
│  │  (fast lookup by name, tag, capability)        │   │
│  └───────────────────────┬───────────────────────┘   │
│                          │                           │
│  Sources:                │                           │
│  ├── /skills/ directory (static skill files)         │
│  ├── MemGate (learned/discovered skills)             │
│  └── Sub-agent results (skills created on the fly)   │
└─────────────────────────────────────────────────────┘
```

## Skill Definition

```json
{
  "name": "check_btc_price",
  "description": "Fetch current Bitcoin price via CoinGecko API",
  "tags": ["finance", "crypto", "web"],
  "tools_required": ["web_search", "bash"],
  "autonomy": "low_risk",
  "steps": "1. Search CoinGecko API...",
  "learned_from": "agent_research_2026-03-28",
  "usage_count": 14,
  "last_used": "2026-03-30",
  "related_skills": ["portfolio_report", "alerts"]
}
```

## How Skills Live in MemGate

Skills are stored as structured facts via `ingest_structured`:

```json
{
  "content": "Skill: check_btc_price - fetches current BTC price via CoinGecko API, parses JSON, returns USD value. Used 14 times. Works well with portfolio_report skill.",
  "entities": [
    {"name": "check_btc_price", "attribute": "type", "value": "skill"},
    {"name": "check_btc_price", "attribute": "tools", "value": "web_search,bash"},
    {"name": "check_btc_price", "attribute": "autonomy", "value": "low_risk"},
    {"name": "check_btc_price", "attribute": "usage_count", "value": "14", "previous": "13"}
  ],
  "relationships": [
    {"a": "check_btc_price", "relation": "composes_with", "b": "portfolio_report"},
    {"a": "check_btc_price", "relation": "composes_with", "b": "price_alerts"}
  ]
}
```

## Cross-Referencing in Action

When the agent receives a task, the core loop:

1. Calls `MemGate.search("task description")` — retrieves relevant memories *including skill memories*
2. MemGate's entity linking connects related skills via `relationships`
3. The LLM sees: "You've done something like this before using skill X, which composes well with skill Y"
4. The agent can then **combine skills** it learned separately

**Example:** User asks "How's my crypto portfolio doing?"
- MemGate retrieves: `check_btc_price` skill, `portfolio_report` skill, the `composes_with` relationship between them, and past memories of portfolio values
- LLM assembles a plan using both skills + historical context

## Skill Learning Lifecycle

```
1. Sub-agent completes a novel task
2. Main agent's LLM evaluates: "This is a reusable pattern"
3. LLM extracts skill definition as structured JSON
4. Ingested into MemGate with entity/relationship data
5. Next time a similar task appears → MemGate retrieves it
6. Usage count increments, related_skills grow over time
```

## Static vs Learned Skills

- **Static** — hand-written skill files in `/skills/` directory. Always available, version-controlled.
- **Learned** — discovered through use, stored only in MemGate. Emerge organically. Can be "promoted" to static by exporting from MemGate to a file.

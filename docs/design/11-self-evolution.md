# Self-Evolution System

## Three Evolution Mechanisms

### 1. Skill Learning (Passive)

Every task completion:
1. LLM evaluates: "Is this a reusable pattern?"
2. If yes → extract skill definition as structured JSON
3. Ingest into MemGate with entities and relationships
4. Cross-reference with existing skills
5. Over time: a skill graph emerges organically

MemGate's novelty gate prevents duplicate skills — only genuinely new patterns are stored.

### 2. Behavior Adaptation (Active)

The agent reflects on its own performance:

- Tracks success/failure per skill
- Tracks user corrections (hallucination patterns, preference misses)
- Periodic self-review (scheduled): "What went wrong this week?"
  - Spawns a sub-agent to analyze patterns
  - Produces improvement suggestions
  - Stores as behavioral directives
- System prompt evolves dynamically: retrieved behavioral directives are injected into the prompt

### 3. Code Self-Modification (Guarded)

The agent can modify its own code within strict boundaries:

**Allowed:**
- Write new skill files to `/skills/`
- Generate new tool definitions
- Adjust config (autonomy rules, schedule)
- Create new platform adapter stubs

**Guardrails:**
- Core loop is **READ-ONLY** to the agent
- All self-modifications require **user confirmation**
- Changes go to a **staging branch** (git)
- Tests must pass before merge
- Rollback: `git revert` if issues detected

## Evolution Data Flow

```python
class EvolutionSignal(BaseModel):
    new_skill: dict | None = None          # Extracted skill definition
    correction: dict | None = None         # Error pattern record
    behavior_note: str | None = None       # "User prefers concise answers"
    config_suggestion: dict | None = None  # "Set check_btc to low_risk"
```

The LLM returns an `EvolutionSignal` with every response. The core loop processes it:
- `new_skill` → ingest into MemGate as skill entity
- `correction` → ingest as error pattern
- `behavior_note` → ingest as behavioral directive
- `config_suggestion` → queue for user confirmation

## Example Over Time

```
Week 1: Agent learns "check_btc_price" skill
Week 2: Agent learns "portfolio_report" skill
Week 3: Agent discovers they compose well → stores relationship
Week 4: User corrects a hallucination about ETH prices
        → Agent stores error pattern
Week 5: Self-review sub-agent notices: "3 crypto hallucinations
        this month, all from training data. Recommendation:
        always use web_search for live prices."
        → Stored as behavioral directive
Week 6: Agent's system prompt now includes: "ALWAYS use
        web_search for live price data."
        → Hallucination pattern eliminated
```

## Modifiable vs Protected Boundary

```
AGENT CAN MODIFY:              AGENT CANNOT MODIFY:
├── /skills/*                   ├── src/zoomac/core/*
├── config/autonomy.yaml        ├── src/zoomac/brain/agent.py
├── config/schedule.yaml        ├── src/zoomac/agents/manager.py
├── new tool definitions        ├── src/zoomac/autonomy/*
└── new adapter stubs           └── Dockerfile, pyproject.toml
     (user confirms)                 (manual only)
```

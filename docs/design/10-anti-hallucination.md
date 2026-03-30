# Anti-Hallucination Strategy

## Defense in Depth — 6 Layers

### Layer 1: MemGate as Ground Truth

Force the agent to **retrieve before reasoning**. Before answering factual questions, the agent MUST call `search_memory()` or `facts()`.

```python
class AgentResponse(BaseModel):
    message: str
    memory: MemoryExtract
    sources: list[str]          # which memories/facts were used
    confidence: float           # 0-1, LLM self-assessed
    needs_verification: bool    # LLM flags uncertainty
```

- `sources` field forces the LLM to cite which MemGate memories it used — if it can't cite, it's likely hallucinating
- MemGate's entity/fact system acts as a structured knowledge base

### Layer 2: Pydantic Validation as Guardrail

Structured outputs can't drift. If the LLM returns malformed data, Pydantic AI retries automatically. Type enforcement catches nonsense.

```python
from pydantic import field_validator

class AgentResponse(BaseModel):
    confidence: float

    @field_validator("confidence")
    @classmethod
    def check_confidence(cls, v):
        if not 0 <= v <= 1:
            raise ValueError("Confidence must be 0-1")
        return v
```

### Layer 3: Tool-Use Forcing

System prompt rule:

```
For any factual claim about entities, dates, numbers, or past events —
you MUST call search_memory() or check_facts() first. NEVER answer from
your training data alone when MemGate has relevant context. If search
returns nothing, say you don't know.
```

Shifts the agent from generative mode (hallucination-prone) to retrieval mode (grounded).

### Layer 4: Verification Loop for High-Risk Actions

```
LLM says "do X" → Verify claims → Execute or reject

Verification strategies:
├── Fact-check against MemGate facts/entities
├── If confidence < 0.7 → spawn verifier sub-agent
├── If action is high-risk → require 2 sources
└── If claim contradicts stored facts → flag conflict
```

```python
@agent.tool
async def verify_claim(ctx, claim: str) -> str:
    """Cross-reference a claim against stored facts."""
    mg: MemGate = ctx.deps.memgate
    facts = mg.facts()
    memories = mg.search(claim, top_k=5)
    return format_evidence(claim, facts, memories)
```

### Layer 5: Self-Evolving Hallucination Detection

The agent learns its own failure modes over time:

```python
# When user corrects the agent:
correction_payload = {
    "content": f"CORRECTION: I said '{wrong}' but correct is '{right}'. Error: hallucination.",
    "entities": [
        {"name": "zoomac", "attribute": "known_error_pattern",
         "value": f"hallucinated about {topic}"}
    ],
    "update": True
}
mg.ingest_structured(correction_payload)
```

Over time, MemGate accumulates patterns of when the agent hallucinates. The system prompt dynamically includes:

```
You have a history of hallucinating about {retrieved error patterns}.
Be extra cautious and always verify when these topics come up.
```

### Layer 6: Sub-Agent Cross-Verification

For critical tasks, spawn two sub-agents independently and compare:

```
Main: "Is this contract safe to sign?"
  → spawn agent_legal_1 (approach A)
  → spawn agent_legal_2 (approach B)
  → compare results
  → if disagree → flag to user with both analyses
  → if agree → higher confidence
```

## Cost Summary

| Layer | Mechanism | Cost | Frequency |
|-------|-----------|------|-----------|
| MemGate retrieval | Ground in stored facts | 1 search call | Every turn |
| Pydantic validation | Structural correctness | Free | Every turn |
| Tool-use forcing | Retrieve, don't generate | System prompt | Every turn |
| Confidence scoring | LLM self-assessment | 1 output field | Every turn |
| Verification loop | Cross-check claims | 1 extra tool call | High-risk only |
| Correction learning | Remember past errors | MemGate ingest | On correction |
| Sub-agent consensus | Independent verification | 2x LLM cost | Critical only |

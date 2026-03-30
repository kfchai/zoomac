# Error Handling & Resilience

## Circuit Breaker (Per Component)

```
States: CLOSED → OPEN → HALF_OPEN

Applied to:
├── LLM providers (rate limits, outages)
├── Platform adapters (API failures)
├── Docker sandbox (container failures)
└── MemGate (DB corruption recovery)
```

## Recovery Strategies

### LLM Failure
1. Retry with exponential backoff
2. Fallback to next provider (claude → gpt-4o → ollama local)
3. Queue event, resume when available

### Gateway Failure
1. Buffer outbound messages
2. Reconnect with backoff
3. Alert user on other active platform

### Sub-Agent Failure
1. Capture partial results
2. Report to main agent
3. Main decides: retry / reassign / abort

### MemGate Failure
1. SQLite WAL recovery (automatic)
2. Degrade to session-only mode
3. Alert: "Memory offline, operating without long-term context"

## Event Queue Durability

- Queue persisted to disk (SQLite)
- Unprocessed events survive restarts
- Dead letter queue for failed events
- Replay capability for debugging

## Key Principle

Degrade gracefully, never crash silently. If memory is down, keep running without it. If one LLM provider is down, fall to the next. If a platform disconnects, buffer and retry.

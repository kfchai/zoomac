# Scheduler & Background Tasks

## Configuration

```yaml
# config/schedule.yaml

jobs:
  - name: daily_digest
    cron: "0 8 * * *"
    task: "Summarize yesterday's messages and tasks"
    spawn_agent: true

  - name: inbox_poll
    cron: "*/5 * * * *"
    task: "Check platforms for new messages"
    spawn_agent: false  # runs in main loop

  - name: memory_consolidate
    cron: "0 3 * * *"
    task: "MemGate.consolidate()"
    spawn_agent: false
```

## Trigger Types

| Type | Description | Example |
|------|-------------|---------|
| `cron` | Time-based | "Every day at 8am" |
| `event` | On message, skill complete, etc. | "When a sub-agent finishes" |
| `condition` | MemGate query matches threshold | "When BTC drops below $50K" |

## How It Works

- Each trigger produces an event pushed to the **EVENT QUEUE**
- Processed by the Core Loop like any other event
- `spawn_agent: true` — scheduler tells the Core Loop to spawn a sub-agent for the task
- `spawn_agent: false` — the main agent handles it directly

## Condition Triggers

The scheduler periodically evaluates conditions against MemGate facts and fires events when thresholds are met.

```yaml
conditions:
  - name: btc_price_alert
    check_interval: "*/15 * * * *"
    query: "Bitcoin current price"
    condition: "price < 50000"
    action: "Alert: BTC dropped below $50K"
    platform: whatsapp  # where to send the alert
```

# Autonomy Manager & Risk Classification

## Architecture

```
Action from LLM
     │
     ▼
┌──────────────┐
│ Risk Classifier│
│               │
│ Rules:        │
│ ├── action_type (read/write/send/delete/spend)
│ ├── platform (terminal/whatsapp/discord/...)
│ ├── skill_override (per-skill autonomy level)
│ └── sandbox_scope (file paths, network access)
└──────┬───────┘
       │
       ▼
┌──────────────────────────────────┐
│         LOW          │    HIGH   │
│  read files          │ send msg  │
│  web search          │ delete    │
│  run sandboxed cmd   │ API calls │
│  MemGate query       │ spend $   │
│  local computation   │ modify    │
│                      │ git push  │
│  → execute           │ → confirm │
└──────────────────────┴──────────┘
```

## Confirmation Routing

- If user is active on CLI → prompt there
- If user is on a platform → ask on that platform
- If offline → queue, wait, or timeout

## Configuration

```yaml
# config/autonomy.yaml

defaults:
  read: allow
  write: allow
  send_message: confirm
  delete: confirm
  financial: confirm

overrides:
  skills:
    check_btc_price: allow
    send_daily_report: allow
  platforms:
    discord: allow
    whatsapp: confirm

timeout:
  confirm_wait_minutes: 30
  on_timeout: expire  # or "escalate"
```

## Key Behaviors

- **Rule cascade** — skill override > platform override > action type default. Most specific wins.
- **Confirmation is async** — the agent doesn't block. It queues the pending action, continues processing other events, and executes when the user confirms.
- **Timeout policy** — configurable per-risk level. If no confirmation in N minutes, either expire (safe default) or escalate to another platform.
- **Audit log** — every action (confirmed or auto-executed) is logged with timestamp, risk level, and decision reason. Queryable via MemGate.

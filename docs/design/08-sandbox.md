# Sandbox & Execution Environment

## Docker Container Pool

```
Base Image: zoomac-sandbox
├── Python, Node.js, common CLI tools
├── No network by default
├── Read-only root filesystem
└── /workspace mounted (ephemeral tmpfs)
```

## Permission Profiles

| Profile | Network | Mounts | Use Case |
|---------|---------|--------|----------|
| `minimal` | No | None | Pure computation, safe scripts |
| `standard` | Yes | /workspace rw | Web research, API calls |
| `project` | Yes | + project dir ro | Code analysis, testing |
| `full` | Host | + home dir rw | Trusted tasks, builds |

## Execution Flow

1. Core Loop receives tool call from LLM
2. Autonomy Manager approves
3. Sandbox Manager selects permission profile
4. Spin up container (or reuse from warm pool)
5. Execute command, capture stdout/stderr
6. Return result to Core Loop
7. Container destroyed (or returned to pool)

## Resource Limits

| Resource | Default | Configurable |
|----------|---------|-------------|
| CPU | Per-task | Yes |
| Memory | 512MB | Yes |
| Timeout | 60s | Yes |
| Disk | 1GB tmpfs | Yes |
| Privileged | No | Never |
| Docker socket | No | Never |

## Sub-Agent Sandboxing

Each sub-agent gets its own container with the permission profile matching its task brief:
- Research agent → `standard` (network access)
- Code agent → `project` (repo mounted read-only)
- Monitoring agent → `standard` (network for API polling)

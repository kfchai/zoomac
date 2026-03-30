# Zoomac Agent — Design Document

## Overview

Zoomac is a **hybrid AI agent** (coding + personal assistant) that connects to messaging platforms, runs autonomously, and learns/memorizes skills over time. It is a personal tool tailored to a single user's workflows.

## Core Stack

- **Language:** Python 3.12+
- **LLM Orchestration:** Pydantic AI (model-agnostic, typed outputs)
- **Memory:** MemGate (intelligent memory gating, zero LLM cost)
- **Async Runtime:** asyncio + uvloop
- **Sandbox:** Docker containers
- **Scheduler:** APScheduler
- **Observability:** Pydantic Logfire (optional)

## Key Properties

| Property | Choice |
|----------|--------|
| Target user | Single user, heavy daily use |
| Platforms | WhatsApp, Telegram, Discord (extensible) |
| Task scope | All — coding, scheduling, research, files, automation, finance, social, email, data pipelines |
| LLM providers | Model-agnostic (Anthropic, OpenAI, Google, local, etc.) |
| Execution | Local (interactive) + server (background), sandboxed |
| Autonomy | Act freely on low-risk, confirm on high-risk, configurable per-skill |
| Privacy | Cloud LLM calls OK, all memory/state stays on your infrastructure |
| Reliability | Auto-recover, queue missed messages, graceful resume |
| Maintenance | Semi-automated — self-update with approval, health monitoring |

## Design Principles

1. **MemGate is the brain** — long-term memory, skill recall, entity/relationship tracking
2. **LLM decides what to remember** — structured extraction per turn, MemGate gates as 2nd filter
3. **Skills are learned, not just configured** — memorized via MemGate, cross-referenced over time
4. **Sub-agents for short-term tasks** — isolated memory, merged on completion
5. **Self-evolving with guardrails** — learns from mistakes, core loop stays read-only
6. **Defense in depth against hallucinations** — 6 layers from retrieval grounding to sub-agent consensus
7. **Degrade gracefully** — if memory is down, keep running; if one LLM is down, fall to next

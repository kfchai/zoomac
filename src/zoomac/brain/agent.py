"""Pydantic AI agent definition for Zoomac."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai import Agent, RunContext

from zoomac.brain.memory_extract import AgentResponse
from zoomac.brain.prompts import SYSTEM_PROMPT
from zoomac.memory.integration import MemoryManager


@dataclass
class ZoomacDeps:
    """Dependencies injected into every agent run."""

    memgate: MemoryManager


def create_agent(model: str) -> Agent[ZoomacDeps, AgentResponse]:
    """Create and configure the Zoomac Pydantic AI agent."""

    agent: Agent[ZoomacDeps, AgentResponse] = Agent(
        model,
        output_type=AgentResponse,
        instructions=SYSTEM_PROMPT,
        deps_type=ZoomacDeps,
    )

    @agent.tool
    async def search_memory(ctx: RunContext[ZoomacDeps], query: str) -> str:
        """Search long-term memory for relevant context. Use this before answering
        factual questions about past events, entities, or previously discussed topics."""
        context = ctx.deps.memgate.retrieve_context(query)
        if not context:
            return "No relevant memories found."
        return context

    @agent.tool
    async def check_facts(ctx: RunContext[ZoomacDeps], entity: str) -> str:
        """Retrieve known facts about a specific entity from memory."""
        facts = ctx.deps.memgate.facts(entity=entity)
        if not facts:
            return f"No facts stored about '{entity}'."
        lines = []
        for f in facts:
            lines.append(f"- {f.get('entity', '')}.{f.get('attribute', '')} = {f.get('value', '')}")
        return "\n".join(lines)

    @agent.tool
    async def memory_status(ctx: RunContext[ZoomacDeps]) -> str:
        """Check the current status of the memory system."""
        status = ctx.deps.memgate.status()
        return (
            f"Memories: {status.get('n_memories', 0)}, "
            f"Total ingested: {status.get('total_ingested', 0)}, "
            f"Filter rate: {status.get('filter_rate', 0):.1%}, "
            f"Clusters: {status.get('n_clusters', 0)}"
        )

    return agent

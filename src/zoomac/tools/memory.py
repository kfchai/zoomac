"""Built-in memory tools."""

from __future__ import annotations

from zoomac.brain.deps import ZoomacDeps
from zoomac.autonomy.classifier import ActionType
from zoomac.tools.base import ToolCapabilities, ToolDefinition, ToolSpec
from zoomac.tools.registry import ToolRegistry


async def search_memory(deps: ZoomacDeps, query: str) -> str:
    """Search long-term memory for relevant context."""
    context = deps.memgate.retrieve_context(query)
    if not context:
        return "No relevant memories found."
    return context


async def check_facts(deps: ZoomacDeps, entity: str) -> str:
    """Retrieve known facts about a specific entity from memory."""
    facts = deps.memgate.facts(entity=entity)
    if not facts:
        return f"No facts stored about '{entity}'."

    lines = []
    for fact in facts:
        lines.append(
            f"- {fact.get('entity', '')}.{fact.get('attribute', '')} = {fact.get('value', '')}"
        )
    return "\n".join(lines)


async def memory_status(deps: ZoomacDeps) -> str:
    """Check the current status of the memory system."""
    status = deps.memgate.status()
    return (
        f"Memories: {status.get('n_memories', 0)}, "
        f"Total ingested: {status.get('total_ingested', 0)}, "
        f"Filter rate: {status.get('filter_rate', 0):.1%}, "
        f"Clusters: {status.get('n_clusters', 0)}"
    )


def build_memory_tool_registry() -> ToolRegistry:
    """Create the registry for built-in memory tools."""
    capabilities = ToolCapabilities(read_only=True, supports_parallel=True)
    registry = ToolRegistry(name="memory")
    registry.extend(
        [
            ToolDefinition(
                spec=ToolSpec(
                    name="search_memory",
                    description=(
                        "Search long-term memory for relevant context. Use this before "
                        "answering factual questions about past events, entities, or "
                        "previously discussed topics."
                    ),
                    capabilities=capabilities,
                    approval_action_type=ActionType.MEMORY_QUERY.value,
                ),
                handler=search_memory,
            ),
            ToolDefinition(
                spec=ToolSpec(
                    name="check_facts",
                    description="Retrieve known facts about a specific entity from memory.",
                    capabilities=capabilities,
                    approval_action_type=ActionType.MEMORY_QUERY.value,
                ),
                handler=check_facts,
            ),
            ToolDefinition(
                spec=ToolSpec(
                    name="memory_status",
                    description="Check the current status of the memory system.",
                    capabilities=capabilities,
                    approval_action_type=ActionType.MEMORY_QUERY.value,
                ),
                handler=memory_status,
            ),
        ]
    )
    return registry

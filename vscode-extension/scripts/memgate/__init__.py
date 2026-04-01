"""MemGate — Intelligent Memory Gating for AI Agents.

Usage:
    from memgate import MemGate

    mg = MemGate(project_dir=".")
    mg.ingest("distilled knowledge here")     # -> {stored, quality, ...}
    mg.search("query", top_k=10)              # -> [{content, relevance, topic}]
    mg.context("what do I know about X")      # -> {context, n_memories, n_clusters}
    mg.status()                               # -> {n_memories, topics, filter_rate, ...}
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from memgate.config import MemGateConfig
from memgate.engine import MemGateEngine


class MemGate:
    """Public API for the MemGate memory engine.

    Zero-config usage: MemGate(project_dir=".") creates a .memgate.db in the
    given directory and is immediately ready for ingest/search/context.
    """

    def __init__(self, project_dir: str | Path = ".", **kwargs: Any) -> None:
        config = MemGateConfig.from_kwargs(**kwargs)
        self._engine = MemGateEngine(project_dir=project_dir, config=config)

    def ingest(self, content: str, metadata: dict | None = None) -> dict:
        """Ingest content through the memory pipeline.

        Args:
            content: Text content to potentially store.
            metadata: Optional metadata dict.

        Returns:
            Dict with keys: stored, quality, novelty, continuity, reason, latency_us
        """
        return self._engine.ingest(content, metadata=metadata)

    def ingest_structured(self, payload: dict, metadata: dict | None = None) -> dict:
        """Ingest with LLM-structured extraction data.

        Runs the same embed/novelty/gate pipeline, but uses LLM-provided
        entities, relationships, temporal refs, and update signals instead
        of regex extraction. Zero additional LLM cost — the agent LLM is
        already running and provides the structured data alongside its response.

        Args:
            payload: Dict with keys:
                content (str): Text to store.
                entities (list[dict]): [{name, attribute, value, previous?}]
                relationships (list[dict]): [{a, relation, b}]
                temporal (str): Date/time string.
                update (bool): If True, supersede conflicting memories.
            metadata: Optional metadata dict.

        Returns:
            Dict with keys: stored, quality, novelty, continuity, reason, latency_us
        """
        return self._engine.ingest_structured(payload, metadata=metadata)

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        """Search for relevant memories.

        Args:
            query: Search query text.
            top_k: Maximum number of results.

        Returns:
            List of dicts with: content, relevance, topic
        """
        return self._engine.search(query, top_k=top_k)

    def context(self, query: str, max_tokens: int = 2000) -> dict:
        """Get assembled context for a query.

        Args:
            query: Query text.
            max_tokens: Maximum token budget.

        Returns:
            Dict with: context, n_memories, n_clusters
        """
        return self._engine.context(query, max_tokens=max_tokens)

    def status(self) -> dict:
        """Get engine status and statistics.

        Returns:
            Dict with: n_memories, topics, filter_rate, stats
        """
        return self._engine.status()

    def consolidate(self) -> list[dict]:
        """Run extractive consolidation on all clusters.

        Returns:
            List of cluster summary dicts with: cluster_id, keywords,
            n_representatives, n_memories.
        """
        return self._engine.consolidate()

    def facts(self, entity: str | None = None) -> list[dict]:
        """Get stored facts, optionally filtered by entity.

        Args:
            entity: Optional entity name to filter by (e.g. "user").

        Returns:
            List of fact dicts with: entity, attribute, value, confidence.
        """
        return self._engine.facts(entity=entity)

    def profile(self, entity: str) -> dict:
        """Get a structured profile for an entity.

        Args:
            entity: Entity name (e.g. "alice", "caroline").

        Returns:
            Dict with: entity, attributes, attribute_history, mention_count,
            first_seen, last_seen, related_memory_ids.
        """
        return self._engine.profile(entity)

    def entities(self) -> list[dict]:
        """List all known entities with counts.

        Returns:
            List of dicts with: entity, mention_count, fact_count, last_seen.
        """
        return self._engine.entities()

    def relationships(self, entity: str) -> list[dict]:
        """Get relationships for an entity.

        Args:
            entity: Entity name (e.g. "alice", "caroline").

        Returns:
            List of dicts with: entity, relation, direction.
        """
        return self._engine.relationships(entity)

    def related_memories(self, entity_a: str, entity_b: str, top_k: int = 10) -> list[dict]:
        """Get memories where both entities are mentioned.

        Args:
            entity_a: First entity name.
            entity_b: Second entity name.
            top_k: Maximum number of results.

        Returns:
            List of dicts with: content, relevance, cluster_id, tier.
        """
        return self._engine.related_memories(entity_a, entity_b, top_k=top_k)

    def flush(self) -> None:
        """Force checkpoint to persist current state."""
        self._engine.flush()

    def close(self) -> None:
        """Checkpoint and close. Call when done."""
        self._engine.close()

    def __enter__(self) -> MemGate:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


__all__ = ["MemGate", "MemGateConfig"]

"""Retriever — three-tier search with temporal boost, entity tier, and recency dedup."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from memgate.config import MemGateConfig
    from memgate.store import MemoryStore
    from memgate.temporal import TemporalRef
    from memgate.topics import TopicModel


class Retriever:
    """Three-tier memory retrieval with recency-aware deduplication.

    Tier 0: Cluster summary search (match query against cluster centroids).
    Tier 1: Topic-filtered k-NN (search within activated clusters).
    Tier 2: Global k-NN fallback (fill remaining slots).
    Post: Recency dedup — when two results are >threshold similar, keep the newer one.
    """

    def __init__(self, config: MemGateConfig | None = None) -> None:
        from memgate.config import MemGateConfig

        self._config = config or MemGateConfig()

    def search(
        self,
        query_embedding: np.ndarray,
        h1_activations: np.ndarray | None,
        store: MemoryStore,
        topic_model: TopicModel | None = None,
        top_k: int = 10,
        temporal_ref: TemporalRef | None = None,
        entity_hint: str | None = None,
        entity_hints: list[str] | None = None,
    ) -> list[dict]:
        """Search for relevant memories using entity + three-tier retrieval + temporal boost.

        Args:
            query_embedding: Query embedding vector (384-dim).
            h1_activations: MLP hidden layer activations for topic assignment.
            store: Memory store to search.
            topic_model: Optional topic model for cluster-based search.
            top_k: Maximum number of results.
            temporal_ref: Optional temporal reference for boosting time-matched results.
            entity_hint: Deprecated, use entity_hints. Single entity name.
            entity_hints: Optional list of entity names to fetch entity-linked memories.

        Returns:
            List of memory dicts sorted by relevance.
        """
        seen_ids: set[str] = set()
        results: list[dict] = []

        # Normalize entity_hint -> entity_hints for backwards compat
        if entity_hints is None and entity_hint is not None:
            entity_hints = [entity_hint]

        # Entity tier: fetch memories linked to entities
        if entity_hints:
            if len(entity_hints) >= 2:
                # Multi-entity: find co-presence memories first
                # Intersect memory IDs across all hinted entities
                per_entity_ids: list[set[str]] = []
                per_entity_map: dict[str, dict] = {}
                for ent in entity_hints:
                    ent_results = store.get_memories_by_entity(
                        ent, query_embedding, top_k=top_k * 3
                    )
                    ids = set()
                    for r in ent_results:
                        ids.add(r["id"])
                        per_entity_map[r["id"]] = r
                    per_entity_ids.append(ids)

                # Co-presence: memories that appear in ALL entity result sets
                co_ids = per_entity_ids[0]
                for s in per_entity_ids[1:]:
                    co_ids = co_ids & s

                # Add co-presence memories first
                for mid in co_ids:
                    if mid not in seen_ids:
                        seen_ids.add(mid)
                        r = per_entity_map[mid]
                        r["tier"] = "co_presence"
                        results.append(r)

                # Then add remaining per-entity memories
                for mid, r in per_entity_map.items():
                    if mid not in seen_ids:
                        seen_ids.add(mid)
                        r["tier"] = "entity"
                        results.append(r)
            else:
                # Single entity hint
                entity_results = store.get_memories_by_entity(
                    entity_hints[0], query_embedding, top_k=top_k
                )
                for r in entity_results:
                    if r["id"] not in seen_ids:
                        seen_ids.add(r["id"])
                        r["tier"] = "entity"
                        results.append(r)

        # Tier 0: Cluster summary search — find best cluster(s) via centroid matching
        tier0_cluster_ids: list[int] = []
        summaries = store.get_cluster_summaries()
        if summaries:
            query_norm = query_embedding / (np.linalg.norm(query_embedding) + 1e-10)
            scored = []
            for s in summaries:
                centroid = s["centroid_embedding"]
                c_norm = centroid / (np.linalg.norm(centroid) + 1e-10)
                sim = float(np.dot(query_norm, c_norm))
                scored.append((s["cluster_id"], sim))
            scored.sort(key=lambda x: x[1], reverse=True)
            # Use top cluster(s) with sim > 0.3
            for cid, sim in scored:
                if sim > 0.3:
                    tier0_cluster_ids.append(cid)
                    if len(tier0_cluster_ids) >= 2:
                        break

        # Search Tier 0 clusters
        for cid in tier0_cluster_ids:
            tier0_results = store.search_by_cluster(
                cid, query_embedding, top_k=top_k
            )
            for r in tier0_results:
                if r["id"] not in seen_ids:
                    seen_ids.add(r["id"])
                    r["tier"] = "summary"
                    results.append(r)

        # Tier 1: Topic-filtered search (via h1 activations)
        if h1_activations is not None and topic_model is not None:
            cluster_id, _ = topic_model.assign(h1_activations)
            if cluster_id is not None and cluster_id not in tier0_cluster_ids:
                tier1 = store.search_by_cluster(
                    cluster_id, query_embedding, top_k=top_k
                )
                for r in tier1:
                    if r["id"] not in seen_ids:
                        seen_ids.add(r["id"])
                        r["tier"] = "topic"
                        results.append(r)

        # Tier 2: Global fallback (fill remaining slots)
        remaining = top_k - len(results)
        if remaining > 0:
            tier2 = store.search_by_vector(
                query_embedding, top_k=top_k + len(results)
            )
            for r in tier2:
                if r["id"] not in seen_ids:
                    seen_ids.add(r["id"])
                    r["tier"] = "global"
                    results.append(r)
                    if len(results) >= top_k * 2:
                        break

        # Sort by relevance descending
        results.sort(key=lambda x: x.get("relevance", 0), reverse=True)

        # Temporal boost (before dedup so time-relevant memories float up)
        if temporal_ref is not None:
            results = self._apply_temporal_boost(results, temporal_ref)
            results.sort(key=lambda x: x.get("relevance", 0), reverse=True)

        # Recency dedup
        results = self._dedup_with_recency(results)

        return results[:top_k]

    def _apply_temporal_boost(
        self, results: list[dict], temporal_ref: TemporalRef
    ) -> list[dict]:
        """Boost relevance of results that are temporally close to the reference.

        Uses Gaussian decay: closer timestamps get a larger boost.
        """
        if temporal_ref.timestamp is None:
            return results

        width_map = {
            "exact": 3600.0,
            "day": 86400.0,
            "month": 30.0 * 86400,
            "year": 365.0 * 86400,
            "relative": 7.0 * 86400,
        }
        width = width_map.get(temporal_ref.precision, 86400.0)
        boost_weight = self._config.temporal_boost_weight

        for r in results:
            event_ts = r.get("event_timestamp") or r.get("created_at", 0)
            if event_ts and event_ts > 0:
                delta = abs(event_ts - temporal_ref.timestamp)
                boost = math.exp(-0.5 * (delta / width) ** 2)
                r["relevance"] *= (1.0 + boost_weight * boost)

        return results

    def _dedup_with_recency(self, results: list[dict]) -> list[dict]:
        """When two results are very similar (cosine > threshold), keep the newer one.

        O(k^2) pairwise comparison. k is typically 10-20, so ~45-190 comparisons.
        """
        if len(results) <= 1:
            return results

        threshold = self._config.conflict_similarity_threshold

        # Collect embeddings
        embeddings = []
        for r in results:
            emb = r.get("embedding")
            if emb is not None:
                embeddings.append(emb / (np.linalg.norm(emb) + 1e-10))
            else:
                embeddings.append(None)

        to_remove: set[int] = set()

        for i in range(len(results)):
            if i in to_remove:
                continue
            for j in range(i + 1, len(results)):
                if j in to_remove:
                    continue
                if embeddings[i] is None or embeddings[j] is None:
                    continue

                sim = float(np.dot(embeddings[i], embeddings[j]))
                if sim >= threshold:
                    # Keep the newer one (higher created_at)
                    t_i = results[i].get("created_at", 0)
                    t_j = results[j].get("created_at", 0)
                    if t_i >= t_j:
                        to_remove.add(j)
                    else:
                        to_remove.add(i)
                        break  # i is removed, no need to compare further

        return [r for idx, r in enumerate(results) if idx not in to_remove]

    @staticmethod
    def assemble_context(
        results: list[dict], max_tokens: int = 2000
    ) -> str:
        """Pack memory results into a context string within token budget.

        Uses a rough 4 chars/token estimate for budget packing.

        Args:
            results: List of memory dicts from search().
            max_tokens: Maximum token budget.

        Returns:
            Assembled context string.
        """
        if not results:
            return ""

        max_chars = max_tokens * 4  # rough estimate
        parts: list[str] = []
        total_chars = 0

        for r in results:
            content = r.get("content", "")
            if total_chars + len(content) + 2 > max_chars:
                # Try to fit a truncated version
                remaining = max_chars - total_chars - 2
                if remaining > 50:
                    parts.append(content[:remaining] + "...")
                break
            parts.append(content)
            total_chars += len(content) + 1  # +1 for newline

        return "\n".join(parts)

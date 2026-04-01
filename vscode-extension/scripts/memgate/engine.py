"""MemGateEngine — core pipeline orchestrator."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import numpy as np

from memgate.config import MemGateConfig
from memgate.consolidator import Consolidator
from memgate.embedder import Embedder
from memgate.facts import FactExtractor
from memgate.gate import QualityGate
from memgate.novelty import NoveltyDetector
from memgate.profiles import ProfileBuilder
from memgate.retriever import Retriever
from memgate.store import MemoryStore
from memgate.temporal import TemporalExtractor
from memgate.topics import TopicModel
from memgate.tracker import ContinuityTracker


class MemGateEngine:
    """Orchestrates the full MemGate pipeline.

    Components: Embedder -> NoveltyDetector -> ContinuityTracker
                -> QualityGate -> TopicModel -> MemoryStore -> Retriever
    """

    def __init__(
        self,
        project_dir: str | Path = ".",
        config: MemGateConfig | None = None,
        **kwargs: Any,
    ) -> None:
        self._config = config or MemGateConfig.from_kwargs(**kwargs)
        self._project_dir = Path(project_dir)

        # Components
        self._embedder = Embedder(self._config)
        self._novelty = NoveltyDetector(self._config)
        self._tracker = ContinuityTracker()
        self._gate = QualityGate(self._config)
        self._topics = TopicModel(
            top_neurons=self._config.top_neurons_per_term,
            overlap_threshold=self._config.cluster_overlap_threshold,
        )
        self._retriever = Retriever(self._config)
        self._consolidator = Consolidator(
            keywords_per_cluster=self._config.keywords_per_cluster,
            representatives_per_cluster=self._config.representatives_per_cluster,
        )
        self._fact_extractor = FactExtractor(
            similarity_threshold=self._config.conflict_similarity_threshold,
        )
        self._temporal = TemporalExtractor()

        # Store
        db_path = self._project_dir / self._config.db_filename
        self._store = MemoryStore(db_path, self._config)
        self._profile_builder = ProfileBuilder(self._store)

        # Projection matrix: 384 -> proj_dim (fixed random, seed=42)
        rng = np.random.RandomState(42)
        raw = rng.randn(self._config.proj_dim, self._embedder.dim).astype(np.float32)
        # Xavier-like normalization
        raw *= np.sqrt(2.0 / self._embedder.dim)
        self._proj_matrix = raw

        # Ring buffer: context_depth recent projections
        self._ring_buffer = np.zeros(
            (self._config.context_depth, self._config.proj_dim), dtype=np.float32
        )

        # Running stats for surprise z-score (EMA)
        self._error_mean = 0.0
        self._error_var = 1.0
        self._ingest_count = 0

        # Previous full embedding for continuity tracking
        self._prev_embedding: np.ndarray | None = None

        # Restore state from DB
        self._restore_state()

    def _restore_state(self) -> None:
        """Restore model state from SQLite if available."""
        state = self._store.load_model_state()
        if state is None:
            return

        if state.get("weights") is not None:
            self._novelty.set_weights(state["weights"])

        if state.get("running_stats") is not None:
            stats = state["running_stats"]
            if "error_mean" in stats:
                self._error_mean = float(stats["error_mean"])
            if "error_var" in stats:
                self._error_var = float(stats["error_var"])
            if "prev_embedding" in stats:
                self._prev_embedding = stats["prev_embedding"].astype(np.float32)
                self._tracker.previous_embedding = self._prev_embedding

        if state.get("ring_buffer") is not None:
            rb = state["ring_buffer"]
            if rb.shape == self._ring_buffer.shape:
                self._ring_buffer = rb.astype(np.float32)

        self._ingest_count = state.get("ingest_count", 0)

    def _checkpoint(self) -> None:
        """Save model state to SQLite."""
        running_stats: dict[str, Any] = {
            "error_mean": np.float64(self._error_mean),
            "error_var": np.float64(self._error_var),
        }
        if self._prev_embedding is not None:
            running_stats["prev_embedding"] = self._prev_embedding

        self._store.save_model_state(
            weights=self._novelty.get_weights(),
            running_stats=running_stats,
            ring_buffer=self._ring_buffer,
            ingest_count=self._ingest_count,
        )

    def _project(self, embedding: np.ndarray) -> np.ndarray:
        """Project 384-dim embedding to proj_dim."""
        return (self._proj_matrix @ embedding).astype(np.float32)

    def _build_context(self) -> np.ndarray:
        """Build flattened context vector from ring buffer with decay weights."""
        weights = np.array(
            self._config.context_decay_weights, dtype=np.float32
        )
        # Weighted combination: element-wise multiply each row by its weight
        weighted = self._ring_buffer * weights[:, np.newaxis]
        return weighted.flatten()

    def _compute_surprise(self, pred_error: float) -> float:
        """Compute surprise score using EMA z-score of prediction error.

        Returns z-score in [-3, 3]. Positive = more novel than average,
        negative = more predictable than average.
        """
        alpha = self._config.ema_alpha

        # Update running mean and variance (EMA)
        self._error_mean = (1 - alpha) * self._error_mean + alpha * pred_error
        diff = pred_error - self._error_mean
        self._error_var = (1 - alpha) * self._error_var + alpha * diff * diff

        # Z-score (allow full range for sigmoid mapping)
        std = max(np.sqrt(self._error_var), 1e-6)
        z_score = (pred_error - self._error_mean) / std

        return float(np.clip(z_score, -3.0, 3.0))

    def _update_ring_buffer(self, proj: np.ndarray) -> None:
        """Push new projection into ring buffer (FIFO, oldest evicted)."""
        self._ring_buffer = np.roll(self._ring_buffer, -1, axis=0)
        self._ring_buffer[-1] = proj

    def ingest(
        self, content: str, metadata: dict | None = None
    ) -> dict[str, Any]:
        """Ingest content through the full pipeline.

        Returns:
            Dict with keys: stored, quality, novelty, continuity, reason, latency_us
        """
        t0 = time.perf_counter()

        # 1. Embed
        embedding = self._embedder.embed(content)

        # 2. Project
        proj = self._project(embedding)

        # 3. Build context from ring buffer
        context = self._build_context()

        # 4. Novelty detector forward + learn
        term = content[:80]
        pred_error = self._novelty.update(context, proj, term=term)

        # 5. Compute surprise (z-score of prediction error)
        surprise = self._compute_surprise(pred_error)
        # Sigmoid mapping: z=0 -> 0.5, z=-3 -> 0.05, z=3 -> 0.95
        # Average content gets novelty ~0.5; below-average gets <0.5
        novelty = float(1.0 / (1.0 + np.exp(-surprise)))

        # 6. Compute continuity
        continuity = self._tracker.compute(embedding)
        self._tracker.update(embedding)
        self._prev_embedding = embedding.copy()

        # 7. Quality gate
        self._ingest_count += 1
        is_cold_start = self._ingest_count <= self._config.cold_start_count

        if is_cold_start:
            should_store = True
            quality_score = 1.0
            reason = f"cold start ({self._ingest_count}/{self._config.cold_start_count})"
        else:
            decision = self._gate.evaluate(novelty, continuity)
            should_store = decision.should_store
            quality_score = decision.quality_score
            reason = decision.explanation

        # 8. Topic assignment
        h1_cache = self._novelty.get_h1_cache()
        if len(h1_cache) >= 3:
            self._topics.extract(h1_cache)
        _, h1, _, _, _ = self._novelty.forward(context)
        cluster_id, cluster_label = self._topics.assign(h1)

        # 9. Store or skip
        superseded_ids: list[str] = []
        if should_store:
            mem_id = self._store.store_memory(
                content=content,
                embedding=embedding,
                proj_embedding=proj,
                novelty_score=novelty,
                continuity_score=continuity,
                quality_score=quality_score,
                cluster_id=cluster_id,
                cluster_label=cluster_label,
                metadata=metadata,
            )
            # Consolidate on high-quality stores
            if quality_score > 1.0:
                self._novelty.consolidate(min(1.0, quality_score - 0.7))

            # 9a. Update detection — mark conflicting memories as superseded
            conflicts = self._fact_extractor.find_conflicting_memories(
                embedding, content, self._store
            )
            for c in conflicts:
                self._store.mark_superseded(c["id"], mem_id)
                superseded_ids.append(c["id"])

            # 9b. Extract and store structured facts
            extracted_facts = self._fact_extractor.extract_facts(content)
            for fact in extracted_facts:
                # Check for existing facts with same (entity, attribute)
                existing = self._store.find_facts_by_entity_attribute(
                    fact["entity"], fact["attribute"]
                )
                new_fact_id = self._store.store_fact(
                    entity=fact["entity"],
                    attribute=fact["attribute"],
                    value=fact["value"],
                    source_memory_id=mem_id,
                )
                # Supersede old facts with same (entity, attribute)
                for old in existing:
                    if old["value"] != fact["value"]:
                        self._store.supersede_fact(old["id"], new_fact_id)

            # 9c. Extract temporal reference -> store event_timestamp
            temporal_refs = self._temporal.extract_from_text(content)
            if temporal_refs and temporal_refs[0].timestamp:
                self._store.update_event_timestamp(mem_id, temporal_refs[0].timestamp)

            # 9d. Extract entity mentions -> store in entity_mentions
            entities = self._fact_extractor.extract_entities(
                content, min_name_length=self._config.entity_min_name_length
            )
            for ent_name, role in entities:
                self._store.store_entity_mention(mem_id, ent_name, role)

            # 9e. Extract relationships -> store in relationships table
            rels = self._fact_extractor.extract_relationships(
                content, min_name_length=self._config.entity_min_name_length
            )
            for rel in rels:
                self._store.store_relationship(
                    rel["entity_a"], rel["relation"], rel["entity_b"],
                    source_memory_id=mem_id,
                )
        else:
            mem_id = self._store.log_skip(
                content=content,
                embedding=embedding,
                proj_embedding=proj,
                novelty_score=novelty,
                continuity_score=continuity,
                quality_score=quality_score,
                cluster_id=cluster_id,
                cluster_label=cluster_label,
                metadata=metadata,
            )

        # 10. Log decision
        self._store.log_decision(
            content=content,
            novelty_score=novelty,
            continuity_score=continuity,
            quality_score=quality_score,
            was_stored=should_store,
            reason=reason,
        )

        # 11. Update ring buffer
        self._update_ring_buffer(proj)

        # 12. Checkpoint every 10 ingestions
        if self._ingest_count % 10 == 0:
            self._checkpoint()

        # 13. Trigger consolidation every N ingests
        if self._ingest_count % self._config.consolidation_interval == 0:
            self.consolidate()

        latency_us = int((time.perf_counter() - t0) * 1_000_000)

        return {
            "stored": should_store,
            "quality": round(quality_score, 4),
            "novelty": round(novelty, 4),
            "continuity": round(continuity, 4),
            "cluster_id": cluster_id,
            "cluster_label": cluster_label,
            "reason": reason,
            "superseded": superseded_ids,
            "latency_us": latency_us,
        }

    def ingest_structured(
        self,
        payload: dict[str, Any],
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        """Ingest with LLM-structured extraction data.

        Runs the same embed/novelty/gate pipeline as ingest(), but uses
        LLM-provided entities, relationships, temporal refs, and update
        signals instead of regex extraction. This gives much higher
        extraction quality at zero additional LLM cost (the agent LLM
        is already running).

        Args:
            payload: Dict with keys:
                content (str, required): Text to store.
                entities (list[dict], optional): Each has:
                    name (str), attribute (str), value (str),
                    previous (str, optional) — old value being replaced.
                relationships (list[dict], optional): Each has:
                    a (str), relation (str), b (str).
                temporal (str, optional): Date/time string for event_timestamp.
                update (bool, optional): If True, aggressively search for
                    conflicting memories to supersede.
            metadata: Optional metadata dict.

        Returns:
            Same dict as ingest(): stored, quality, novelty, continuity, etc.
        """
        content = payload.get("content", "")
        if not content:
            return {
                "stored": False, "quality": 0.0, "novelty": 0.0,
                "continuity": 0.0, "cluster_id": None, "cluster_label": None,
                "reason": "empty content", "superseded": [], "latency_us": 0,
            }

        t0 = time.perf_counter()

        # --- Standard pipeline: embed, project, novelty, gate ---
        embedding = self._embedder.embed(content)
        proj = self._project(embedding)
        context = self._build_context()

        term = content[:80]
        pred_error = self._novelty.update(context, proj, term=term)
        surprise = self._compute_surprise(pred_error)
        novelty = float(1.0 / (1.0 + np.exp(-surprise)))

        continuity = self._tracker.compute(embedding)
        self._tracker.update(embedding)
        self._prev_embedding = embedding.copy()

        self._ingest_count += 1
        is_cold_start = self._ingest_count <= self._config.cold_start_count

        if is_cold_start:
            should_store = True
            quality_score = 1.0
            reason = f"cold start ({self._ingest_count}/{self._config.cold_start_count})"
        else:
            decision = self._gate.evaluate(novelty, continuity)
            should_store = decision.should_store
            quality_score = decision.quality_score
            reason = decision.explanation

        # If LLM says this is an update, boost through the gate
        is_update = payload.get("update", False)
        if is_update and not should_store and quality_score > 0.4:
            should_store = True
            reason = "LLM-flagged update (gate overridden)"

        # Topic assignment
        h1_cache = self._novelty.get_h1_cache()
        if len(h1_cache) >= 3:
            self._topics.extract(h1_cache)
        _, h1, _, _, _ = self._novelty.forward(context)
        cluster_id, cluster_label = self._topics.assign(h1)

        # --- Store or skip ---
        superseded_ids: list[str] = []
        if should_store:
            mem_id = self._store.store_memory(
                content=content,
                embedding=embedding,
                proj_embedding=proj,
                novelty_score=novelty,
                continuity_score=continuity,
                quality_score=quality_score,
                cluster_id=cluster_id,
                cluster_label=cluster_label,
                metadata=metadata,
            )
            if quality_score > 1.0:
                self._novelty.consolidate(min(1.0, quality_score - 0.7))

            # --- LLM-structured extraction (replaces regex) ---

            # Entities -> facts + entity_mentions
            for ent in payload.get("entities", []):
                name = ent.get("name", "").lower().strip()
                attr = ent.get("attribute", "").strip()
                value = ent.get("value", "").strip()
                if not name or not attr or not value:
                    continue

                # Store fact
                new_fact_id = self._store.store_fact(
                    entity=name, attribute=attr, value=value,
                    source_memory_id=mem_id,
                )
                # Supersede previous value if provided
                previous = ent.get("previous", "").strip()
                if previous:
                    existing = self._store.find_facts_by_entity_attribute(name, attr)
                    for old in existing:
                        if old["id"] != new_fact_id:
                            self._store.supersede_fact(old["id"], new_fact_id)

                # Store entity mention
                self._store.store_entity_mention(mem_id, name, "subject")

            # Relationships
            for rel in payload.get("relationships", []):
                a = rel.get("a", "").strip()
                relation = rel.get("relation", "").strip()
                b = rel.get("b", "").strip()
                if a and relation and b:
                    self._store.store_relationship(
                        a.lower(), relation.lower(), b.lower(),
                        source_memory_id=mem_id,
                    )
                    self._store.store_entity_mention(mem_id, a.lower(), "subject")
                    self._store.store_entity_mention(mem_id, b.lower(), "subject")

            # Temporal
            temporal_str = payload.get("temporal", "")
            if temporal_str:
                refs = self._temporal.extract_from_text(str(temporal_str))
                if refs and refs[0].timestamp:
                    self._store.update_event_timestamp(mem_id, refs[0].timestamp)

            # Update detection: supersede conflicting memories
            if is_update:
                conflicts = self._fact_extractor.find_conflicting_memories(
                    embedding, content, self._store
                )
                for c in conflicts:
                    if c["id"] != mem_id:
                        self._store.mark_superseded(c["id"], mem_id)
                        superseded_ids.append(c["id"])
        else:
            mem_id = self._store.log_skip(
                content=content,
                embedding=embedding,
                proj_embedding=proj,
                novelty_score=novelty,
                continuity_score=continuity,
                quality_score=quality_score,
                cluster_id=cluster_id,
                cluster_label=cluster_label,
                metadata=metadata,
            )

        self._store.log_decision(
            content=content,
            novelty_score=novelty,
            continuity_score=continuity,
            quality_score=quality_score,
            was_stored=should_store,
            reason=reason,
        )

        self._update_ring_buffer(proj)
        if self._ingest_count % 10 == 0:
            self._checkpoint()
        if self._ingest_count % self._config.consolidation_interval == 0:
            self.consolidate()

        latency_us = int((time.perf_counter() - t0) * 1_000_000)

        return {
            "stored": should_store,
            "quality": round(quality_score, 4),
            "novelty": round(novelty, 4),
            "continuity": round(continuity, 4),
            "cluster_id": cluster_id,
            "cluster_label": cluster_label,
            "reason": reason,
            "superseded": superseded_ids,
            "latency_us": latency_us,
        }

    def _detect_entities_in_query(self, query: str) -> list[str]:
        """Detect all known entity names in a query string."""
        known_entities = self._store.get_entity_list()
        if not known_entities:
            return []

        query_lower = query.lower()
        # Check longest entity names first to avoid partial matches
        entities_by_length = sorted(
            known_entities, key=lambda e: len(e["entity"]), reverse=True
        )
        found: list[str] = []
        for e in entities_by_length:
            ent = e["entity"]
            if ent in query_lower:
                found.append(ent)
        return found

    def search(self, query: str, top_k: int | None = None) -> list[dict]:
        """Search for relevant memories.

        Returns list of dicts with: content, relevance, topic, cluster_id, tier.
        """
        top_k = top_k or self._config.default_top_k
        embedding = self._embedder.embed(query)

        # Detect temporal + entity signals
        is_temporal, temporal_ref = self._temporal.detect_temporal_query(query)
        entity_hints = self._detect_entities_in_query(query)

        # Get h1 for topic-aware search
        proj = self._project(embedding)
        context = self._build_context()
        _, h1, _, _, _ = self._novelty.forward(context)

        results = self._retriever.search(
            query_embedding=embedding,
            h1_activations=h1,
            store=self._store,
            topic_model=self._topics,
            top_k=top_k,
            temporal_ref=temporal_ref,
            entity_hints=entity_hints or None,
        )

        # Clean results for public API
        return [
            {
                "content": r["content"],
                "relevance": round(r.get("relevance", 0), 4),
                "topic": r.get("cluster_label"),
                "cluster_id": r.get("cluster_id"),
                "tier": r.get("tier", "global"),
            }
            for r in results
        ]

    def context(self, query: str, max_tokens: int | None = None) -> dict:
        """Get assembled context for a query.

        Returns dict with: context, n_memories, n_clusters.
        """
        max_tokens = max_tokens or self._config.max_context_tokens
        results = self.search(query, top_k=self._config.default_top_k)

        context_str = self._retriever.assemble_context(results, max_tokens)

        cluster_ids = {r.get("cluster_id") for r in results if r.get("cluster_id") is not None}

        return {
            "context": context_str,
            "n_memories": len(results),
            "n_clusters": len(cluster_ids),
        }

    def status(self) -> dict:
        """Get engine status and statistics."""
        stats = self._store.get_stats()
        return {
            "n_memories": stats["total_stored"],
            "total_ingested": stats["total_ingested"],
            "filter_rate": round(stats["filter_rate"], 4),
            "topics": [
                {
                    "cluster_id": c.cluster_id,
                    "n_terms": len(c.terms),
                    "coherence": round(c.coherence, 4),
                }
                for c in self._topics.clusters
            ],
            "n_clusters": self._topics.n_clusters,
            "ingest_count": self._ingest_count,
            "stats": stats,
        }

    def consolidate(self) -> list[dict]:
        """Run extractive consolidation on all clusters.

        For each cluster with 2+ memories, extracts TF-IDF keywords and
        selects MMR-diverse representative memories. Saves cluster summaries.

        Returns:
            List of cluster summary dicts.
        """
        # Get all cluster IDs from stored memories
        all_memories = self._store.get_all_memories(stored_only=True)
        cluster_ids = {m["cluster_id"] for m in all_memories if m["cluster_id"] is not None}

        if not cluster_ids:
            return []

        # Collect all texts for corpus-level IDF
        all_texts = [m["content"] for m in all_memories]

        summaries = []
        for cid in cluster_ids:
            cluster_mems = self._store.get_cluster_memories(cid, stored_only=True)
            if len(cluster_mems) < 2:
                continue

            summary = self._consolidator.consolidate_cluster(
                memories=cluster_mems,
                cluster_id=cid,
                corpus_texts=all_texts,
            )
            if summary is not None:
                self._store.save_cluster_summary(
                    cluster_id=summary.cluster_id,
                    keywords=summary.keywords,
                    representative_ids=summary.representative_ids,
                    centroid_embedding=summary.centroid_embedding,
                    n_memories=summary.n_memories,
                )
                summaries.append({
                    "cluster_id": summary.cluster_id,
                    "keywords": summary.keywords,
                    "n_representatives": len(summary.representative_ids),
                    "n_memories": summary.n_memories,
                })

        return summaries

    def facts(self, entity: str | None = None) -> list[dict]:
        """Get stored facts, optionally filtered by entity.

        Returns:
            List of fact dicts with: entity, attribute, value, confidence.
        """
        return self._store.get_facts(entity=entity)

    def profile(self, entity: str) -> dict:
        """Get a structured profile for an entity.

        Returns:
            Dict with: entity, attributes, attribute_history, mention_count,
            first_seen, last_seen, related_memory_ids.
        """
        p = self._profile_builder.build_profile(entity)
        return {
            "entity": p.entity,
            "attributes": p.attributes,
            "attribute_history": p.attribute_history,
            "mention_count": p.mention_count,
            "first_seen": p.first_seen,
            "last_seen": p.last_seen,
            "related_memory_ids": p.related_memory_ids,
            "relationships": p.relationships,
        }

    def entities(self) -> list[dict]:
        """List all known entities with counts.

        Returns:
            List of dicts with: entity, mention_count, fact_count, last_seen.
        """
        return self._profile_builder.list_entities()

    def relationships(self, entity: str) -> list[dict]:
        """Get relationships for an entity.

        Returns:
            List of dicts with: entity, relation, direction.
        """
        raw = self._store.get_relationships(entity)
        entity_lower = entity.lower()
        result: list[dict] = []
        seen: set[tuple[str, str, str]] = set()
        for r in raw:
            if r["entity_a"] == entity_lower:
                other = r["entity_b"]
                direction = "outgoing"
            else:
                other = r["entity_a"]
                direction = "incoming"
            key = (other, r["relation"], direction)
            if key not in seen:
                seen.add(key)
                result.append({
                    "entity": other,
                    "relation": r["relation"],
                    "direction": direction,
                })
        return result

    def related_memories(self, entity_a: str, entity_b: str, top_k: int = 10) -> list[dict]:
        """Get memories where both entities are mentioned.

        Returns:
            List of dicts with: content, relevance, cluster_id, tier.
        """
        results = self._store.get_shared_memories(entity_a, entity_b, top_k=top_k)
        return [
            {
                "content": r["content"],
                "relevance": round(r.get("relevance", 0), 4),
                "cluster_id": r.get("cluster_id"),
                "tier": "co_presence",
            }
            for r in results
        ]

    def flush(self) -> None:
        """Force a checkpoint to persist current state."""
        self._checkpoint()

    def close(self) -> None:
        """Checkpoint and close the store."""
        self._checkpoint()
        self._store.close()

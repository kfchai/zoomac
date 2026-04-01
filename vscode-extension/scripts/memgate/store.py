"""MemoryStore — SQLite persistence with WAL mode."""

from __future__ import annotations

import io
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from memgate.config import MemGateConfig


def _np_to_blob(arr: np.ndarray) -> bytes:
    """Serialize numpy array to bytes."""
    buf = io.BytesIO()
    np.save(buf, arr)
    return buf.getvalue()


def _blob_to_np(blob: bytes) -> np.ndarray:
    """Deserialize numpy array from bytes."""
    buf = io.BytesIO(blob)
    return np.load(buf)


def _state_to_blob(state: dict) -> bytes:
    """Serialize a dict of numpy arrays / scalars to compressed bytes."""
    buf = io.BytesIO()
    np.savez_compressed(buf, **{k: np.asarray(v) for k, v in state.items()})
    return buf.getvalue()


def _blob_to_state(blob: bytes) -> dict:
    """Deserialize state dict from compressed bytes."""
    buf = io.BytesIO(blob)
    npz = np.load(buf, allow_pickle=True)
    return {k: npz[k] for k in npz.files}


@dataclass
class MemoryEntry:
    id: str
    content: str
    embedding: np.ndarray
    proj_embedding: np.ndarray
    novelty_score: float
    continuity_score: float
    quality_score: float
    was_stored: bool
    cluster_id: int | None
    cluster_label: str | None
    metadata: dict | None
    session_id: str | None
    created_at: float


class MemoryStore:
    """SQLite-backed memory persistence."""

    def __init__(self, db_path: str | Path, config: MemGateConfig | None = None) -> None:
        from memgate.config import MemGateConfig

        self._config = config or MemGateConfig()
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                embedding BLOB NOT NULL,
                proj_embedding BLOB NOT NULL,
                novelty_score REAL NOT NULL,
                continuity_score REAL NOT NULL,
                quality_score REAL NOT NULL,
                was_stored INTEGER NOT NULL DEFAULT 1,
                cluster_id INTEGER,
                cluster_label TEXT,
                metadata TEXT,
                session_id TEXT,
                created_at REAL NOT NULL,
                event_timestamp REAL
            );

            CREATE TABLE IF NOT EXISTS model_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                weights BLOB,
                running_stats BLOB,
                ring_buffer BLOB,
                ingest_count INTEGER DEFAULT 0,
                updated_at REAL
            );

            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_prefix TEXT,
                novelty_score REAL,
                continuity_score REAL,
                quality_score REAL,
                was_stored INTEGER,
                reason TEXT,
                created_at REAL
            );

            CREATE TABLE IF NOT EXISTS session_stats (
                session_id TEXT PRIMARY KEY,
                ingested INTEGER DEFAULT 0,
                stored INTEGER DEFAULT 0,
                skipped INTEGER DEFAULT 0,
                started_at REAL,
                updated_at REAL
            );

            CREATE INDEX IF NOT EXISTS idx_memories_cluster ON memories(cluster_id);
            CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
            CREATE INDEX IF NOT EXISTS idx_memories_stored ON memories(was_stored);

            CREATE TABLE IF NOT EXISTS cluster_summaries (
                cluster_id INTEGER PRIMARY KEY,
                keywords TEXT NOT NULL,
                representative_ids TEXT NOT NULL,
                centroid_embedding BLOB NOT NULL,
                n_memories INTEGER NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS facts (
                id TEXT PRIMARY KEY,
                entity TEXT NOT NULL,
                attribute TEXT NOT NULL,
                value TEXT NOT NULL,
                source_memory_id TEXT,
                confidence REAL NOT NULL DEFAULT 1.0,
                superseded_by TEXT,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_facts_entity ON facts(entity);
            CREATE INDEX IF NOT EXISTS idx_facts_entity_attr ON facts(entity, attribute);

            CREATE TABLE IF NOT EXISTS entity_mentions (
                memory_id TEXT NOT NULL,
                entity TEXT NOT NULL,
                role TEXT DEFAULT 'mention',
                created_at REAL NOT NULL,
                PRIMARY KEY (memory_id, entity)
            );

            CREATE INDEX IF NOT EXISTS idx_entity_mentions_entity ON entity_mentions(entity);
            CREATE INDEX IF NOT EXISTS idx_memories_event_ts ON memories(event_timestamp);

            CREATE TABLE IF NOT EXISTS relationships (
                id TEXT PRIMARY KEY,
                entity_a TEXT NOT NULL,
                relation TEXT NOT NULL,
                entity_b TEXT NOT NULL,
                source_memory_id TEXT,
                confidence REAL DEFAULT 1.0,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rel_entity_a ON relationships(entity_a);
            CREATE INDEX IF NOT EXISTS idx_rel_entity_b ON relationships(entity_b);
        """)
        self._conn.commit()
        self._migrate_superseded_column()
        self._migrate_event_timestamp()

    def _migrate_superseded_column(self) -> None:
        """Add superseded_by column to memories if it doesn't exist."""
        try:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN superseded_by TEXT"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists

    def _migrate_event_timestamp(self) -> None:
        """Add event_timestamp column to memories if it doesn't exist."""
        try:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN event_timestamp REAL"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists

    def store_memory(
        self,
        content: str,
        embedding: np.ndarray,
        proj_embedding: np.ndarray,
        novelty_score: float,
        continuity_score: float,
        quality_score: float,
        cluster_id: int | None = None,
        cluster_label: str | None = None,
        metadata: dict | None = None,
        session_id: str | None = None,
        event_timestamp: float | None = None,
    ) -> str:
        """Store a memory entry. Returns the memory ID."""
        mem_id = str(uuid.uuid4())
        now = time.time()
        self._conn.execute(
            """INSERT INTO memories
               (id, content, embedding, proj_embedding, novelty_score,
                continuity_score, quality_score, was_stored, cluster_id,
                cluster_label, metadata, session_id, created_at, event_timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)""",
            (
                mem_id,
                content,
                _np_to_blob(embedding),
                _np_to_blob(proj_embedding),
                novelty_score,
                continuity_score,
                quality_score,
                cluster_id,
                cluster_label,
                json.dumps(metadata) if metadata else None,
                session_id,
                now,
                event_timestamp,
            ),
        )
        self._conn.commit()
        return mem_id

    def log_skip(
        self,
        content: str,
        embedding: np.ndarray,
        proj_embedding: np.ndarray,
        novelty_score: float,
        continuity_score: float,
        quality_score: float,
        cluster_id: int | None = None,
        cluster_label: str | None = None,
        metadata: dict | None = None,
        session_id: str | None = None,
    ) -> str:
        """Log a skipped memory (was_stored=0). Returns the memory ID."""
        mem_id = str(uuid.uuid4())
        now = time.time()
        self._conn.execute(
            """INSERT INTO memories
               (id, content, embedding, proj_embedding, novelty_score,
                continuity_score, quality_score, was_stored, cluster_id,
                cluster_label, metadata, session_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)""",
            (
                mem_id,
                content,
                _np_to_blob(embedding),
                _np_to_blob(proj_embedding),
                novelty_score,
                continuity_score,
                quality_score,
                cluster_id,
                cluster_label,
                json.dumps(metadata) if metadata else None,
                session_id,
                now,
            ),
        )
        self._conn.commit()
        return mem_id

    def log_decision(
        self,
        content: str,
        novelty_score: float,
        continuity_score: float,
        quality_score: float,
        was_stored: bool,
        reason: str = "",
    ) -> None:
        """Log a store/skip decision."""
        self._conn.execute(
            """INSERT INTO decisions
               (content_prefix, novelty_score, continuity_score,
                quality_score, was_stored, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                content[:100],
                novelty_score,
                continuity_score,
                quality_score,
                1 if was_stored else 0,
                reason,
                time.time(),
            ),
        )
        self._conn.commit()

    def search_by_vector(
        self,
        query_embedding: np.ndarray,
        top_k: int = 10,
        stored_only: bool = True,
        exclude_superseded: bool = True,
    ) -> list[dict]:
        """Brute-force cosine similarity search over all stored memories."""
        conditions = []
        if stored_only:
            conditions.append("was_stored = 1")
        if exclude_superseded:
            conditions.append("superseded_by IS NULL")
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = self._conn.execute(
            f"SELECT id, content, embedding, proj_embedding, novelty_score, "
            f"continuity_score, quality_score, cluster_id, cluster_label, "
            f"metadata, created_at, event_timestamp FROM memories {where}"
        ).fetchall()

        if not rows:
            return []

        query_norm = query_embedding / (np.linalg.norm(query_embedding) + 1e-10)
        results = []

        for row in rows:
            emb = _blob_to_np(row[2])
            emb_norm = emb / (np.linalg.norm(emb) + 1e-10)
            similarity = float(np.dot(query_norm, emb_norm))
            results.append(
                {
                    "id": row[0],
                    "content": row[1],
                    "embedding": emb,
                    "proj_embedding": _blob_to_np(row[3]),
                    "novelty_score": row[4],
                    "continuity_score": row[5],
                    "quality_score": row[6],
                    "cluster_id": row[7],
                    "cluster_label": row[8],
                    "metadata": json.loads(row[9]) if row[9] else None,
                    "created_at": row[10],
                    "event_timestamp": row[11],
                    "relevance": similarity,
                }
            )

        results.sort(key=lambda x: x["relevance"], reverse=True)
        return results[:top_k]

    def search_by_cluster(
        self,
        cluster_id: int,
        query_embedding: np.ndarray,
        top_k: int = 10,
        exclude_superseded: bool = True,
    ) -> list[dict]:
        """Search within a specific cluster by cosine similarity."""
        where = "WHERE was_stored = 1 AND cluster_id = ?"
        if exclude_superseded:
            where += " AND superseded_by IS NULL"
        rows = self._conn.execute(
            "SELECT id, content, embedding, proj_embedding, novelty_score, "
            "continuity_score, quality_score, cluster_id, cluster_label, "
            f"metadata, created_at, event_timestamp FROM memories {where}",
            (cluster_id,),
        ).fetchall()

        if not rows:
            return []

        query_norm = query_embedding / (np.linalg.norm(query_embedding) + 1e-10)
        results = []

        for row in rows:
            emb = _blob_to_np(row[2])
            emb_norm = emb / (np.linalg.norm(emb) + 1e-10)
            similarity = float(np.dot(query_norm, emb_norm))
            results.append(
                {
                    "id": row[0],
                    "content": row[1],
                    "embedding": emb,
                    "proj_embedding": _blob_to_np(row[3]),
                    "novelty_score": row[4],
                    "continuity_score": row[5],
                    "quality_score": row[6],
                    "cluster_id": row[7],
                    "cluster_label": row[8],
                    "metadata": json.loads(row[9]) if row[9] else None,
                    "created_at": row[10],
                    "event_timestamp": row[11],
                    "relevance": similarity,
                }
            )

        results.sort(key=lambda x: x["relevance"], reverse=True)
        return results[:top_k]

    def get_all_memories(self, stored_only: bool = True) -> list[dict]:
        """Get all stored memories (without embeddings for efficiency)."""
        where = "WHERE was_stored = 1" if stored_only else ""
        rows = self._conn.execute(
            f"SELECT id, content, novelty_score, continuity_score, "
            f"quality_score, cluster_id, cluster_label, created_at "
            f"FROM memories {where} ORDER BY created_at DESC"
        ).fetchall()
        return [
            {
                "id": r[0],
                "content": r[1],
                "novelty_score": r[2],
                "continuity_score": r[3],
                "quality_score": r[4],
                "cluster_id": r[5],
                "cluster_label": r[6],
                "created_at": r[7],
            }
            for r in rows
        ]

    def save_model_state(
        self,
        weights: dict | None = None,
        running_stats: dict | None = None,
        ring_buffer: np.ndarray | None = None,
        ingest_count: int = 0,
    ) -> None:
        """Save or update model state (single row)."""
        now = time.time()
        self._conn.execute(
            """INSERT INTO model_state (id, weights, running_stats, ring_buffer, ingest_count, updated_at)
               VALUES (1, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 weights=excluded.weights,
                 running_stats=excluded.running_stats,
                 ring_buffer=excluded.ring_buffer,
                 ingest_count=excluded.ingest_count,
                 updated_at=excluded.updated_at""",
            (
                _state_to_blob(weights) if weights else None,
                _state_to_blob(running_stats) if running_stats else None,
                _np_to_blob(ring_buffer) if ring_buffer is not None else None,
                ingest_count,
                now,
            ),
        )
        self._conn.commit()

    def load_model_state(self) -> dict | None:
        """Load model state. Returns None if no state saved."""
        row = self._conn.execute(
            "SELECT weights, running_stats, ring_buffer, ingest_count FROM model_state WHERE id=1"
        ).fetchone()
        if row is None:
            return None
        return {
            "weights": _blob_to_state(row[0]) if row[0] else None,
            "running_stats": _blob_to_state(row[1]) if row[1] else None,
            "ring_buffer": _blob_to_np(row[2]) if row[2] else None,
            "ingest_count": row[3] or 0,
        }

    def get_stats(self) -> dict:
        """Get aggregate statistics."""
        total = self._conn.execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0]
        stored = self._conn.execute(
            "SELECT COUNT(*) FROM memories WHERE was_stored = 1"
        ).fetchone()[0]
        skipped = total - stored

        clusters = self._conn.execute(
            "SELECT cluster_id, cluster_label, COUNT(*) FROM memories "
            "WHERE was_stored = 1 AND cluster_id IS NOT NULL "
            "GROUP BY cluster_id, cluster_label ORDER BY COUNT(*) DESC"
        ).fetchall()

        return {
            "total_ingested": total,
            "total_stored": stored,
            "total_skipped": skipped,
            "filter_rate": skipped / total if total > 0 else 0.0,
            "clusters": [
                {"cluster_id": c[0], "label": c[1], "count": c[2]} for c in clusters
            ],
        }

    def get_memory_count(self, stored_only: bool = True) -> int:
        where = "WHERE was_stored = 1" if stored_only else ""
        return self._conn.execute(
            f"SELECT COUNT(*) FROM memories {where}"
        ).fetchone()[0]

    # --- Supersession ---

    def mark_superseded(self, old_memory_id: str, new_memory_id: str) -> None:
        """Mark an old memory as superseded by a newer one."""
        self._conn.execute(
            "UPDATE memories SET superseded_by = ? WHERE id = ?",
            (new_memory_id, old_memory_id),
        )
        self._conn.commit()

    # --- Cluster summaries ---

    def get_cluster_memories(
        self, cluster_id: int, stored_only: bool = True
    ) -> list[dict]:
        """Get all memories in a cluster with embeddings."""
        where = "WHERE cluster_id = ?"
        if stored_only:
            where += " AND was_stored = 1"
        rows = self._conn.execute(
            "SELECT id, content, embedding, created_at FROM memories " + where,
            (cluster_id,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "content": r[1],
                "embedding": _blob_to_np(r[2]),
                "created_at": r[3],
            }
            for r in rows
        ]

    def save_cluster_summary(
        self,
        cluster_id: int,
        keywords: list[str],
        representative_ids: list[str],
        centroid_embedding: np.ndarray,
        n_memories: int,
    ) -> None:
        """Save or update a cluster summary."""
        now = time.time()
        self._conn.execute(
            """INSERT INTO cluster_summaries
               (cluster_id, keywords, representative_ids, centroid_embedding, n_memories, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(cluster_id) DO UPDATE SET
                 keywords=excluded.keywords,
                 representative_ids=excluded.representative_ids,
                 centroid_embedding=excluded.centroid_embedding,
                 n_memories=excluded.n_memories,
                 updated_at=excluded.updated_at""",
            (
                cluster_id,
                json.dumps(keywords),
                json.dumps(representative_ids),
                _np_to_blob(centroid_embedding),
                n_memories,
                now,
            ),
        )
        self._conn.commit()

    def get_cluster_summaries(self) -> list[dict]:
        """Get all cluster summaries."""
        rows = self._conn.execute(
            "SELECT cluster_id, keywords, representative_ids, "
            "centroid_embedding, n_memories, updated_at "
            "FROM cluster_summaries"
        ).fetchall()
        return [
            {
                "cluster_id": r[0],
                "keywords": json.loads(r[1]),
                "representative_ids": json.loads(r[2]),
                "centroid_embedding": _blob_to_np(r[3]),
                "n_memories": r[4],
                "updated_at": r[5],
            }
            for r in rows
        ]

    # --- Facts ---

    def store_fact(
        self,
        entity: str,
        attribute: str,
        value: str,
        source_memory_id: str | None = None,
        confidence: float = 1.0,
    ) -> str:
        """Store a fact triple. Returns fact ID."""
        fact_id = str(uuid.uuid4())
        now = time.time()
        self._conn.execute(
            """INSERT INTO facts
               (id, entity, attribute, value, source_memory_id, confidence, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (fact_id, entity, attribute, value, source_memory_id, confidence, now),
        )
        self._conn.commit()
        return fact_id

    def get_facts(
        self, entity: str | None = None, include_superseded: bool = False
    ) -> list[dict]:
        """Get facts, optionally filtered by entity."""
        conditions = []
        params: list[Any] = []
        if entity is not None:
            conditions.append("entity = ?")
            params.append(entity)
        if not include_superseded:
            conditions.append("superseded_by IS NULL")
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = self._conn.execute(
            f"SELECT id, entity, attribute, value, source_memory_id, "
            f"confidence, superseded_by, created_at FROM facts {where} "
            f"ORDER BY created_at DESC",
            params,
        ).fetchall()
        return [
            {
                "id": r[0],
                "entity": r[1],
                "attribute": r[2],
                "value": r[3],
                "source_memory_id": r[4],
                "confidence": r[5],
                "superseded_by": r[6],
                "created_at": r[7],
            }
            for r in rows
        ]

    def supersede_fact(self, old_fact_id: str, new_fact_id: str) -> None:
        """Mark an old fact as superseded by a newer one."""
        self._conn.execute(
            "UPDATE facts SET superseded_by = ? WHERE id = ?",
            (new_fact_id, old_fact_id),
        )
        self._conn.commit()

    def find_facts_by_entity_attribute(
        self, entity: str, attribute: str
    ) -> list[dict]:
        """Find active facts for a given (entity, attribute) pair."""
        rows = self._conn.execute(
            "SELECT id, entity, attribute, value, source_memory_id, "
            "confidence, created_at FROM facts "
            "WHERE entity = ? AND attribute = ? AND superseded_by IS NULL "
            "ORDER BY created_at DESC",
            (entity, attribute),
        ).fetchall()
        return [
            {
                "id": r[0],
                "entity": r[1],
                "attribute": r[2],
                "value": r[3],
                "source_memory_id": r[4],
                "confidence": r[5],
                "created_at": r[6],
            }
            for r in rows
        ]

    # --- Event timestamps ---

    def update_event_timestamp(self, memory_id: str, timestamp: float) -> None:
        """Set event_timestamp on a memory after extraction."""
        self._conn.execute(
            "UPDATE memories SET event_timestamp = ? WHERE id = ?",
            (timestamp, memory_id),
        )
        self._conn.commit()

    # --- Entity mentions ---

    def store_entity_mention(
        self, memory_id: str, entity: str, role: str = "mention"
    ) -> None:
        """Store an entity mention linked to a memory."""
        now = time.time()
        self._conn.execute(
            """INSERT OR IGNORE INTO entity_mentions
               (memory_id, entity, role, created_at)
               VALUES (?, ?, ?, ?)""",
            (memory_id, entity.lower(), role, now),
        )
        self._conn.commit()

    def get_memories_by_entity(
        self,
        entity: str,
        query_embedding: np.ndarray | None = None,
        top_k: int = 10,
    ) -> list[dict]:
        """Get memories linked to an entity, optionally ranked by cosine similarity."""
        rows = self._conn.execute(
            "SELECT m.id, m.content, m.embedding, m.proj_embedding, m.novelty_score, "
            "m.continuity_score, m.quality_score, m.cluster_id, m.cluster_label, "
            "m.metadata, m.created_at, m.event_timestamp "
            "FROM memories m "
            "INNER JOIN entity_mentions em ON m.id = em.memory_id "
            "WHERE em.entity = ? AND m.was_stored = 1 AND m.superseded_by IS NULL",
            (entity.lower(),),
        ).fetchall()

        if not rows:
            return []

        results = []
        query_norm = None
        if query_embedding is not None:
            query_norm = query_embedding / (np.linalg.norm(query_embedding) + 1e-10)

        for row in rows:
            emb = _blob_to_np(row[2])
            similarity = 0.5  # default if no query
            if query_norm is not None:
                emb_norm = emb / (np.linalg.norm(emb) + 1e-10)
                similarity = float(np.dot(query_norm, emb_norm))
            results.append(
                {
                    "id": row[0],
                    "content": row[1],
                    "embedding": emb,
                    "proj_embedding": _blob_to_np(row[3]),
                    "novelty_score": row[4],
                    "continuity_score": row[5],
                    "quality_score": row[6],
                    "cluster_id": row[7],
                    "cluster_label": row[8],
                    "metadata": json.loads(row[9]) if row[9] else None,
                    "created_at": row[10],
                    "event_timestamp": row[11],
                    "relevance": similarity,
                }
            )

        results.sort(key=lambda x: x["relevance"], reverse=True)
        return results[:top_k]

    def get_entity_list(self) -> list[dict]:
        """Get list of known entities with counts."""
        rows = self._conn.execute(
            "SELECT em.entity, COUNT(*) as cnt, MAX(em.created_at) as last_seen "
            "FROM entity_mentions em "
            "INNER JOIN memories m ON em.memory_id = m.id "
            "WHERE m.was_stored = 1 AND m.superseded_by IS NULL "
            "GROUP BY em.entity ORDER BY cnt DESC"
        ).fetchall()
        return [
            {"entity": r[0], "mention_count": r[1], "last_seen": r[2]}
            for r in rows
        ]

    def search_by_time_range(
        self,
        query_embedding: np.ndarray,
        start_ts: float,
        end_ts: float,
        top_k: int = 10,
    ) -> list[dict]:
        """Search memories within a time range, ranked by cosine similarity."""
        rows = self._conn.execute(
            "SELECT id, content, embedding, proj_embedding, novelty_score, "
            "continuity_score, quality_score, cluster_id, cluster_label, "
            "metadata, created_at, event_timestamp FROM memories "
            "WHERE was_stored = 1 AND superseded_by IS NULL "
            "AND event_timestamp BETWEEN ? AND ?",
            (start_ts, end_ts),
        ).fetchall()

        if not rows:
            return []

        query_norm = query_embedding / (np.linalg.norm(query_embedding) + 1e-10)
        results = []

        for row in rows:
            emb = _blob_to_np(row[2])
            emb_norm = emb / (np.linalg.norm(emb) + 1e-10)
            similarity = float(np.dot(query_norm, emb_norm))
            results.append(
                {
                    "id": row[0],
                    "content": row[1],
                    "embedding": emb,
                    "proj_embedding": _blob_to_np(row[3]),
                    "novelty_score": row[4],
                    "continuity_score": row[5],
                    "quality_score": row[6],
                    "cluster_id": row[7],
                    "cluster_label": row[8],
                    "metadata": json.loads(row[9]) if row[9] else None,
                    "created_at": row[10],
                    "event_timestamp": row[11],
                    "relevance": similarity,
                }
            )

        results.sort(key=lambda x: x["relevance"], reverse=True)
        return results[:top_k]

    # --- Relationships ---

    def store_relationship(
        self,
        entity_a: str,
        relation: str,
        entity_b: str,
        source_memory_id: str | None = None,
        confidence: float = 1.0,
    ) -> str:
        """Store a relationship. Deduplicates by (entity_a, relation, entity_b)."""
        entity_a = entity_a.lower()
        entity_b = entity_b.lower()
        # Dedup: skip if already exists
        existing = self._conn.execute(
            "SELECT id FROM relationships WHERE entity_a = ? AND relation = ? AND entity_b = ?",
            (entity_a, relation, entity_b),
        ).fetchone()
        if existing:
            return existing[0]

        rel_id = str(uuid.uuid4())
        now = time.time()
        self._conn.execute(
            """INSERT INTO relationships
               (id, entity_a, relation, entity_b, source_memory_id, confidence, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (rel_id, entity_a, relation, entity_b, source_memory_id, confidence, now),
        )
        self._conn.commit()
        return rel_id

    def get_relationships(self, entity: str) -> list[dict]:
        """Get all relationships where entity is either entity_a or entity_b."""
        entity = entity.lower()
        rows = self._conn.execute(
            "SELECT id, entity_a, relation, entity_b, source_memory_id, confidence, created_at "
            "FROM relationships WHERE entity_a = ? OR entity_b = ? "
            "ORDER BY created_at DESC",
            (entity, entity),
        ).fetchall()
        return [
            {
                "id": r[0],
                "entity_a": r[1],
                "relation": r[2],
                "entity_b": r[3],
                "source_memory_id": r[4],
                "confidence": r[5],
                "created_at": r[6],
            }
            for r in rows
        ]

    def get_related_entities(self, entity: str) -> list[dict]:
        """Get unique connected entities with their relation types."""
        entity = entity.lower()
        rows = self._conn.execute(
            "SELECT entity_a, relation, entity_b FROM relationships "
            "WHERE entity_a = ? OR entity_b = ?",
            (entity, entity),
        ).fetchall()

        # Aggregate: group by related entity
        related: dict[str, list[str]] = {}
        for row in rows:
            if row[0] == entity:
                other = row[2]
            else:
                other = row[0]
            related.setdefault(other, [])
            if row[1] not in related[other]:
                related[other].append(row[1])

        return [
            {"entity": ent, "relations": rels}
            for ent, rels in sorted(related.items())
        ]

    def get_shared_memories(
        self,
        entity_a: str,
        entity_b: str,
        query_embedding: np.ndarray | None = None,
        top_k: int = 10,
    ) -> list[dict]:
        """Get memories where both entities are mentioned."""
        entity_a = entity_a.lower()
        entity_b = entity_b.lower()
        rows = self._conn.execute(
            "SELECT m.id, m.content, m.embedding, m.proj_embedding, m.novelty_score, "
            "m.continuity_score, m.quality_score, m.cluster_id, m.cluster_label, "
            "m.metadata, m.created_at, m.event_timestamp "
            "FROM memories m "
            "INNER JOIN entity_mentions em1 ON m.id = em1.memory_id "
            "INNER JOIN entity_mentions em2 ON m.id = em2.memory_id "
            "WHERE em1.entity = ? AND em2.entity = ? "
            "AND m.was_stored = 1 AND m.superseded_by IS NULL",
            (entity_a, entity_b),
        ).fetchall()

        if not rows:
            return []

        query_norm = None
        if query_embedding is not None:
            query_norm = query_embedding / (np.linalg.norm(query_embedding) + 1e-10)

        results = []
        for row in rows:
            emb = _blob_to_np(row[2])
            similarity = 0.5
            if query_norm is not None:
                emb_norm = emb / (np.linalg.norm(emb) + 1e-10)
                similarity = float(np.dot(query_norm, emb_norm))
            results.append(
                {
                    "id": row[0],
                    "content": row[1],
                    "embedding": emb,
                    "proj_embedding": _blob_to_np(row[3]),
                    "novelty_score": row[4],
                    "continuity_score": row[5],
                    "quality_score": row[6],
                    "cluster_id": row[7],
                    "cluster_label": row[8],
                    "metadata": json.loads(row[9]) if row[9] else None,
                    "created_at": row[10],
                    "event_timestamp": row[11],
                    "relevance": similarity,
                }
            )

        results.sort(key=lambda x: x["relevance"], reverse=True)
        return results[:top_k]

    def close(self) -> None:
        self._conn.close()

"""ProfileBuilder — on-the-fly entity profile aggregation from facts + entity_mentions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memgate.store import MemoryStore


@dataclass
class EntityProfile:
    """Structured profile for an entity, aggregated from facts and mentions."""

    entity: str
    attributes: dict[str, str] = field(default_factory=dict)  # attr -> latest value
    attribute_history: dict[str, list[dict]] = field(default_factory=dict)  # attr -> [{value, created_at}]
    mention_count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    related_memory_ids: list[str] = field(default_factory=list)
    relationships: list[dict] = field(default_factory=list)  # [{entity, relation, direction}]


class ProfileBuilder:
    """Builds entity profiles on-the-fly from facts and entity_mentions."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def build_profile(self, entity: str) -> EntityProfile:
        """Build a structured profile for the given entity.

        Aggregates facts (current + history) and entity_mentions.
        """
        entity_lower = entity.lower()

        # Get all facts for this entity (including superseded for history)
        all_facts = self._store.get_facts(entity=entity_lower, include_superseded=True)
        active_facts = self._store.get_facts(entity=entity_lower, include_superseded=False)

        # Build attributes (latest value) and history
        attributes: dict[str, str] = {}
        attribute_history: dict[str, list[dict]] = {}

        # Active facts -> current attributes
        seen_attrs: set[str] = set()
        for f in active_facts:
            attr = f["attribute"]
            if attr not in seen_attrs:
                attributes[attr] = f["value"]
                seen_attrs.add(attr)

        # All facts -> history grouped by attribute
        for f in all_facts:
            attr = f["attribute"]
            if attr not in attribute_history:
                attribute_history[attr] = []
            attribute_history[attr].append({
                "value": f["value"],
                "created_at": f["created_at"],
                "superseded": f.get("superseded_by") is not None,
            })

        # Sort history by created_at descending (newest first)
        for attr in attribute_history:
            attribute_history[attr].sort(key=lambda x: x["created_at"], reverse=True)

        # Get entity mentions for memory linkage
        memories = self._store.get_memories_by_entity(entity_lower)
        memory_ids = [m["id"] for m in memories]

        first_seen = 0.0
        last_seen = 0.0
        if memories:
            timestamps = [m["created_at"] for m in memories]
            first_seen = min(timestamps)
            last_seen = max(timestamps)

        # Get relationships
        raw_rels = self._store.get_relationships(entity_lower)
        rel_list: list[dict] = []
        seen_rels: set[tuple[str, str, str]] = set()
        for r in raw_rels:
            if r["entity_a"] == entity_lower:
                other = r["entity_b"]
                direction = "outgoing"
            else:
                other = r["entity_a"]
                direction = "incoming"
            key = (other, r["relation"], direction)
            if key not in seen_rels:
                seen_rels.add(key)
                rel_list.append({
                    "entity": other,
                    "relation": r["relation"],
                    "direction": direction,
                })

        return EntityProfile(
            entity=entity_lower,
            attributes=attributes,
            attribute_history=attribute_history,
            mention_count=len(memories),
            first_seen=first_seen,
            last_seen=last_seen,
            related_memory_ids=memory_ids,
            relationships=rel_list,
        )

    def list_entities(self) -> list[dict]:
        """List all known entities with counts and last-seen timestamps.

        Also includes fact counts from the facts table.
        """
        # Get entity mentions list
        mention_entities = self._store.get_entity_list()

        # Get fact-based entities
        all_facts = self._store.get_facts(include_superseded=False)
        fact_counts: dict[str, int] = {}
        for f in all_facts:
            ent = f["entity"]
            fact_counts[ent] = fact_counts.get(ent, 0) + 1

        # Merge: entity_mentions are primary, supplement with facts
        result_map: dict[str, dict] = {}
        for em in mention_entities:
            ent = em["entity"]
            result_map[ent] = {
                "entity": ent,
                "mention_count": em["mention_count"],
                "fact_count": fact_counts.pop(ent, 0),
                "last_seen": em["last_seen"],
            }

        # Add entities that only appear in facts (no mentions)
        for ent, count in fact_counts.items():
            if ent not in result_map:
                result_map[ent] = {
                    "entity": ent,
                    "mention_count": 0,
                    "fact_count": count,
                    "last_seen": 0.0,
                }

        # Sort by total activity (mentions + facts)
        result = list(result_map.values())
        result.sort(
            key=lambda x: x["mention_count"] + x["fact_count"], reverse=True
        )
        return result

"""FactExtractor — update detection, conflict finding, and entity-fact extraction."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from memgate.store import MemoryStore

# Regex patterns for update/change language
_UPDATE_PATTERNS: list[tuple[re.Pattern, float]] = [
    (re.compile(r"\b(moved to|relocated to|living in now)\b", re.I), 0.9),
    (re.compile(r"\b(no longer|not anymore|stopped|quit)\b", re.I), 0.8),
    (re.compile(r"\b(changed to|switched to|converted to)\b", re.I), 0.9),
    (re.compile(r"\b(now I|I now|I've started|I started)\b", re.I), 0.7),
    (re.compile(r"\b(used to|previously|formerly|in the past)\b", re.I), 0.6),
    (re.compile(r"\b(actually|correction|update|instead)\b", re.I), 0.5),
    (re.compile(r"\b(replaced|upgraded|downgraded)\b", re.I), 0.7),
    (re.compile(r"\b(married|divorced|engaged|separated)\b", re.I), 0.6),
    (re.compile(r"\b(promoted to|got a new job|new role)\b", re.I), 0.7),
    (re.compile(r"\b(broke up|got together|dating)\b", re.I), 0.6),
]

# Entity extraction patterns: captures the entity reference
_ENTITY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(I|my|me|mine|myself)\b", re.I), "user"),
    (re.compile(r"\b(we|our|us|ourselves)\b", re.I), "user"),
]

# Speaker pattern from formatted text: "[Session N, date] Name:" -> name
_SPEAKER_RE = re.compile(r"\[Session\s+\d+.*?\]\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*):")

# "My friend/sister/boss Name" -> name
_RELATION_NAME_RE = re.compile(
    r"\b(?:[Mm]y\s+)?(?:[Ff]riend|[Ss]ister|[Bb]rother|[Bb]oss|[Cc]olleague|[Pp]artner|[Ww]ife|[Hh]usband|"
    r"[Mm]other|[Ff]ather|[Mm]om|[Dd]ad|[Aa]unt|[Uu]ncle|[Cc]ousin|[Nn]eighbor|[Rr]oommate|[Cc]oworker)\s+"
    r"([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]+)*)",
)

# Relationship extraction: captures BOTH the relation word AND the name
# "my friend Alice" -> (user, friend, alice)
_MY_RELATION_RE = re.compile(
    r"\b[Mm]y\s+(friend|sister|brother|boss|colleague|partner|wife|husband|"
    r"mother|father|mom|dad|aunt|uncle|cousin|neighbor|roommate|coworker)\s+"
    r"([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]+)*)",
)

# "Alice is Bob's sister/friend/etc." -> (bob, sister, alice)
_IS_POSSESSIVE_REL_RE = re.compile(
    r"([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]+)*)\s+is\s+"
    r"([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]+)*)'s\s+"
    r"(friend|sister|brother|boss|colleague|partner|wife|husband|"
    r"mother|father|mom|dad|aunt|uncle|cousin|neighbor|roommate|coworker)",
    re.I,
)

# "Alice is my friend" -> (user, friend, alice)
_IS_MY_REL_RE = re.compile(
    r"([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]+)*)\s+is\s+my\s+"
    r"(friend|sister|brother|boss|colleague|partner|wife|husband|"
    r"mother|father|mom|dad|aunt|uncle|cousin|neighbor|roommate|coworker)",
    re.I,
)

# Co-conjunction: "Alice and Bob" (two capitalized names joined by "and")
_CO_CONJUNCTION_RE = re.compile(
    r"([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]+)*)\s+and\s+"
    r"([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]+)*)",
)

# Interaction verbs: "met with/talked to/went with Name"
_INTERACTION_RE = re.compile(
    r"\b(?:met|talked|went|hung out|traveled|worked|studied|played|dined|"
    r"chatted|visited|stayed|shopped|walked|ran|hiked|cooked|ate|drank)\s+"
    r"(?:with|to)\s+([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]+)*)",
)

# Canonical relation names (normalize mom->mother, dad->father)
_RELATION_CANONICAL: dict[str, str] = {
    "mom": "mother",
    "dad": "father",
}

# Valid relation types for output
_VALID_RELATIONS = frozenset({
    "friend", "sister", "brother", "boss", "colleague", "partner",
    "wife", "husband", "mother", "father", "cousin", "neighbor",
    "roommate", "coworker", "aunt", "uncle", "co_mentioned", "interaction",
})

# Sentence-initial capitalized name followed by verb
_NAME_VERB_RE = re.compile(
    r"(?:^|[.!?]\s+)([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]+)*)\s+(?:said|told|asked|went|is|was|has|had|does|did|likes|loves|hates|wants|thinks|believes|knows|works|lives|moved|started|stopped|got|came|left|bought|sold|made|gave|took)",
)

# Stop words that look like names at sentence start but aren't
_NAME_STOPWORDS = {
    "the", "this", "that", "these", "those", "what", "when", "where", "which",
    "who", "how", "why", "yes", "yeah", "sure", "okay", "well", "but", "and",
    "also", "just", "really", "very", "actually", "maybe", "probably",
    "today", "tomorrow", "yesterday", "everyone", "someone", "anyone",
    "nothing", "something", "everything", "here", "there",
}

# Attribute templates for matching — (pattern, attribute_name)
_ATTRIBUTE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:live[sd]? in|moved to|relocated to|living in)\s+(.+?)(?:\.|,|$)", re.I), "location"),
    (re.compile(r"\b(?:work[s]? at|works? for|employed at|job at)\s+(.+?)(?:\.|,|$)", re.I), "workplace"),
    (re.compile(r"\b(?:name is|called|go by)\s+(.+?)(?:\.|,|$)", re.I), "name"),
    (re.compile(r"\b(?:favorite (?:food|meal) is|love eating|prefer eating)\s+(.+?)(?:\.|,|$)", re.I), "favorite_food"),
    (re.compile(r"\b(?:favorite (?:color|colour) is)\s+(.+?)(?:\.|,|$)", re.I), "favorite_color"),
    (re.compile(r"\b(?:born in|birthday is|born on)\s+(.+?)(?:\.|,|$)", re.I), "birthday"),
    (re.compile(r"\b(?:married to|spouse is|partner is|wife is|husband is)\s+(.+?)(?:\.|,|$)", re.I), "spouse"),
    (re.compile(r"\b(?:studying|major in|majoring in|degree in)\s+(.+?)(?:\.|,|$)", re.I), "education"),
    (re.compile(r"\b(?:hobby is|hobbies are|enjoy|passionate about)\s+(.+?)(?:\.|,|$)", re.I), "hobby"),
    (re.compile(r"\b(?:pet is|have a pet|own a)\s+(.+?)(?:\.|,|$)", re.I), "pet"),
    (re.compile(r"\b(?:allergic to)\s+(.+?)(?:\.|,|$)", re.I), "allergy"),
    (re.compile(r"\b(?:speak[s]?|fluent in)\s+(.+?)(?:\.|,|$)", re.I), "language"),
]


class FactExtractor:
    """Extracts update signals, conflicting memories, and structured facts.

    All extraction is regex + embedding based — no LLM calls.
    """

    def __init__(self, similarity_threshold: float = 0.85) -> None:
        self._similarity_threshold = similarity_threshold

    def detect_update_signal(self, text: str) -> float:
        """Detect whether text contains update/change language.

        Returns a signal strength in [0, 1]. Higher = more likely an update.
        """
        max_signal = 0.0
        for pattern, weight in _UPDATE_PATTERNS:
            if pattern.search(text):
                max_signal = max(max_signal, weight)
        return max_signal

    def find_conflicting_memories(
        self,
        embedding: np.ndarray,
        text: str,
        store: MemoryStore,
        top_k: int = 5,
    ) -> list[dict]:
        """Find existing memories that may conflict with new content.

        Requires BOTH:
        1. Update language in the new text (lexical signal)
        2. High cosine similarity with existing memory (semantic signal)

        Returns list of potentially conflicting memories.
        """
        update_signal = self.detect_update_signal(text)
        if update_signal < 0.3:
            return []

        # Search for similar memories
        candidates = store.search_by_vector(
            embedding, top_k=top_k, exclude_superseded=True
        )

        conflicts = []
        for c in candidates:
            if c["relevance"] >= self._similarity_threshold:
                # Don't flag a memory as conflicting with itself
                if c["content"].strip() == text.strip():
                    continue
                conflicts.append(c)

        return conflicts

    def extract_entities(self, text: str, min_name_length: int = 3) -> list[tuple[str, str]]:
        """Extract named entities from text.

        Returns list of (entity_name, role) where role is "speaker", "subject", or "mention".
        Entity names are lowercased for consistency.
        """
        entities: list[tuple[str, str]] = []
        seen: set[str] = set()

        # 1. Speaker from formatted text: "[Session N, date] Caroline:" -> "caroline"
        for m in _SPEAKER_RE.finditer(text):
            name = m.group(1).strip().lower()
            if name not in seen and len(name) >= min_name_length:
                entities.append((name, "speaker"))
                seen.add(name)

        # 2. Relation + name: "my friend Alice" -> "alice"
        for m in _RELATION_NAME_RE.finditer(text):
            name = m.group(1).strip().lower()
            if name not in seen and len(name) >= min_name_length and name not in _NAME_STOPWORDS:
                entities.append((name, "subject"))
                seen.add(name)

        # 3. Sentence-initial name + verb: "Alice said..." -> "alice"
        for m in _NAME_VERB_RE.finditer(text):
            name = m.group(1).strip().lower()
            if name not in seen and len(name) >= min_name_length and name not in _NAME_STOPWORDS:
                entities.append((name, "subject"))
                seen.add(name)

        # 4. Standalone capitalized words (not at text start, not stopwords)
        words = text.split()
        for i, w in enumerate(words):
            # Skip first word and words inside brackets
            if i == 0:
                continue
            clean = w.strip(".,;:!?()[]\"'")
            if (
                clean
                and clean[0].isupper()
                and clean.isalpha()
                and len(clean) >= min_name_length
                and clean.lower() not in seen
                and clean.lower() not in _NAME_STOPWORDS
                # Skip all-caps (likely acronyms)
                and not clean.isupper()
            ):
                entities.append((clean.lower(), "mention"))
                seen.add(clean.lower())

        return entities

    def extract_relationships(self, text: str, min_name_length: int = 3) -> list[dict]:
        """Extract inter-entity relationships from text.

        Returns list of:
            {entity_a: str, relation: str, entity_b: str, evidence: str}
        """
        relationships: list[dict] = []
        seen: set[tuple[str, str, str]] = set()

        def _canon(rel: str) -> str:
            return _RELATION_CANONICAL.get(rel.lower(), rel.lower())

        def _add(a: str, rel: str, b: str, evidence: str) -> None:
            a_l, b_l = a.lower(), b.lower()
            rel_c = _canon(rel)
            if (
                len(a_l) >= min_name_length
                and len(b_l) >= min_name_length
                and a_l != b_l
                and a_l not in _NAME_STOPWORDS
                and b_l not in _NAME_STOPWORDS
                and rel_c in _VALID_RELATIONS
                and (a_l, rel_c, b_l) not in seen
            ):
                seen.add((a_l, rel_c, b_l))
                relationships.append({
                    "entity_a": a_l,
                    "relation": rel_c,
                    "entity_b": b_l,
                    "evidence": evidence[:200],
                })

        # Determine the speaker (entity_a for "my X" patterns)
        speaker = "user"
        speaker_m = _SPEAKER_RE.search(text)
        if speaker_m:
            speaker = speaker_m.group(1).strip().lower()

        # 1. "my friend/sister/etc. Name" -> (speaker, relation, name)
        for m in _MY_RELATION_RE.finditer(text):
            rel = m.group(1)
            name = m.group(2).strip()
            _add(speaker, rel, name, m.group(0))

        # 2. "Alice is Bob's sister" -> (bob, sister, alice)
        for m in _IS_POSSESSIVE_REL_RE.finditer(text):
            subject = m.group(1).strip()
            possessor = m.group(2).strip()
            rel = m.group(3)
            _add(possessor, rel, subject, m.group(0))

        # 3. "Alice is my friend" -> (speaker, friend, alice)
        for m in _IS_MY_REL_RE.finditer(text):
            name = m.group(1).strip()
            rel = m.group(2)
            _add(speaker, rel, name, m.group(0))

        # 4. Interaction: "met with Alice" -> (speaker, interaction, alice)
        for m in _INTERACTION_RE.finditer(text):
            name = m.group(1).strip()
            _add(speaker, "interaction", name, m.group(0))

        # 5. Co-conjunction: "Alice and Bob" -> (alice, co_mentioned, bob)
        for m in _CO_CONJUNCTION_RE.finditer(text):
            name_a = m.group(1).strip()
            name_b = m.group(2).strip()
            _add(name_a, "co_mentioned", name_b, m.group(0))

        return relationships

    def extract_facts(self, text: str) -> list[dict]:
        """Extract (entity, attribute, value) triples from text.

        Uses regex patterns. Returns list of fact dicts.
        Fragile by design — supplements, doesn't replace, raw memory retrieval.
        """
        facts = []

        # Determine entity — try named entity patterns first
        entity = None

        # Check for speaker entity
        speaker_m = _SPEAKER_RE.search(text)
        if speaker_m:
            entity = speaker_m.group(1).strip().lower()

        # Try relation + name pattern (before first-person, since "my friend X"
        # means the entity is X, not "user")
        if entity is None:
            rel_m = _RELATION_NAME_RE.search(text)
            if rel_m:
                name = rel_m.group(1).strip().lower()
                if name not in _NAME_STOPWORDS:
                    entity = name

        # Try name + verb pattern
        if entity is None:
            nv_m = _NAME_VERB_RE.search(text)
            if nv_m:
                name = nv_m.group(1).strip().lower()
                if name not in _NAME_STOPWORDS:
                    entity = name

        # Check for first-person
        if entity is None:
            for pattern, ent_name in _ENTITY_PATTERNS:
                if pattern.search(text):
                    entity = ent_name
                    break

        # Fallback: proper nouns (capitalized words not at start)
        if entity is None:
            words = text.split()
            for i, w in enumerate(words):
                if i > 0 and w[0:1].isupper() and w.isalpha() and len(w) > 1:
                    if w.lower() not in _NAME_STOPWORDS:
                        entity = w.lower()
                        break

        if entity is None:
            entity = "unknown"

        # Extract attributes
        for pattern, attr_name in _ATTRIBUTE_PATTERNS:
            match = pattern.search(text)
            if match:
                value = match.group(1).strip().rstrip(".")
                if value:
                    facts.append({
                        "entity": entity,
                        "attribute": attr_name,
                        "value": value,
                    })

        return facts

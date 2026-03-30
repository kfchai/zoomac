"""MemGate integration wrapper for Zoomac."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from memgate import MemGate


class MemoryManager:
    """Wraps MemGate for use in the Zoomac core loop."""

    def __init__(self, project_dir: str | Path, max_tokens: int = 2000, top_k: int = 10) -> None:
        self._project_dir = str(project_dir)
        self._max_tokens = max_tokens
        self._top_k = top_k
        self._mg: MemGate | None = None

    @property
    def mg(self) -> MemGate:
        if self._mg is None:
            self._mg = MemGate(project_dir=self._project_dir)
        return self._mg

    def retrieve_context(self, query: str) -> str:
        """Retrieve relevant memories as formatted context for prompt injection."""
        result = self.mg.context(query, max_tokens=self._max_tokens)
        return result.get("context", "")

    def search(self, query: str, top_k: int | None = None) -> list[dict]:
        """Search memories by semantic similarity."""
        return self.mg.search(query, top_k=top_k or self._top_k)

    def ingest(self, content: str, metadata: dict | None = None) -> dict[str, Any]:
        """Ingest plain text into memory."""
        return self.mg.ingest(content, metadata=metadata)

    def ingest_structured(self, payload: dict[str, Any], metadata: dict | None = None) -> dict[str, Any]:
        """Ingest LLM-extracted structured data into memory."""
        return self.mg.ingest_structured(payload, metadata=metadata)

    def facts(self, entity: str | None = None) -> list[dict]:
        """Retrieve stored facts, optionally filtered by entity."""
        return self.mg.facts(entity=entity)

    def profile(self, entity: str) -> dict:
        """Get a full entity profile."""
        return self.mg.profile(entity)

    def entities(self) -> list[dict]:
        """List all known entities."""
        return self.mg.entities()

    def status(self) -> dict:
        """Return memory system status."""
        return self.mg.status()

    def consolidate(self) -> list[dict]:
        """Run memory consolidation."""
        return self.mg.consolidate()

    def close(self) -> None:
        """Flush and close MemGate."""
        if self._mg is not None:
            self._mg.close()
            self._mg = None

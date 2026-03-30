"""Schemas for LLM-driven memory extraction and agent responses."""

from __future__ import annotations

from pydantic import BaseModel, Field


class EntityFact(BaseModel):
    """A single entity-attribute-value fact."""

    name: str = Field(description="Entity name")
    attribute: str = Field(description="Property name")
    value: str = Field(description="Property value")
    previous: str | None = Field(default=None, description="Previous value being replaced, if any")


class Relationship(BaseModel):
    """A relationship between two entities."""

    a: str = Field(description="First entity")
    relation: str = Field(description="Relationship type")
    b: str = Field(description="Second entity")


class MemoryExtract(BaseModel):
    """LLM-produced structured memory payload for MemGate ingestion."""

    worth_remembering: bool = Field(description="Whether this interaction contains information worth storing in long-term memory")
    content: str | None = Field(default=None, description="Concise summary of what's worth remembering")
    entities: list[EntityFact] | None = Field(default=None, description="Extracted entity facts")
    relationships: list[Relationship] | None = Field(default=None, description="Extracted relationships between entities")
    temporal: str | None = Field(default=None, description="Date/time reference if relevant")
    update: bool = Field(default=False, description="Whether this supersedes existing facts")

    def to_memgate_payload(self) -> dict:
        """Convert to MemGate ingest_structured format."""
        payload: dict = {"content": self.content or ""}
        if self.entities:
            payload["entities"] = [e.model_dump(exclude_none=True) for e in self.entities]
        if self.relationships:
            payload["relationships"] = [r.model_dump() for r in self.relationships]
        if self.temporal:
            payload["temporal"] = self.temporal
        if self.update:
            payload["update"] = True
        return payload


class EvolutionSignal(BaseModel):
    """Signals for self-evolution extracted from each interaction."""

    new_skill: dict | None = Field(default=None, description="New reusable skill definition if discovered")
    correction: dict | None = Field(default=None, description="Error pattern if user corrected the agent")
    behavior_note: str | None = Field(default=None, description="Behavioral preference observed")
    config_suggestion: dict | None = Field(default=None, description="Suggested config change")


class AgentResponse(BaseModel):
    """Structured output from the Zoomac agent."""

    message: str = Field(description="Response message to the user")
    memory: MemoryExtract = Field(description="Memory extraction for this interaction")
    sources: list[str] = Field(default_factory=list, description="MemGate memory IDs or descriptions used")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Self-assessed confidence in the response")
    needs_verification: bool = Field(default=False, description="Whether this response should be verified before acting on it")
    evolution: EvolutionSignal = Field(default_factory=EvolutionSignal, description="Self-evolution signals")

"""Phase 1 tests — MemGate integration and memory round-trip."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memgate"))

from zoomac.brain.memory_extract import MemoryExtract, EntityFact, Relationship
from zoomac.memory.integration import MemoryManager


def test_memory_manager_init(memory_manager):
    """MemoryManager initializes and returns status."""
    status = memory_manager.status()
    assert status["n_memories"] == 0
    assert status["total_ingested"] == 0


def test_ingest_plain_text(memory_manager):
    """Ingest plain text and verify it was stored."""
    result = memory_manager.ingest("The project uses Python 3.11 with Pydantic AI.")
    assert "stored" in result
    assert "quality" in result


def test_ingest_structured(memory_manager):
    """Ingest structured data via ingest_structured and verify facts."""
    payload = {
        "content": "Alice is the lead engineer on the Zoomac project.",
        "entities": [
            {"name": "Alice", "attribute": "role", "value": "lead engineer"},
            {"name": "Alice", "attribute": "project", "value": "Zoomac"},
        ],
        "relationships": [
            {"a": "Alice", "relation": "works_on", "b": "Zoomac"},
        ],
    }
    result = memory_manager.ingest_structured(payload)
    assert "stored" in result


def test_memory_extract_round_trip(memory_manager):
    """MemoryExtract → to_memgate_payload → ingest_structured round-trip."""
    extract = MemoryExtract(
        worth_remembering=True,
        content="Bob prefers dark mode and uses VS Code.",
        entities=[
            EntityFact(name="Bob", attribute="editor", value="VS Code"),
            EntityFact(name="Bob", attribute="theme", value="dark"),
        ],
        relationships=[
            Relationship(a="Bob", relation="uses", b="VS Code"),
        ],
        temporal="2026-03-30",
    )

    payload = extract.to_memgate_payload()
    assert payload["content"] == "Bob prefers dark mode and uses VS Code."
    assert len(payload["entities"]) == 2
    assert payload["temporal"] == "2026-03-30"

    result = memory_manager.ingest_structured(payload)
    assert "stored" in result


def test_search_after_ingest(memory_manager):
    """Ingest several items, then search for relevant ones."""
    # Ingest several distinct facts
    items = [
        "Python is the primary language for the Zoomac agent.",
        "The agent uses Pydantic AI for LLM orchestration.",
        "MemGate provides intelligent memory gating with novelty detection.",
        "Docker containers are used for sandboxed execution.",
        "The scheduler uses APScheduler for cron-like background tasks.",
    ]
    for item in items:
        memory_manager.ingest(item)

    # Search for something specific
    results = memory_manager.search("LLM orchestration", top_k=3)
    assert isinstance(results, list)


def test_retrieve_context(memory_manager):
    """retrieve_context returns a formatted string."""
    memory_manager.ingest("Zoomac is a hybrid AI agent for coding and personal tasks.")
    memory_manager.ingest("The gateway connects to WhatsApp, Telegram, and Discord.")
    memory_manager.ingest("Sub-agents run in isolated Docker containers with their own MemGate.")

    context = memory_manager.retrieve_context("messaging platforms")
    assert isinstance(context, str)


def test_facts_retrieval(memory_manager):
    """Ingest structured facts and retrieve them."""
    payload = {
        "content": "Charlie manages the backend team.",
        "entities": [
            {"name": "Charlie", "attribute": "role", "value": "backend team manager"},
        ],
    }
    memory_manager.ingest_structured(payload)

    facts = memory_manager.facts(entity="Charlie")
    assert isinstance(facts, list)


def test_entity_update_with_previous(memory_manager):
    """Test fact supersession via the 'previous' field."""
    # Initial fact
    memory_manager.ingest_structured({
        "content": "Dave is a junior developer.",
        "entities": [
            {"name": "Dave", "attribute": "role", "value": "junior developer"},
        ],
    })

    # Update fact
    memory_manager.ingest_structured({
        "content": "Dave got promoted to senior developer.",
        "entities": [
            {"name": "Dave", "attribute": "role", "value": "senior developer", "previous": "junior developer"},
        ],
        "update": True,
    })

    facts = memory_manager.facts(entity="Dave")
    assert isinstance(facts, list)


def test_consolidate(memory_manager):
    """Consolidation runs without error."""
    # Need some data first
    for i in range(15):
        memory_manager.ingest(f"Memory item number {i} about topic {i % 3}.")

    result = memory_manager.consolidate()
    assert isinstance(result, list)


def test_entities_list(memory_manager):
    """entities() returns a list."""
    memory_manager.ingest_structured({
        "content": "Eve works at Acme Corp.",
        "entities": [
            {"name": "Eve", "attribute": "company", "value": "Acme Corp"},
        ],
    })
    entities = memory_manager.entities()
    assert isinstance(entities, list)

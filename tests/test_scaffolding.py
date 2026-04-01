"""Phase 0 scaffolding tests — verify the project structure and imports."""

from zoomac.core.config import ZoomacSettings, load_settings
from zoomac.core.events import EventSource, EventPriority, MessageEvent, ScheduleEvent, SystemEvent
from zoomac.brain.memory_extract import (
    AgentResponse,
    EvolutionSignal,
    MemoryExtract,
    EntityFact,
    Relationship,
)
from zoomac.brain.prompts import SYSTEM_PROMPT


def test_settings_defaults():
    settings = ZoomacSettings()
    assert settings.model == "claude-sonnet-4-20250514"
    assert settings.memory_max_tokens == 2000
    assert settings.memory_top_k == 10
    assert settings.max_sub_agents == 5
    assert settings.confidence_threshold == 0.7


def test_load_settings():
    settings = load_settings()
    assert isinstance(settings, ZoomacSettings)


def test_message_event():
    event = MessageEvent(
        source=EventSource.CLI,
        channel="cli",
        author="user",
        content="hello",
    )
    assert event.content == "hello"
    assert event.source == EventSource.CLI
    assert event.priority == EventPriority.NORMAL
    assert event.id  # auto-generated


def test_schedule_event():
    event = ScheduleEvent(job_name="test", task="do something")
    assert event.source == EventSource.SCHEDULER


def test_system_event():
    event = SystemEvent(event_type="health_check", detail="ok")
    assert event.source == EventSource.SYSTEM


def test_memory_extract_not_worth():
    extract = MemoryExtract(worth_remembering=False)
    assert not extract.worth_remembering
    assert extract.content is None


def test_memory_extract_to_payload():
    extract = MemoryExtract(
        worth_remembering=True,
        content="User prefers dark mode",
        entities=[EntityFact(name="user", attribute="preference", value="dark_mode")],
        relationships=[Relationship(a="user", relation="prefers", b="dark_mode")],
        temporal="2026-03-30",
        update=False,
    )
    payload = extract.to_memgate_payload()
    assert payload["content"] == "User prefers dark mode"
    assert len(payload["entities"]) == 1
    assert payload["entities"][0]["name"] == "user"
    assert len(payload["relationships"]) == 1
    assert payload["temporal"] == "2026-03-30"
    assert "update" not in payload  # update=False should not be included


def test_memory_extract_update_flag():
    extract = MemoryExtract(
        worth_remembering=True,
        content="Role changed",
        entities=[EntityFact(name="Alice", attribute="role", value="manager", previous="engineer")],
        update=True,
    )
    payload = extract.to_memgate_payload()
    assert payload["update"] is True
    assert payload["entities"][0]["previous"] == "engineer"


def test_agent_response_schema():
    resp = AgentResponse(
        message="Hello!",
        memory=MemoryExtract(worth_remembering=False),
        sources=[],
        confidence=0.9,
        needs_verification=False,
    )
    assert resp.message == "Hello!"
    assert resp.confidence == 0.9
    assert isinstance(resp.evolution, EvolutionSignal)


def test_evolution_signal_defaults():
    signal = EvolutionSignal()
    assert signal.new_skill is None
    assert signal.correction is None
    assert signal.behavior_note is None
    assert signal.config_suggestion is None


def test_system_prompt_exists():
    assert "Zoomac" in SYSTEM_PROMPT
    assert "search_memory" in SYSTEM_PROMPT
    assert "worth_remembering" in SYSTEM_PROMPT

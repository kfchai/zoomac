"""Phase 3 tests — Autonomy manager: classification, cascading, audit log."""

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memgate"))

from zoomac.autonomy.classifier import (
    ActionClassification,
    ActionType,
    RiskClassifier,
    RiskLevel,
)
from zoomac.autonomy.policy import AutonomyManager


# --- RiskClassifier tests ---


def test_default_low_risk_actions():
    """Actions that are low-risk by default."""
    c = RiskClassifier()
    for action in (ActionType.READ_FILE, ActionType.WEB_SEARCH, ActionType.MEMORY_QUERY, ActionType.SPAWN_AGENT):
        result = c.classify(action)
        assert result.risk == RiskLevel.LOW
        assert not result.requires_confirmation


def test_default_high_risk_actions():
    """Actions that are high-risk by default."""
    c = RiskClassifier()
    for action in (ActionType.SEND_MESSAGE, ActionType.DELETE_FILE, ActionType.GIT_PUSH, ActionType.FINANCIAL):
        result = c.classify(action)
        assert result.risk == RiskLevel.HIGH
        assert result.requires_confirmation


def test_action_default_override():
    """Custom action defaults override built-in defaults."""
    c = RiskClassifier(action_defaults={"send_message": "allow"})
    result = c.classify(ActionType.SEND_MESSAGE)
    assert result.risk == RiskLevel.LOW
    assert not result.requires_confirmation


def test_skill_override_takes_precedence():
    """Skill override beats action default."""
    c = RiskClassifier(
        skill_overrides={"check_btc_price": "allow"},
    )
    # API_CALL is high-risk by default, but skill override says allow
    result = c.classify(ActionType.API_CALL, skill_name="check_btc_price")
    assert result.risk == RiskLevel.LOW
    assert result.matched_rule == "skill:check_btc_price"


def test_platform_override_takes_precedence():
    """Platform override beats action default."""
    c = RiskClassifier(
        platform_overrides={"discord": "allow"},
    )
    result = c.classify(ActionType.SEND_MESSAGE, platform="discord")
    assert result.risk == RiskLevel.LOW
    assert result.matched_rule == "platform:discord"


def test_skill_override_beats_platform():
    """Skill override is more specific than platform override."""
    c = RiskClassifier(
        skill_overrides={"send_daily_report": "allow"},
        platform_overrides={"whatsapp": "confirm"},
    )
    # Even though whatsapp says confirm, skill override says allow
    result = c.classify(
        ActionType.SEND_MESSAGE,
        skill_name="send_daily_report",
        platform="whatsapp",
    )
    assert result.risk == RiskLevel.LOW
    assert result.matched_rule == "skill:send_daily_report"


def test_unknown_action_defaults_high():
    """An unknown action type would default to HIGH."""
    c = RiskClassifier()
    # Directly test with a known type but removed from defaults
    c._action_risks.pop(ActionType.RUN_COMMAND, None)
    result = c.classify(ActionType.RUN_COMMAND)
    assert result.risk == RiskLevel.HIGH


def test_classification_has_reason():
    """Classification includes a human-readable reason."""
    c = RiskClassifier()
    result = c.classify(ActionType.READ_FILE)
    assert "read_file" in result.reason.lower()


# --- AutonomyManager tests ---


def test_autonomy_manager_no_config():
    """AutonomyManager works without a config file."""
    am = AutonomyManager()
    result = am.classify(ActionType.READ_FILE)
    assert result.risk == RiskLevel.LOW
    am.close()


def test_autonomy_manager_with_yaml_config(tmp_path):
    """AutonomyManager loads YAML config correctly."""
    config = {
        "defaults": {
            "send_message": "allow",
            "delete_file": "confirm",
        },
        "overrides": {
            "skills": {
                "check_btc_price": "allow",
            },
            "platforms": {
                "discord": "allow",
                "whatsapp": "confirm",
            },
        },
        "timeout": {
            "confirm_wait_minutes": 15,
            "on_timeout": "escalate",
        },
    }
    config_path = tmp_path / "autonomy.yaml"
    with open(config_path, "w") as f:
        yaml.dump(config, f)

    am = AutonomyManager(config_path=config_path)

    # send_message overridden to allow
    result = am.classify(ActionType.SEND_MESSAGE)
    assert result.risk == RiskLevel.LOW

    # skill override
    result = am.classify(ActionType.API_CALL, skill_name="check_btc_price")
    assert result.risk == RiskLevel.LOW

    # platform override
    result = am.classify(ActionType.SEND_MESSAGE, platform="discord")
    assert result.risk == RiskLevel.LOW

    # timeout config
    assert am.timeout_minutes == 15
    assert am.on_timeout == "escalate"

    am.close()


def test_audit_log(tmp_path):
    """check_and_log writes to the audit log."""
    db_path = tmp_path / "audit.db"
    am = AutonomyManager(db_path=db_path)

    am.check_and_log(
        ActionType.SEND_MESSAGE,
        platform="whatsapp",
        confirmed=True,
        detail="Sent daily report to user",
    )

    am.check_and_log(
        ActionType.READ_FILE,
        detail="Read config.yaml",
    )

    log = am.audit_log(limit=10)
    assert len(log) == 2

    # Most recent first
    assert log[0]["action_type"] == "read_file"
    assert log[0]["risk"] == "low"

    assert log[1]["action_type"] == "send_message"
    assert log[1]["risk"] == "high"
    assert log[1]["confirmed"] == 1
    assert log[1]["platform"] == "whatsapp"

    am.close()


def test_audit_log_no_db():
    """audit_log returns empty list when no DB configured."""
    am = AutonomyManager()
    log = am.audit_log()
    assert log == []
    am.close()


def test_audit_log_persists(tmp_path):
    """Audit log survives close and reopen."""
    db_path = tmp_path / "audit.db"

    am1 = AutonomyManager(db_path=db_path)
    am1.check_and_log(ActionType.GIT_PUSH, detail="push to main")
    am1.close()

    am2 = AutonomyManager(db_path=db_path)
    log = am2.audit_log()
    assert len(log) == 1
    assert log[0]["action_type"] == "git_push"
    am2.close()


def test_default_config_file(tmp_path):
    """The shipped autonomy.yaml loads without error."""
    config_path = Path(__file__).parent.parent / "config" / "autonomy.yaml"
    if config_path.exists():
        am = AutonomyManager(config_path=config_path)
        # Verify defaults match what we expect
        result = am.classify(ActionType.READ_FILE)
        assert result.risk == RiskLevel.LOW
        result = am.classify(ActionType.SEND_MESSAGE)
        assert result.risk == RiskLevel.HIGH
        am.close()

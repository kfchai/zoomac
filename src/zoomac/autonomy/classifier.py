"""Risk classification for agent actions."""

from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, Field


class RiskLevel(str, Enum):
    LOW = "low"
    HIGH = "high"


class ActionType(str, Enum):
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    DELETE_FILE = "delete_file"
    RUN_COMMAND = "run_command"
    WEB_SEARCH = "web_search"
    SEND_MESSAGE = "send_message"
    API_CALL = "api_call"
    GIT_PUSH = "git_push"
    FINANCIAL = "financial"
    MODIFY_CONFIG = "modify_config"
    MEMORY_QUERY = "memory_query"
    SPAWN_AGENT = "spawn_agent"


class ActionClassification(BaseModel):
    """Result of classifying an action's risk level."""

    action_type: ActionType
    risk: RiskLevel
    reason: str
    requires_confirmation: bool
    matched_rule: str = Field(default="default", description="Which rule matched")


# Default risk levels per action type
_DEFAULT_RISKS: dict[ActionType, RiskLevel] = {
    ActionType.READ_FILE: RiskLevel.LOW,
    ActionType.WRITE_FILE: RiskLevel.LOW,
    ActionType.DELETE_FILE: RiskLevel.HIGH,
    ActionType.RUN_COMMAND: RiskLevel.LOW,
    ActionType.WEB_SEARCH: RiskLevel.LOW,
    ActionType.SEND_MESSAGE: RiskLevel.HIGH,
    ActionType.API_CALL: RiskLevel.HIGH,
    ActionType.GIT_PUSH: RiskLevel.HIGH,
    ActionType.FINANCIAL: RiskLevel.HIGH,
    ActionType.MODIFY_CONFIG: RiskLevel.HIGH,
    ActionType.MEMORY_QUERY: RiskLevel.LOW,
    ActionType.SPAWN_AGENT: RiskLevel.LOW,
}


class RiskClassifier:
    """Classifies actions by risk level using a cascade: skill > platform > action type."""

    def __init__(
        self,
        action_defaults: dict[str, str] | None = None,
        skill_overrides: dict[str, str] | None = None,
        platform_overrides: dict[str, str] | None = None,
    ) -> None:
        # Parse action defaults (override built-in defaults)
        self._action_risks: dict[ActionType, RiskLevel] = dict(_DEFAULT_RISKS)
        if action_defaults:
            for action_str, level_str in action_defaults.items():
                try:
                    action = ActionType(action_str)
                    level = self._parse_level(level_str)
                    self._action_risks[action] = level
                except (ValueError, KeyError):
                    pass

        # Skill overrides: skill_name -> risk level
        self._skill_overrides: dict[str, RiskLevel] = {}
        if skill_overrides:
            for skill, level_str in skill_overrides.items():
                self._skill_overrides[skill] = self._parse_level(level_str)

        # Platform overrides: platform -> risk level
        self._platform_overrides: dict[str, RiskLevel] = {}
        if platform_overrides:
            for platform, level_str in platform_overrides.items():
                self._platform_overrides[platform] = self._parse_level(level_str)

    def classify(
        self,
        action_type: ActionType,
        skill_name: str | None = None,
        platform: str | None = None,
    ) -> ActionClassification:
        """Classify an action. Cascade: skill override > platform override > action default."""

        # 1. Skill override (most specific)
        if skill_name and skill_name in self._skill_overrides:
            risk = self._skill_overrides[skill_name]
            return ActionClassification(
                action_type=action_type,
                risk=risk,
                reason=f"Skill override for '{skill_name}'",
                requires_confirmation=risk == RiskLevel.HIGH,
                matched_rule=f"skill:{skill_name}",
            )

        # 2. Platform override
        if platform and platform in self._platform_overrides:
            risk = self._platform_overrides[platform]
            return ActionClassification(
                action_type=action_type,
                risk=risk,
                reason=f"Platform override for '{platform}'",
                requires_confirmation=risk == RiskLevel.HIGH,
                matched_rule=f"platform:{platform}",
            )

        # 3. Action type default
        risk = self._action_risks.get(action_type, RiskLevel.HIGH)
        return ActionClassification(
            action_type=action_type,
            risk=risk,
            reason=f"Default for action type '{action_type.value}'",
            requires_confirmation=risk == RiskLevel.HIGH,
            matched_rule=f"action:{action_type.value}",
        )

    @staticmethod
    def _parse_level(level_str: str) -> RiskLevel:
        """Parse 'allow' / 'low' → LOW, 'confirm' / 'high' → HIGH."""
        level_str = level_str.lower().strip()
        if level_str in ("allow", "low"):
            return RiskLevel.LOW
        return RiskLevel.HIGH

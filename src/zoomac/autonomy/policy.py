"""Policy engine — loads autonomy config and provides the AutonomyManager."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from zoomac.autonomy.classifier import (
    ActionClassification,
    ActionType,
    RiskClassifier,
    RiskLevel,
)
from zoomac.autonomy.pipeline import (
    ApprovalDecision,
    ApprovalPipeline,
    ApprovalRequest,
)


_AUDIT_SCHEMA = """\
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    action_type TEXT NOT NULL,
    risk TEXT NOT NULL,
    requires_confirmation INTEGER NOT NULL,
    confirmed INTEGER,
    skill TEXT,
    platform TEXT,
    matched_rule TEXT,
    reason TEXT,
    detail TEXT
);
"""


class AutonomyManager:
    """Manages action classification, confirmation, and audit logging."""

    def __init__(self, config_path: str | Path | None = None, db_path: str | Path | None = None) -> None:
        config = self._load_config(config_path) if config_path else {}

        defaults_section = config.get("defaults", {})
        overrides = config.get("overrides", {})

        self._classifier = RiskClassifier(
            action_defaults=defaults_section,
            skill_overrides=overrides.get("skills", {}),
            platform_overrides=overrides.get("platforms", {}),
        )

        self._timeout_minutes = config.get("timeout", {}).get("confirm_wait_minutes", 30)
        self._on_timeout = config.get("timeout", {}).get("on_timeout", "expire")

        # Audit log
        self._db_path = str(db_path) if db_path else None
        self._db: sqlite3.Connection | None = None
        self._pipeline = ApprovalPipeline(self._classifier, self._get_db)

    @property
    def db(self) -> sqlite3.Connection | None:
        if self._db_path is None:
            return None
        if self._db is None:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self._db_path)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=MEMORY")
            self._db.execute("PRAGMA synchronous=OFF")
            self._db.executescript(_AUDIT_SCHEMA)
            self._db.executescript(self._pipeline.schema)
        return self._db

    def _get_db(self) -> sqlite3.Connection | None:
        return self.db

    def classify(
        self,
        action_type: ActionType,
        skill_name: str | None = None,
        platform: str | None = None,
        ) -> ActionClassification:
        """Classify an action and return its risk assessment."""
        return self._classifier.classify(action_type, skill_name, platform)

    def evaluate_action(
        self,
        action_type: ActionType,
        *,
        detail: str | None = None,
        skill_name: str | None = None,
        platform: str | None = None,
        session_id: str | None = None,
        command_text: str | None = None,
        file_path: str | None = None,
    ) -> ApprovalDecision:
        """Evaluate an action through the approval pipeline."""
        return self._pipeline.evaluate(
            ApprovalRequest(
                action_type=action_type,
                detail=detail,
                skill_name=skill_name,
                platform=platform,
                session_id=session_id,
                command_text=command_text,
                file_path=file_path,
            )
        )

    def allow_for_session(
        self, session_id: str, action_type: ActionType | None = None
    ) -> None:
        self._pipeline.allow_for_session(session_id, action_type)

    def allow_command_prefix(
        self, prefix: str, action_type: ActionType | None = ActionType.RUN_COMMAND
    ) -> None:
        self._pipeline.allow_command_prefix(prefix, action_type)

    def allow_path_prefix(
        self, path_prefix: str, action_type: ActionType | None = None
    ) -> None:
        self._pipeline.allow_path_prefix(path_prefix, action_type)

    def deny_command_prefix(
        self, prefix: str, action_type: ActionType | None = ActionType.RUN_COMMAND
    ) -> None:
        self._pipeline.deny_command_prefix(prefix, action_type)

    def deny_path_prefix(
        self, path_prefix: str, action_type: ActionType | None = None
    ) -> None:
        self._pipeline.deny_path_prefix(path_prefix, action_type)

    def approval_rules(self) -> list[dict[str, Any]]:
        return [asdict(rule) for rule in self._pipeline.list_rules()]

    def approval_log(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._pipeline.decision_log(limit)

    def check_and_log(
        self,
        action_type: ActionType,
        skill_name: str | None = None,
        platform: str | None = None,
        confirmed: bool | None = None,
        detail: str | None = None,
    ) -> ActionClassification:
        """Classify, log to audit, and return classification."""
        classification = self.classify(action_type, skill_name, platform)

        self._log_audit(
            classification=classification,
            skill=skill_name,
            platform=platform,
            confirmed=confirmed,
            detail=detail,
        )

        return classification

    def _log_audit(
        self,
        classification: ActionClassification,
        skill: str | None = None,
        platform: str | None = None,
        confirmed: bool | None = None,
        detail: str | None = None,
    ) -> None:
        db = self.db
        if db is None:
            return
        db.execute(
            "INSERT INTO audit_log (timestamp, action_type, risk, requires_confirmation, "
            "confirmed, skill, platform, matched_rule, reason, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                classification.action_type.value,
                classification.risk.value,
                int(classification.requires_confirmation),
                int(confirmed) if confirmed is not None else None,
                skill,
                platform,
                classification.matched_rule,
                classification.reason,
                detail,
            ),
        )
        db.commit()

    def audit_log(self, limit: int = 50) -> list[dict[str, Any]]:
        """Retrieve recent audit log entries."""
        db = self.db
        if db is None:
            return []
        cursor = db.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in cursor.fetchall()]

    @property
    def timeout_minutes(self) -> int:
        return self._timeout_minutes

    @property
    def on_timeout(self) -> str:
        return self._on_timeout

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    @staticmethod
    def _load_config(path: str | Path) -> dict:
        path = Path(path)
        if not path.exists():
            return {}
        with open(path) as f:
            return yaml.safe_load(f) or {}

"""Approval pipeline for actions that may require user confirmation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

from zoomac.autonomy.classifier import ActionClassification, ActionType, RiskClassifier


_PIPELINE_SCHEMA = """\
CREATE TABLE IF NOT EXISTS approval_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    value TEXT NOT NULL,
    action_type TEXT,
    mode TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_approval_rules_scope
    ON approval_rules(scope, value);

CREATE TABLE IF NOT EXISTS approval_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    action_type TEXT NOT NULL,
    outcome TEXT NOT NULL,
    mode TEXT NOT NULL,
    provenance TEXT NOT NULL,
    session_id TEXT,
    detail TEXT,
    command_text TEXT,
    file_path TEXT,
    reason TEXT NOT NULL
);
"""


class ApprovalOutcome(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ApprovalMode(str, Enum):
    AUTO_ALLOW = "auto_allow"
    ASK_ONCE = "ask_once"
    ALLOW_FOR_SESSION = "allow_for_session"
    ALLOW_BY_RULE = "allow_by_rule"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Context for evaluating whether an action may proceed."""

    action_type: ActionType
    detail: str | None = None
    skill_name: str | None = None
    platform: str | None = None
    session_id: str | None = None
    command_text: str | None = None
    file_path: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """Outcome of the approval pipeline."""

    action_type: ActionType
    outcome: ApprovalOutcome
    mode: ApprovalMode
    reason: str
    provenance: str
    classification: ActionClassification

    @property
    def allows_execution(self) -> bool:
        return self.outcome == ApprovalOutcome.ALLOW

    @property
    def requires_user_input(self) -> bool:
        return self.outcome == ApprovalOutcome.ASK


@dataclass(frozen=True, slots=True)
class ApprovalRule:
    """Stored reusable approval rule."""

    id: int
    scope: str
    value: str
    action_type: ActionType | None
    mode: ApprovalMode
    created_at: str


class ApprovalPipeline:
    """Evaluates actions using static risk classification plus reusable rules."""

    def __init__(
        self,
        classifier: RiskClassifier,
        db_provider: Callable[[], sqlite3.Connection | None],
    ) -> None:
        self._classifier = classifier
        self._db_provider = db_provider

    @property
    def schema(self) -> str:
        return _PIPELINE_SCHEMA

    def evaluate(self, request: ApprovalRequest) -> ApprovalDecision:
        """Evaluate whether an action should be allowed, asked, or denied."""
        classification = self._classifier.classify(
            request.action_type,
            skill_name=request.skill_name,
            platform=request.platform,
        )

        deny_rule = self._match_rule(request, allowed=False)
        if deny_rule is not None:
            decision = ApprovalDecision(
                action_type=request.action_type,
                outcome=ApprovalOutcome.DENY,
                mode=ApprovalMode.DENY,
                reason=f"Denied by stored rule ({deny_rule.scope})",
                provenance=f"rule:{deny_rule.scope}:{deny_rule.value}",
                classification=classification,
            )
            self._record_decision(request, decision)
            return decision

        allow_rule = self._match_rule(request, allowed=True)
        if allow_rule is not None:
            mode = (
                ApprovalMode.ALLOW_FOR_SESSION
                if allow_rule.scope == "session"
                else ApprovalMode.ALLOW_BY_RULE
            )
            decision = ApprovalDecision(
                action_type=request.action_type,
                outcome=ApprovalOutcome.ALLOW,
                mode=mode,
                reason=f"Allowed by stored rule ({allow_rule.scope})",
                provenance=f"rule:{allow_rule.scope}:{allow_rule.value}",
                classification=classification,
            )
            self._record_decision(request, decision)
            return decision

        if classification.requires_confirmation:
            decision = ApprovalDecision(
                action_type=request.action_type,
                outcome=ApprovalOutcome.ASK,
                mode=ApprovalMode.ASK_ONCE,
                reason=classification.reason,
                provenance=classification.matched_rule,
                classification=classification,
            )
            self._record_decision(request, decision)
            return decision

        decision = ApprovalDecision(
            action_type=request.action_type,
            outcome=ApprovalOutcome.ALLOW,
            mode=ApprovalMode.AUTO_ALLOW,
            reason=classification.reason,
            provenance=classification.matched_rule,
            classification=classification,
        )
        self._record_decision(request, decision)
        return decision

    def allow_for_session(self, session_id: str, action_type: ActionType | None = None) -> None:
        self._add_rule(
            scope="session",
            value=session_id,
            action_type=action_type,
            mode=ApprovalMode.ALLOW_FOR_SESSION,
        )

    def allow_command_prefix(
        self, prefix: str, action_type: ActionType | None = ActionType.RUN_COMMAND
    ) -> None:
        self._add_rule(
            scope="command_prefix",
            value=prefix,
            action_type=action_type,
            mode=ApprovalMode.ALLOW_BY_RULE,
        )

    def allow_path_prefix(self, path_prefix: str, action_type: ActionType | None = None) -> None:
        self._add_rule(
            scope="path_prefix",
            value=self._normalize_path(path_prefix),
            action_type=action_type,
            mode=ApprovalMode.ALLOW_BY_RULE,
        )

    def deny_command_prefix(
        self, prefix: str, action_type: ActionType | None = ActionType.RUN_COMMAND
    ) -> None:
        self._add_rule(
            scope="command_prefix",
            value=prefix,
            action_type=action_type,
            mode=ApprovalMode.DENY,
        )

    def deny_path_prefix(self, path_prefix: str, action_type: ActionType | None = None) -> None:
        self._add_rule(
            scope="path_prefix",
            value=self._normalize_path(path_prefix),
            action_type=action_type,
            mode=ApprovalMode.DENY,
        )

    def list_rules(self) -> list[ApprovalRule]:
        db = self._db_provider()
        if db is None:
            return []
        rows = db.execute(
            "SELECT id, scope, value, action_type, mode, created_at "
            "FROM approval_rules ORDER BY id DESC"
        ).fetchall()
        return [
            ApprovalRule(
                id=row["id"],
                scope=row["scope"],
                value=row["value"],
                action_type=ActionType(row["action_type"]) if row["action_type"] else None,
                mode=ApprovalMode(row["mode"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def decision_log(self, limit: int = 50) -> list[dict[str, str | None]]:
        db = self._db_provider()
        if db is None:
            return []
        rows = db.execute(
            "SELECT * FROM approval_decisions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _match_rule(self, request: ApprovalRequest, *, allowed: bool) -> ApprovalRule | None:
        rules = self.list_rules()
        for rule in rules:
            if allowed and rule.mode not in (
                ApprovalMode.ALLOW_FOR_SESSION,
                ApprovalMode.ALLOW_BY_RULE,
            ):
                continue
            if not allowed and rule.mode != ApprovalMode.DENY:
                continue
            if rule.action_type is not None and rule.action_type != request.action_type:
                continue
            if rule.scope == "session" and request.session_id == rule.value:
                return rule
            if rule.scope == "command_prefix" and request.command_text:
                if request.command_text.strip().lower().startswith(rule.value.strip().lower()):
                    return rule
            if rule.scope == "path_prefix" and request.file_path:
                normalized_request = self._normalize_path(request.file_path)
                if normalized_request.startswith(rule.value):
                    return rule
        return None

    def _add_rule(
        self,
        *,
        scope: str,
        value: str,
        action_type: ActionType | None,
        mode: ApprovalMode,
    ) -> None:
        db = self._db_provider()
        if db is None:
            return
        db.execute(
            "INSERT INTO approval_rules (scope, value, action_type, mode, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                scope,
                value,
                action_type.value if action_type is not None else None,
                mode.value,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db.commit()

    def _record_decision(self, request: ApprovalRequest, decision: ApprovalDecision) -> None:
        db = self._db_provider()
        if db is None:
            return
        db.execute(
            "INSERT INTO approval_decisions "
            "(timestamp, action_type, outcome, mode, provenance, session_id, detail, command_text, file_path, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                request.action_type.value,
                decision.outcome.value,
                decision.mode.value,
                decision.provenance,
                request.session_id,
                request.detail,
                request.command_text,
                self._normalize_path(request.file_path) if request.file_path else None,
                decision.reason,
            ),
        )
        db.commit()

    @staticmethod
    def _normalize_path(path_text: str) -> str:
        return Path(path_text).resolve(strict=False).as_posix().lower()

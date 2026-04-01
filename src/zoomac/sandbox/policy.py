"""Policy-driven sandbox profile resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from zoomac.autonomy.pipeline import ApprovalDecision, ApprovalOutcome
from zoomac.sandbox.profiles import ProfileName, SandboxProfile, get_profile


@dataclass(frozen=True, slots=True)
class SandboxExecutionIntent:
    """Describe what the upcoming sandbox execution needs."""

    command_text: str
    requested_profile: ProfileName | str | None = None
    requires_network: bool = False
    reads_project: bool = False
    writes_project: bool = False
    allowed_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EffectiveSandboxPolicy:
    """Resolved sandbox execution decision."""

    profile_name: ProfileName
    profile: SandboxProfile
    reason: str
    audit_reasons: list[str]
    normalized_allowed_paths: list[str]
    project_mount_mode: str | None = None
    requires_approval: bool = False
    approval_outcome: str | None = None
    execution_allowed: bool = True


class SandboxPolicyResolver:
    """Resolve an execution intent into a concrete sandbox profile."""

    def __init__(self, project_dir: str | None = None, home_dir: str | None = None) -> None:
        self._project_dir = project_dir
        self._home_dir = home_dir

    def resolve(
        self,
        intent: SandboxExecutionIntent,
        approval: ApprovalDecision | None = None,
    ) -> EffectiveSandboxPolicy:
        """Resolve the execution policy for a command."""
        requested = (
            ProfileName(intent.requested_profile)
            if isinstance(intent.requested_profile, str)
            else intent.requested_profile
        )

        normalized_paths = list(
            dict.fromkeys(self.normalize_path(path) for path in intent.allowed_paths)
        )
        project_mount_mode: str | None = None
        audit_reasons: list[str] = []

        if requested is not None:
            profile_name = requested
            audit_reasons.append(f"Explicit profile request: {profile_name.value}")
        elif intent.writes_project and intent.requires_network:
            profile_name = ProfileName.FULL
            audit_reasons.append(
                "Project writes combined with network access require elevated sandboxing"
            )
        elif intent.writes_project:
            profile_name = ProfileName.PROJECT
            project_mount_mode = "rw"
            audit_reasons.append("Project write access requires the project profile")
        elif intent.reads_project:
            profile_name = ProfileName.PROJECT
            project_mount_mode = "ro"
            audit_reasons.append("Project reads require the project profile")
        elif intent.requires_network:
            profile_name = ProfileName.STANDARD
            audit_reasons.append("Network access requires the standard profile")
        else:
            profile_name = ProfileName.MINIMAL
            audit_reasons.append("Read-only inspect command can use the minimal profile")

        requires_approval = profile_name == ProfileName.FULL
        execution_allowed = True
        approval_outcome = approval.outcome.value if approval is not None else None
        if approval is not None:
            if approval.outcome == ApprovalOutcome.ALLOW:
                requires_approval = False
                audit_reasons.append(
                    f"Approval already granted via {approval.mode.value} ({approval.provenance})"
                )
            elif approval.outcome == ApprovalOutcome.ASK:
                requires_approval = True
                audit_reasons.append(
                    f"Approval still required via {approval.mode.value} ({approval.provenance})"
                )
            elif approval.outcome == ApprovalOutcome.DENY:
                requires_approval = False
                execution_allowed = False
                audit_reasons.append(
                    f"Execution denied by approval pipeline ({approval.provenance})"
                )

        extra_mounts = self._build_allowed_path_mounts(normalized_paths)
        if normalized_paths:
            audit_reasons.append(
                f"Mounted {len(normalized_paths)} extra path(s) as read-only context"
            )

        profile = get_profile(
            profile_name,
            project_dir=self._project_dir,
            home_dir=self._home_dir,
            project_mode=project_mount_mode or "ro",
            extra_mounts=extra_mounts,
        )

        return EffectiveSandboxPolicy(
            profile_name=profile_name,
            profile=profile,
            reason=audit_reasons[0],
            audit_reasons=audit_reasons,
            normalized_allowed_paths=normalized_paths,
            project_mount_mode=project_mount_mode,
            requires_approval=requires_approval,
            approval_outcome=approval_outcome,
            execution_allowed=execution_allowed,
        )

    @staticmethod
    def normalize_path(path_text: str) -> str:
        """Normalize a path for policy comparisons across OSes."""
        return Path(path_text).resolve(strict=False).as_posix().lower()

    @staticmethod
    def _build_allowed_path_mounts(paths: list[str]) -> list[dict[str, str]]:
        mounts: list[dict[str, str]] = []
        for index, path in enumerate(paths):
            mounts.append(
                {
                    "source": path,
                    "target": f"/context/path-{index}",
                    "mode": "ro",
                    "type": "bind",
                }
            )
        return mounts

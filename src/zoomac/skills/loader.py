"""Skill descriptor loading."""

from __future__ import annotations

from pathlib import Path

import yaml

from zoomac.skills.registry import SkillDescriptor, SkillRegistry


def load_skill_registry(*directories: Path) -> SkillRegistry:
    """Load all `*.skill.yaml` descriptors from the provided directories."""
    registry = SkillRegistry()
    for directory in directories:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.skill.yaml")):
            registry.register(_load_skill(path))
    return registry


def load_builtin_skill_registry() -> SkillRegistry:
    """Load packaged built-in skill descriptors."""
    base_dir = Path(__file__).resolve().parent / "builtin"
    return load_skill_registry(base_dir)


def _load_skill(path: Path) -> SkillDescriptor:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return SkillDescriptor(
        name=data["name"],
        description=data["description"],
        instructions=data["instructions"],
        triggers=list(data.get("triggers", [])),
        tool_names=list(data.get("tool_names", [])),
        tags=list(data.get("tags", [])),
        source=str(path),
    )

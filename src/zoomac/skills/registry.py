"""Skill descriptor models and registry helpers."""

from __future__ import annotations

from dataclasses import dataclass, field

from zoomac.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class SkillDescriptor:
    """A reusable capability that can guide prompts and tool selection."""

    name: str
    description: str
    instructions: str
    triggers: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source: str = "builtin"

    def matches(self, text: str) -> bool:
        haystack = text.lower()
        return any(trigger.lower() in haystack for trigger in self.triggers + self.tags)


@dataclass(slots=True)
class SkillRegistry:
    """Stores and queries loaded skill descriptors."""

    _skills: dict[str, SkillDescriptor] = field(default_factory=dict)

    def register(self, skill: SkillDescriptor) -> None:
        if skill.name in self._skills:
            raise ValueError(f"Skill '{skill.name}' is already registered.")
        self._skills[skill.name] = skill

    def list(self) -> list[SkillDescriptor]:
        return list(self._skills.values())

    def names(self) -> list[str]:
        return list(self._skills)

    def get(self, name: str) -> SkillDescriptor:
        return self._skills[name]

    def relevant_for(self, text: str) -> list[SkillDescriptor]:
        return [skill for skill in self._skills.values() if skill.matches(text)]

    def prompt_section(self, text: str | None = None) -> str:
        skills = self.relevant_for(text) if text else self.list()
        if not skills:
            return ""
        lines = ["## Available Skills"]
        for skill in skills:
            lines.append(f"- {skill.name}: {skill.description}")
            lines.append(f"  Instructions: {skill.instructions}")
            if skill.tool_names:
                lines.append(f"  Tools: {', '.join(skill.tool_names)}")
        return "\n".join(lines)

    def filter_tools(self, registry: ToolRegistry, text: str | None = None) -> ToolRegistry:
        skills = self.relevant_for(text) if text else self.list()
        allowed_tools = {tool for skill in skills for tool in skill.tool_names}
        if not allowed_tools:
            return registry
        return registry.select(lambda tool: tool.name in allowed_tools)

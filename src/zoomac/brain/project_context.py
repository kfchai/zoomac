"""Auto-detect project structure and load instruction files."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

INSTRUCTION_FILES = ["ZOOMAC.md", "CLAUDE.md", "AGENTS.md"]

SKIP_DIRS = {
    "node_modules", ".git", ".next", ".turbo", "__pycache__", ".venv",
    "venv", "dist", "build", "out", ".cache", "coverage", ".zoomac",
    ".claude", "target", "vendor",
}

CONFIG_FILES = [
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
    "tsconfig.json", "turbo.json", "vite.config.ts", "next.config.js",
]


def load_instruction_file(project_dir: str) -> tuple[str, str] | None:
    """Load the first found instruction file (ZOOMAC.md → CLAUDE.md → AGENTS.md)."""
    for name in INSTRUCTION_FILES:
        path = os.path.join(project_dir, name)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    return name, content
            except Exception:
                pass
    return None


def gather_project_context(project_dir: str) -> str:
    """Gather project structure, configs, and git info."""
    sections: list[str] = []

    # Directory tree (top 2 levels)
    tree = _build_tree(project_dir, max_depth=2)
    if tree:
        sections.append(f"## Project Structure\n```\n{tree}\n```")

    # Config files
    for name in CONFIG_FILES:
        path = os.path.join(project_dir, name)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                if len(content) < 10000:
                    lines = content.split("\n")[:50]
                    sections.append(f"## {name}\n```\n{chr(10).join(lines)}\n```")
            except Exception:
                pass

    # Git context
    git_log = _run_cmd("git log --oneline -10", project_dir)
    if git_log:
        sections.append(f"## Recent Commits\n```\n{git_log.strip()}\n```")

    git_status = _run_cmd("git status --short", project_dir)
    if git_status:
        lines = git_status.strip().split("\n")[:20]
        sections.append(f"## Git Status\n```\n{chr(10).join(lines)}\n```")

    if not sections:
        return ""

    return (
        "\n\n## Auto-detected Project Context\n\n"
        + "\n\n".join(sections)
    )


def _build_tree(dir_path: str, max_depth: int, prefix: str = "", depth: int = 0) -> str:
    """Build a directory tree string."""
    if depth > max_depth:
        return ""

    try:
        entries = sorted(os.listdir(dir_path))
    except OSError:
        return ""

    items = []
    for name in entries:
        if name.startswith(".") and name not in (".gitignore", ".env.example"):
            continue
        if name in SKIP_DIRS:
            continue
        full = os.path.join(dir_path, name)
        try:
            is_dir = os.path.isdir(full)
        except OSError:
            continue
        items.append((name, is_dir, full))

    items.sort(key=lambda x: (not x[1], x[0]))
    max_items = 30 if depth == 0 else 15
    shown = items[:max_items]
    hidden = len(items) - len(shown)

    lines: list[str] = []
    for i, (name, is_dir, full) in enumerate(shown):
        is_last = i == len(shown) - 1 and hidden == 0
        connector = "└── " if is_last else "├── "
        child_prefix = "    " if is_last else "│   "

        if is_dir:
            lines.append(f"{prefix}{connector}{name}/")
            if depth < max_depth:
                sub = _build_tree(full, max_depth, prefix + child_prefix, depth + 1)
                if sub:
                    lines.append(sub)
        else:
            lines.append(f"{prefix}{connector}{name}")

    if hidden > 0:
        lines.append(f"{prefix}└── ... ({hidden} more)")

    return "\n".join(lines)


def _run_cmd(cmd: str, cwd: str) -> str:
    """Run a shell command, return stdout or empty string."""
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True,
            text=True, timeout=5,
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""

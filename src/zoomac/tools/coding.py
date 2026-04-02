"""Direct file I/O and shell tools for autonomous coding mode."""

from __future__ import annotations

import asyncio
import glob as glob_module
import os
import subprocess
from pathlib import Path

from zoomac.autonomy.classifier import ActionType
from zoomac.brain.deps import ZoomacDeps
from zoomac.tools.base import ToolCapabilities, ToolDefinition, ToolSpec
from zoomac.tools.registry import ToolRegistry


def _resolve_path(project_dir: str, file_path: str) -> str:
    """Resolve a file path relative to the project directory."""
    if os.path.isabs(file_path):
        return file_path
    return os.path.join(project_dir, file_path)


async def read_file(deps: ZoomacDeps, file_path: str, offset: int = 0, limit: int = 0) -> str:
    """Read a file from the workspace with optional line range."""
    project_dir = getattr(deps, "project_dir", None) or os.getcwd()
    resolved = _resolve_path(project_dir, file_path)

    try:
        with open(resolved, "r", encoding="utf-8") as f:
            lines = f.readlines()

        start = offset
        end = (offset + limit) if limit > 0 else len(lines)
        selected = lines[start:end]

        numbered = [f"{start + i + 1}\t{line.rstrip()}" for i, line in enumerate(selected)]
        return "\n".join(numbered)
    except Exception as e:
        return f"Error reading {file_path}: {e}"


async def write_file(deps: ZoomacDeps, file_path: str, content: str) -> str:
    """Write content to a file, creating parent directories if needed."""
    project_dir = getattr(deps, "project_dir", None) or os.getcwd()
    resolved = _resolve_path(project_dir, file_path)

    try:
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
        line_count = content.count("\n") + 1
        return f"Wrote {len(content)} bytes ({line_count} lines) to {file_path}"
    except Exception as e:
        return f"Error writing {file_path}: {e}"


async def edit_file(deps: ZoomacDeps, file_path: str, old_string: str, new_string: str) -> str:
    """Replace a unique string in a file."""
    project_dir = getattr(deps, "project_dir", None) or os.getcwd()
    resolved = _resolve_path(project_dir, file_path)

    try:
        with open(resolved, "r", encoding="utf-8") as f:
            text = f.read()

        idx = text.find(old_string)
        if idx == -1:
            return f"Error: old_string not found in {file_path}"

        second = text.find(old_string, idx + 1)
        if second != -1:
            return f"Error: old_string is not unique in {file_path} (found at positions {idx} and {second}). Provide more context."

        new_text = text.replace(old_string, new_string, 1)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(new_text)

        old_lines = old_string.count("\n") + 1
        new_lines = new_string.count("\n") + 1
        diff = new_lines - old_lines
        diff_str = f"(+{diff} lines)" if diff > 0 else f"({diff} lines)" if diff < 0 else "(same line count)"
        return f"Edited {file_path}: replaced {old_lines} lines with {new_lines} lines {diff_str}"
    except Exception as e:
        return f"Error editing {file_path}: {e}"


async def run_bash(deps: ZoomacDeps, command: str, timeout: int = 30000) -> str:
    """Execute a shell command in the project directory."""
    project_dir = getattr(deps, "project_dir", None) or os.getcwd()
    timeout_sec = timeout / 1000

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        output = result.stdout
        if result.stderr:
            output += f"\nSTDERR:\n{result.stderr}" if output else result.stderr
        if not output:
            output = "(Bash completed with no output)"
        if len(output) > 10000:
            output = output[:10000] + f"\n... [truncated, {len(output)} total chars]"
        return output
    except subprocess.TimeoutExpired:
        return f"[TIMED OUT after {timeout_sec}s]"
    except Exception as e:
        return f"Error: {e}"


async def glob_files(deps: ZoomacDeps, pattern: str, path: str = "") -> str:
    """Find files matching a glob pattern."""
    project_dir = getattr(deps, "project_dir", None) or os.getcwd()
    search_dir = _resolve_path(project_dir, path) if path else project_dir

    try:
        full_pattern = os.path.join(search_dir, pattern)
        matches = glob_module.glob(full_pattern, recursive=True)

        # Filter out common noise
        skip = {"node_modules", ".git", "__pycache__", ".next", ".turbo", "dist", "build"}
        filtered = [
            m for m in matches
            if not any(s in m.split(os.sep) for s in skip)
        ]

        if not filtered:
            return f"No files found matching: {pattern}"

        relative = sorted(os.path.relpath(m, project_dir) for m in filtered[:500])
        return "\n".join(relative)
    except Exception as e:
        return f"Error searching: {e}"


async def grep_files(deps: ZoomacDeps, pattern: str, path: str = "", glob: str = "") -> str:
    """Search file contents for a regex pattern using ripgrep."""
    project_dir = getattr(deps, "project_dir", None) or os.getcwd()
    search_path = _resolve_path(project_dir, path) if path else project_dir

    cmd = f'rg -n --no-heading "{pattern}"'
    if glob:
        cmd += f' --glob "{glob}"'
    cmd += f' "{search_path}"'

    try:
        result = subprocess.run(
            cmd, shell=True, cwd=project_dir,
            capture_output=True, text=True, timeout=15,
        )
        output = result.stdout
        if not output:
            return "No matches found."
        if len(output) > 10000:
            output = output[:10000] + f"\n... [truncated, {len(output)} total chars]"
        return output
    except subprocess.TimeoutExpired:
        return "Grep timed out."
    except Exception as e:
        return f"Grep error: {e}"


async def python_exec(deps: ZoomacDeps, code: str, timeout: int = 30000) -> str:
    """Execute inline Python code and return stdout/stderr."""
    import tempfile

    project_dir = getattr(deps, "project_dir", None) or os.getcwd()
    timeout_sec = timeout / 1000

    # Write code to a temp file and execute
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", dir=project_dir,
            prefix="_zoomac_exec_", delete=False, encoding="utf-8",
        ) as f:
            f.write(code)
            tmp_path = f.name

        try:
            result = subprocess.run(
                ["python", tmp_path],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
            output = result.stdout
            if result.stderr:
                output += f"\nSTDERR:\n{result.stderr}" if output else result.stderr
            if result.returncode != 0 and not output:
                output = f"Exit code: {result.returncode}"
            if not output:
                output = "(No output)"
            if len(output) > 10000:
                output = output[:10000] + f"\n... [truncated, {len(output)} total chars]"
            return output
        finally:
            os.unlink(tmp_path)
    except subprocess.TimeoutExpired:
        return f"[TIMED OUT after {timeout_sec}s]"
    except Exception as e:
        return f"Error: {e}"


async def ask_user(deps: ZoomacDeps, question: str, options: str = "") -> str:
    """Ask the user a question and wait for their response (CLI mode: stdin)."""
    print(f"\n🔵 Agent asks: {question}")
    if options:
        opt_list = [o.strip() for o in options.split(",")]
        for i, opt in enumerate(opt_list, 1):
            print(f"  {i}. {opt}")
    answer = input("Your answer: ").strip()
    return f"User answered: {answer}"


def build_coding_tool_registry() -> ToolRegistry:
    """Create the registry for direct coding tools."""
    registry = ToolRegistry(name="coding")
    registry.extend([
        ToolDefinition(
            spec=ToolSpec(
                name="read",
                description="Read a file from the workspace. Returns file contents with line numbers. Use offset and limit for large files.",
                capabilities=ToolCapabilities(read_only=True, supports_parallel=True),
            ),
            handler=read_file,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="write",
                description="Write content to a file. Creates parent directories automatically.",
                capabilities=ToolCapabilities(writes_project=True),
                approval_action_type=ActionType.WRITE_FILE.value,
            ),
            handler=write_file,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="edit",
                description="Replace a specific string in a file. The old_string must be unique.",
                capabilities=ToolCapabilities(writes_project=True),
                approval_action_type=ActionType.WRITE_FILE.value,
            ),
            handler=edit_file,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="bash",
                description="Execute a shell command in the workspace directory.",
                capabilities=ToolCapabilities(requires_network=True),
                approval_action_type=ActionType.RUN_COMMAND.value,
            ),
            handler=run_bash,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="glob",
                description="Find files matching a glob pattern in the workspace.",
                capabilities=ToolCapabilities(read_only=True, supports_parallel=True),
            ),
            handler=glob_files,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="grep",
                description="Search file contents for a regex pattern. Returns matching lines with file paths and line numbers.",
                capabilities=ToolCapabilities(read_only=True, supports_parallel=True),
            ),
            handler=grep_files,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="python_exec",
                description=(
                    "Execute inline Python code and return stdout/stderr. "
                    "Use for quick calculations, data exploration, testing snippets, "
                    "or running scripts without creating a file. "
                    "The code runs in the workspace directory with full access to installed packages."
                ),
                capabilities=ToolCapabilities(requires_network=True),
                approval_action_type=ActionType.RUN_COMMAND.value,
            ),
            handler=python_exec,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="ask_user",
                description=(
                    "Ask the user a question and wait for their response. "
                    "Use when you need clarification, confirmation, or a choice between options."
                ),
                capabilities=ToolCapabilities(read_only=True),
            ),
            handler=ask_user,
        ),
    ])
    return registry

"""Tool result trimming and conversation history compression."""

from __future__ import annotations

import re


# Tools that can be executed in parallel safely
SAFE_TOOLS = {"read", "glob", "grep", "search_memory", "check_facts", "memory_status"}

# Tools that modify state and need sequential/confirmed execution
DESTRUCTIVE_TOOLS = {"write", "edit", "bash", "write_code", "execute_in_sandbox"}


def trim_tool_result(result: str, tool_name: str) -> str:
    """Aggressively trim tool results before sending to LLM."""
    if len(result) <= 1500:
        return result

    if tool_name == "read":
        lines = result.split("\n")
        if len(lines) > 60:
            head = "\n".join(lines[:40])
            tail = "\n".join(lines[-10:])
            return f"{head}\n\n... [{len(lines) - 50} lines omitted] ...\n\n{tail}"
        if len(result) > 3000:
            return result[:2500] + f"\n... [truncated, {len(lines)} lines total]"
        return result

    if tool_name in ("grep", "search"):
        lines = result.split("\n")
        if len(lines) > 20:
            return "\n".join(lines[:20]) + f"\n... [{len(lines) - 20} more matches]"
        return result

    if tool_name == "glob":
        lines = result.split("\n")
        if len(lines) > 30:
            return "\n".join(lines[:30]) + f"\n... [{len(lines) - 30} more files]"
        return result

    if tool_name == "bash":
        lines = result.split("\n")
        if len(lines) > 50:
            head = "\n".join(lines[:30])
            tail = "\n".join(lines[-10:])
            return f"{head}\n... [{len(lines) - 40} lines omitted] ...\n{tail}"
        if len(result) > 4000:
            return result[:3000] + "\n... [truncated]"
        return result

    if tool_name == "edit":
        return result  # Already short

    if len(result) > 3000:
        return result[:2500] + f"\n... [truncated, {len(result)} chars]"

    return result


def compress_history_tool_results(messages: list[dict]) -> list[dict]:
    """Compress verbose tool results in history with one-line summaries."""
    for i, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        modified = False
        for j, block in enumerate(content):
            if block.get("type") != "tool_result":
                continue
            result_text = block.get("content", "")
            if not isinstance(result_text, str) or len(result_text) <= 200:
                continue

            # Find matching tool_use in previous assistant message
            tool_name = "tool"
            if i > 0:
                prev = messages[i - 1]
                if prev.get("role") == "assistant" and isinstance(prev.get("content"), list):
                    for b in prev["content"]:
                        if b.get("type") == "tool_use" and b.get("id") == block.get("tool_use_id"):
                            tool_name = b.get("name", "tool")
                            break

            content[j] = {**block, "content": _summarize_result(tool_name, result_text)}
            modified = True

        if modified:
            messages[i] = {**msg, "content": content}

    return messages


def _summarize_result(tool_name: str, result: str) -> str:
    """One-line summary of a tool result."""
    lines = result.split("\n")
    line_count = len(lines)

    if tool_name == "read":
        return f"[Read: {line_count} lines, {len(result)} chars]"
    if tool_name in ("write", "write_code"):
        return result if len(result) <= 200 else f"[Wrote file: {line_count} lines]"
    if tool_name == "edit":
        return result
    if tool_name == "bash":
        return lines[0] + (f"\n... [{line_count} lines total]" if line_count > 1 else "")
    if tool_name == "glob":
        return f"[Found {line_count} files]"
    if tool_name in ("grep", "search", "search_memory"):
        return f"[{line_count} matches found]"
    return result[:150] + "..." if len(result) > 200 else result


def extract_inline_memory(text: str) -> list[dict]:
    """Extract <memory> JSON blocks from LLM response text."""
    blocks = []
    for match in re.finditer(r"<memory>\s*(\{[\s\S]*?\})\s*</memory>", text):
        try:
            payload = __import__("json").loads(match.group(1))
            if payload.get("content"):
                blocks.append(payload)
        except Exception:
            pass
    return blocks


def strip_memory_blocks(text: str) -> str:
    """Remove <memory> blocks from text for display."""
    return re.sub(r"<memory>[\s\S]*?</memory>", "", text).strip()

"""Slash commands for the autonomous agent (/commit, /review, /clear, etc.)."""

from __future__ import annotations

import logging
from typing import Any

from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)


async def handle_slash_command(
    cmd: str,
    args: str,
    *,
    client: AsyncAnthropic,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int = 4096,
) -> dict[str, Any] | None:
    """Handle a slash command. Returns a dict with results, or None if not a command."""

    if cmd == "/commit":
        return {
            "type": "inject",
            "content": (
                "The user wants to commit their changes. "
                "Run `git diff --stat` and `git status --short` to see what changed, "
                "then generate a concise commit message. "
                "Stage relevant files (skip .env, secrets, lock files) "
                f"and run `git commit -m \"...\"`. {f'User note: {args}' if args else ''}"
            ),
        }

    if cmd == "/review":
        review = await _run_reviewer(client, model, messages, args, max_tokens)
        return {
            "type": "review",
            "display": f"### 🔍 Reviewer Agent\n\n{review}",
            "inject": (
                f"[REVIEWER FEEDBACK — a second agent reviewed your recent response. "
                f"Consider this feedback and adjust if needed.]\n\n{review}"
            ),
        }

    if cmd == "/clear":
        return {"type": "clear"}

    if cmd == "/compact":
        return {"type": "compact"}

    if cmd == "/model":
        if args:
            return {"type": "model_switch", "model": args}
        return {"type": "model_show", "model": model}

    if cmd == "/help":
        return {
            "type": "display",
            "content": (
                "### Commands\n"
                "- `/commit [note]` — Auto-commit with generated message\n"
                "- `/review [focus]` — Get a 2nd opinion from a reviewer agent\n"
                "- `/clear` — Clear conversation\n"
                "- `/compact` — Force context compaction\n"
                "- `/model [name]` — Show/switch model\n"
                "- `/help` — This help"
            ),
        }

    return None


async def _run_reviewer(
    client: AsyncAnthropic,
    model: str,
    messages: list[dict[str, Any]],
    focus: str,
    max_tokens: int,
) -> str:
    """Run a reviewer agent that critiques the main agent's recent work."""
    reviewer_system = (
        "You are a senior code reviewer. Review the conversation and the agent's "
        "recent proposals and actions. Be constructive but honest. Point out:\n"
        "- Bugs, logic errors, or edge cases missed\n"
        "- Security concerns or bad practices\n"
        "- Better approaches or alternatives\n"
        "- Missing error handling or tests\n"
        "- Things done well\n\n"
        "Be concise — 3-5 bullet points max."
    )

    # Summarize recent conversation
    recent = messages[-20:] if len(messages) > 20 else messages
    summary = "\n\n".join(
        f"[{m.get('role', '?')}]: {_msg_preview(m)}" for m in recent
    )

    prompt = (
        f"Review with focus on: {focus}\n\n{summary}"
        if focus
        else f"Review the agent's recent work:\n\n{summary}"
    )

    try:
        response = await client.messages.create(
            model=model,
            system=reviewer_system,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=min(2048, max_tokens),
        )
        text = ""
        for block in response.content:
            if block.type == "text":
                text += block.text
        return text or "No review feedback generated."
    except Exception as e:
        return f"Review failed: {e}"


def _msg_preview(msg: dict) -> str:
    content = msg.get("content", "")
    if isinstance(content, str):
        return content[:500]
    parts = []
    for block in content:
        if block.get("type") == "text":
            parts.append(block.get("text", "")[:200])
        elif block.get("type") == "tool_use":
            parts.append(f"[tool: {block.get('name', '?')}]")
        elif block.get("type") == "tool_result":
            parts.append(f"[result: {block.get('content', '')[:100]}]")
    return " ".join(parts)

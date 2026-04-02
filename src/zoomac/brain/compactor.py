"""Context compaction — summarizes old conversation turns when approaching model limits."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return len(text) // 4


def message_tokens(msg: dict) -> int:
    """Estimate tokens for a single message."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return estimate_tokens(content) + 4
    total = 4
    for block in content:
        if block.get("type") == "text":
            total += estimate_tokens(block.get("text", ""))
        elif block.get("type") == "tool_use":
            total += estimate_tokens(block.get("name", "")) + estimate_tokens(str(block.get("input", {})))
        elif block.get("type") == "tool_result":
            total += estimate_tokens(block.get("content", ""))
    return total


def estimate_total_tokens(system: str, messages: list[dict]) -> int:
    """Estimate total tokens for system prompt + all messages."""
    total = estimate_tokens(system)
    for msg in messages:
        total += message_tokens(msg)
    return total


def get_max_context_tokens(model: str) -> int:
    """Model context limits."""
    m = model.lower()
    if "claude-3-5" in m or "claude-sonnet-4" in m or "claude-opus" in m:
        return 200000
    if "claude" in m:
        return 100000
    if "gemini-2.5" in m or "gemini-2.0" in m:
        return 1000000
    if "gpt-4o" in m or "gpt-4-turbo" in m:
        return 128000
    if "gpt-4" in m:
        return 8192
    return 32000


@dataclass
class CompactionResult:
    messages: list[dict]
    compacted_count: int
    tokens_saved: int


async def compact_context(
    client: AsyncAnthropic,
    model: str,
    system: str,
    messages: list[dict],
    max_context_tokens: int,
    threshold: float = 0.7,
    keep_recent: int = 6,
) -> CompactionResult | None:
    """Compact conversation context when approaching the model's token limit."""
    total_tokens = estimate_total_tokens(system, messages)
    limit = int(max_context_tokens * threshold)

    if total_tokens < limit:
        return None

    if len(messages) <= keep_recent * 2:
        return None

    split_index = len(messages) - keep_recent * 2
    old_messages = messages[:split_index]
    recent_messages = messages[split_index:]

    # Summarize old messages
    conv_text = "\n".join(
        f"[{m.get('role', '?')}]: {_msg_preview(m)}" for m in old_messages
    )

    summary_prompt = (
        "Summarize this conversation concisely. Focus on: decisions made, "
        "files modified, key facts learned, user preferences. Be concise.\n\n"
        + conv_text
    )

    try:
        response = await client.messages.create(
            model=model,
            system="You are a conversation summarizer. Produce a concise summary.",
            messages=[{"role": "user", "content": summary_prompt}],
            max_tokens=2048,
        )

        summary = ""
        for block in response.content:
            if block.type == "text":
                summary += block.text

        if not summary:
            return None

        summary_msg = {
            "role": "user",
            "content": f"[Conversation summary from earlier messages]\n\n{summary}\n\n[End summary]",
        }
        ack_msg = {
            "role": "assistant",
            "content": [{"type": "text", "text": "Understood. I have the context. Continuing."}],
        }

        new_messages = [summary_msg, ack_msg] + recent_messages

        old_tokens = sum(message_tokens(m) for m in old_messages)
        new_tokens = message_tokens(summary_msg) + message_tokens(ack_msg)

        return CompactionResult(
            messages=new_messages,
            compacted_count=len(old_messages),
            tokens_saved=old_tokens - new_tokens,
        )
    except Exception:
        logger.exception("Context compaction failed")
        return None


def _msg_preview(msg: dict) -> str:
    content = msg.get("content", "")
    if isinstance(content, str):
        return content[:300]
    parts = []
    for block in content:
        if block.get("type") == "text":
            parts.append(block.get("text", "")[:200])
        elif block.get("type") == "tool_use":
            parts.append(f"[tool: {block.get('name', '?')}]")
        elif block.get("type") == "tool_result":
            parts.append(f"[result: {block.get('content', '')[:100]}]")
    return " ".join(parts)

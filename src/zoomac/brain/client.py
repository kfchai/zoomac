"""Async wrapper around the Anthropic Messages API with optimizations."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any, Type

from anthropic import AsyncAnthropic
from pydantic import BaseModel

from zoomac.brain.optimizer import SAFE_TOOLS, trim_tool_result
from zoomac.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}

DEFAULT_MAX_TOKENS = 8192


def _python_type_to_json(annotation: Any) -> str:
    return _TYPE_MAP.get(annotation, "string")


def tool_definitions_to_anthropic(registry: ToolRegistry) -> list[dict[str, Any]]:
    """Convert a ToolRegistry into Anthropic API ``tools`` parameter format."""
    tools: list[dict[str, Any]] = []
    for tool_def in registry.list():
        sig = inspect.signature(tool_def.handler)
        params = list(sig.parameters.values())
        properties: dict[str, Any] = {}
        required: list[str] = []
        for p in params[1:]:  # skip deps
            prop: dict[str, Any] = {"type": _python_type_to_json(p.annotation)}
            if p.default is not inspect.Parameter.empty:
                prop["default"] = p.default
            else:
                required.append(p.name)
            properties[p.name] = prop
        tools.append({
            "name": tool_def.spec.name,
            "description": tool_def.spec.description,
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        })
    return tools


def tool_handlers_from_registry(registry: ToolRegistry) -> dict[str, Any]:
    return {td.spec.name: td.handler for td in registry.list()}


async def run_with_tools(
    client: AsyncAnthropic,
    model: str,
    *,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_handlers: dict[str, Any] | None = None,
    deps: Any = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_iterations: int = 200,
    enable_caching: bool = True,
    enable_thinking: bool = True,
) -> dict[str, Any]:
    """Run a conversation turn with tool execution loop.

    Returns a dict with:
      - text: final assistant text
      - messages: updated message list
      - usage: cumulative usage dict from all API calls
    """
    msgs = list(messages)
    total_usage: dict[str, int] = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }

    for iteration in range(max_iterations):
        # Build API params
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": msgs,
            "max_tokens": max_tokens,
        }

        # Prompt caching: structured system blocks with cache_control
        if enable_caching:
            kwargs["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        else:
            kwargs["system"] = system

        # Extended thinking — only on first call (planning), not during tool loop
        if enable_thinking and iteration == 0 and ("sonnet-4" in model or "opus-4" in model or "claude-4" in model):
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": min(4096, max_tokens)}

        if tools:
            kwargs["tools"] = tools

        response = await client.messages.create(**kwargs)

        # Accumulate usage
        usage = response.usage
        if usage:
            usage_dict = vars(usage) if hasattr(usage, "__dict__") else {}
            for key in total_usage:
                total_usage[key] += getattr(usage, key, 0) or usage_dict.get(key, 0)

        # Filter content — skip thinking blocks and empty text
        text_parts: list[str] = []
        tool_uses: list[dict[str, Any]] = []
        assistant_content: list[dict[str, Any]] = []

        for block in response.content:
            if block.type == "text" and block.text:
                text_parts.append(block.text)
                assistant_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                tool_uses.append({"id": block.id, "name": block.name, "input": block.input})
                assistant_content.append({
                    "type": "tool_use", "id": block.id,
                    "name": block.name, "input": block.input,
                })
            # Skip thinking blocks and empty text blocks

        if response.stop_reason != "tool_use" or not tool_uses:
            return {
                "text": "\n".join(text_parts),
                "messages": msgs,
                "usage": total_usage,
            }

        msgs.append({"role": "assistant", "content": assistant_content})

        # Split into safe (parallel) and destructive (sequential)
        safe = [tu for tu in tool_uses if tu["name"] in SAFE_TOOLS]
        destructive = [tu for tu in tool_uses if tu["name"] not in SAFE_TOOLS]

        tool_results: list[dict[str, Any]] = []

        # Execute safe tools in parallel
        if safe:
            async def _exec_safe(tu: dict) -> dict:
                handler = (tool_handlers or {}).get(tu["name"])
                if handler is None:
                    result = f"Error: unknown tool '{tu['name']}'"
                else:
                    try:
                        result = await handler(deps, **tu["input"])
                    except Exception as exc:
                        logger.exception("Tool %s failed", tu["name"])
                        result = f"Error: {exc}"
                trimmed = trim_tool_result(result, tu["name"])
                return {"type": "tool_result", "tool_use_id": tu["id"], "content": trimmed}

            safe_results = await asyncio.gather(*[_exec_safe(tu) for tu in safe])
            tool_results.extend(safe_results)

        # Execute destructive tools sequentially
        for tu in destructive:
            handler = (tool_handlers or {}).get(tu["name"])
            if handler is None:
                result_text = f"Error: unknown tool '{tu['name']}'"
            else:
                try:
                    result_text = await handler(deps, **tu["input"])
                except Exception as exc:
                    logger.exception("Tool %s failed", tu["name"])
                    result_text = f"Error: {exc}"
            trimmed = trim_tool_result(result_text, tu["name"])
            tool_results.append({
                "type": "tool_result", "tool_use_id": tu["id"], "content": trimmed,
            })

        msgs.append({"role": "user", "content": tool_results})

    return {
        "text": "\n".join(text_parts) if text_parts else "",
        "messages": msgs,
        "usage": total_usage,
    }


async def run_structured(
    client: AsyncAnthropic,
    model: str,
    prompt: str,
    output_type: Type[BaseModel],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_retries: int = 1,
) -> BaseModel:
    """Call the API requesting structured JSON output, parse into *output_type*."""
    schema = json.dumps(output_type.model_json_schema(), indent=2)
    system = (
        "You are a structured-output assistant. "
        "Respond ONLY with valid JSON (no markdown fences, no commentary) "
        f"matching this schema:\n{schema}"
    )

    api_messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

    for attempt in range(1 + max_retries):
        response = await client.messages.create(
            model=model, system=system, messages=api_messages, max_tokens=max_tokens,
        )

        text = ""
        for block in response.content:
            if block.type == "text":
                text += block.text

        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        try:
            return output_type.model_validate_json(text)
        except Exception as exc:
            if attempt < max_retries:
                api_messages.append({"role": "assistant", "content": text})
                api_messages.append({
                    "role": "user",
                    "content": f"Your response was not valid JSON: {exc}\nPlease fix and respond with ONLY valid JSON.",
                })
            else:
                raise ValueError(
                    f"Failed to parse {output_type.__name__} after {1 + max_retries} attempts: {exc}"
                ) from exc

    raise RuntimeError("run_structured loop exited unexpectedly")

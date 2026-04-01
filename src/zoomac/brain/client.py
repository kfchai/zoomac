"""Thin async wrapper around the Anthropic Messages API."""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Type

from anthropic import AsyncAnthropic
from pydantic import BaseModel

from zoomac.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Python type annotation -> JSON Schema type mapping
_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}

DEFAULT_MAX_TOKENS = 4096


def _python_type_to_json(annotation: Any) -> str:
    """Map a Python type annotation to a JSON Schema type string."""
    return _TYPE_MAP.get(annotation, "string")


def tool_definitions_to_anthropic(registry: ToolRegistry) -> list[dict[str, Any]]:
    """Convert a ToolRegistry into Anthropic API ``tools`` parameter format."""
    tools: list[dict[str, Any]] = []
    for tool_def in registry.list():
        sig = inspect.signature(tool_def.handler)
        params = list(sig.parameters.values())
        # First param is ``deps`` — skip it
        properties: dict[str, Any] = {}
        required: list[str] = []
        for p in params[1:]:
            prop: dict[str, Any] = {"type": _python_type_to_json(p.annotation)}
            if p.default is not inspect.Parameter.empty:
                prop["default"] = p.default
            else:
                required.append(p.name)
            properties[p.name] = prop
        tools.append(
            {
                "name": tool_def.spec.name,
                "description": tool_def.spec.description,
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            }
        )
    return tools


def tool_handlers_from_registry(registry: ToolRegistry) -> dict[str, Any]:
    """Build a name -> handler lookup dict from a ToolRegistry."""
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
    max_iterations: int = 10,
) -> str:
    """Run a conversation turn, looping on tool calls until the model stops.

    Returns the final assistant text content.
    """
    msgs = list(messages)

    for _ in range(max_iterations):
        kwargs: dict[str, Any] = {
            "model": model,
            "system": system,
            "messages": msgs,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools

        response = await client.messages.create(**kwargs)

        # Collect text and tool_use blocks
        text_parts: list[str] = []
        tool_uses: list[dict[str, Any]] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(
                    {"id": block.id, "name": block.name, "input": block.input}
                )

        if response.stop_reason != "tool_use" or not tool_uses:
            return "\n".join(text_parts)

        # Execute tool calls and build result messages
        assistant_content: list[dict[str, Any]] = []
        for block in response.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )

        msgs.append({"role": "assistant", "content": assistant_content})

        tool_results: list[dict[str, Any]] = []
        for tu in tool_uses:
            handler = (tool_handlers or {}).get(tu["name"])
            if handler is None:
                result_text = f"Error: unknown tool '{tu['name']}'"
            else:
                try:
                    result_text = await handler(deps, **tu["input"])
                except Exception as exc:
                    logger.exception("Tool %s failed", tu["name"])
                    result_text = f"Error: {exc}"
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": result_text,
                }
            )

        msgs.append({"role": "user", "content": tool_results})

    return "\n".join(text_parts) if text_parts else ""


async def run_structured(
    client: AsyncAnthropic,
    model: str,
    prompt: str,
    output_type: Type[BaseModel],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_retries: int = 1,
) -> BaseModel:
    """Call the API requesting structured JSON output, parse into *output_type*.

    Injects the JSON schema into the system prompt and validates the response.
    On parse failure, retries once asking the model to fix its JSON.
    """
    schema = json.dumps(output_type.model_json_schema(), indent=2)
    system = (
        "You are a structured-output assistant. "
        "Respond ONLY with valid JSON (no markdown fences, no commentary) "
        f"matching this schema:\n{schema}"
    )

    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

    for attempt in range(1 + max_retries):
        response = await client.messages.create(
            model=model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
        )

        text = ""
        for block in response.content:
            if block.type == "text":
                text += block.text

        # Strip markdown fences if the model adds them despite instructions
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            # Remove first and last fence lines
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        try:
            return output_type.model_validate_json(text)
        except Exception as exc:
            if attempt < max_retries:
                messages.append({"role": "assistant", "content": text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Your response was not valid JSON: {exc}\n"
                            "Please fix and respond with ONLY valid JSON."
                        ),
                    }
                )
                logger.debug("Structured output parse failed (attempt %d), retrying", attempt + 1)
            else:
                raise ValueError(
                    f"Failed to parse {output_type.__name__} after {1 + max_retries} attempts: {exc}"
                ) from exc

    # Unreachable, but satisfies type checker
    raise RuntimeError("run_structured loop exited unexpectedly")

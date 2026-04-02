"""Token usage tracking and cost estimation."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Pricing per million tokens (USD)
_PRICING: dict[str, dict[str, float]] = {
    "opus": {"input": 15, "output": 75, "cache_read": 1.5, "cache_write": 18.75},
    "sonnet": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75},
    "haiku": {"input": 0.25, "output": 1.25, "cache_read": 0.025, "cache_write": 0.3},
    "gpt-4o": {"input": 2.5, "output": 10, "cache_read": 1.25, "cache_write": 2.5},
    "gpt-4": {"input": 30, "output": 60, "cache_read": 15, "cache_write": 30},
    "default": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75},
}


@dataclass
class UsageTracker:
    """Tracks cumulative token usage and costs for a session."""

    total_input: int = 0
    total_output: int = 0
    total_cache_read: int = 0
    total_cache_write: int = 0
    api_calls: int = 0
    total_cost: float = 0.0

    def track(self, model: str, usage: dict) -> dict:
        """Track usage from an API response. Returns summary dict."""
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_write = usage.get("cache_creation_input_tokens", 0)

        self.api_calls += 1
        self.total_input += input_tokens
        self.total_output += output_tokens
        self.total_cache_read += cache_read
        self.total_cache_write += cache_write

        call_cost = self._estimate_cost(model, input_tokens, output_tokens, cache_read, cache_write)
        self.total_cost += call_cost

        cache_pct = 0
        if self.total_input + self.total_cache_read > 0:
            cache_pct = round(self.total_cache_read / (self.total_input + self.total_cache_read) * 100)

        return {
            "input": input_tokens,
            "output": output_tokens,
            "cache_read": cache_read,
            "cache_write": cache_write,
            "call_cost": call_cost,
            "total_input": self.total_input,
            "total_output": self.total_output,
            "total_cost": self.total_cost,
            "cache_pct": cache_pct,
            "api_calls": self.api_calls,
        }

    def format_summary(self, info: dict) -> str:
        """Format a CC CLI-style usage summary line."""
        inp = info["input"] / 1000
        out = info["output"] / 1000
        t_inp = info["total_input"] / 1000
        t_out = info["total_output"] / 1000

        parts = [f"↑{inp:.1f}K ↓{out:.1f}K"]

        if info["cache_read"] > 0 or info["cache_write"] > 0:
            cr = info["cache_read"] / 1000
            cw = info["cache_write"] / 1000
            parts.append(f"cache: {cr:.1f}K read, {cw:.1f}K write")

        if info["call_cost"] > 0:
            parts.append(f"${info['call_cost']:.4f}")

        session = [f"↑{t_inp:.1f}K ↓{t_out:.1f}K"]
        if info["cache_pct"] > 0:
            session.append(f"{info['cache_pct']}% cached")
        session.append(f"${info['total_cost']:.4f}")
        session.append(f"{info['api_calls']} calls")

        return " · ".join(parts) + " │ Session: " + " · ".join(session)

    def _estimate_cost(
        self, model: str, input_t: int, output_t: int, cache_read: int, cache_write: int
    ) -> float:
        m = model.lower()
        prices = _PRICING["default"]
        for key in _PRICING:
            if key in m:
                prices = _PRICING[key]
                break

        return (
            input_t * prices["input"]
            + output_t * prices["output"]
            + cache_read * prices["cache_read"]
            + cache_write * prices["cache_write"]
        ) / 1_000_000

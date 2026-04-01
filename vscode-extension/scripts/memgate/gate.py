"""QualityGate — multiplicative store/skip decision."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from memgate.config import MemGateConfig


@dataclass
class GateDecision:
    """Result of a quality gate evaluation."""

    should_store: bool
    quality_score: float
    priority: float
    explanation: str


class QualityGate:
    """Multiplicative quality gate combining novelty and continuity signals.

    quality = novelty_gate * continuity_gate
    where:
        novelty_gate    = clamp(novelty, 0, 1) * (1 + novelty_strength)
        continuity_gate = 1.0 + (continuity - 0.5) * continuity_strength

    Novelty is the primary driver: low novelty (< 0.35) drags quality below
    threshold regardless of continuity. Continuity acts as a boost/penalty
    for on-topic vs off-topic content.

    With sigmoid-mapped novelty (average content = 0.5):
      - novelty > 0.5 (above-average) → novelty_gate > 1.0 → likely stores
      - novelty < 0.35 (redundant) → novelty_gate < 0.7 → likely skips
      - continuity shifts the boundary by ~+/-15%
    """

    def __init__(self, config: MemGateConfig | None = None) -> None:
        from memgate.config import MemGateConfig

        self._config = config or MemGateConfig()

    def evaluate(self, novelty: float, continuity: float) -> GateDecision:
        """Evaluate whether to store or skip.

        Args:
            novelty: Novelty score in [0, 1] (sigmoid-mapped, 0.5 = average).
            continuity: Continuity score in [0, 1].

        Returns:
            GateDecision with store/skip verdict.
        """
        novelty_clamped = float(np.clip(novelty, 0.0, 1.0))
        continuity_clamped = float(np.clip(continuity, 0.0, 1.0))

        # Novelty gate: 0 at novelty=0, scales linearly
        # With novelty_strength=1.0: range [0, 2.0]
        novelty_gate = novelty_clamped * (1.0 + self._config.novelty_strength)

        # Continuity gate: centered at 1.0, penalizes off-topic, boosts on-topic
        # With continuity_strength=0.8: range [0.6, 1.4]
        continuity_gate = 1.0 + (continuity_clamped - 0.5) * self._config.continuity_strength

        quality_score = novelty_gate * continuity_gate

        should_store = quality_score > self._config.store_threshold
        priority = quality_score / self._config.store_threshold if self._config.store_threshold > 0 else quality_score

        # Build explanation
        if novelty_clamped > 0.5 and continuity_clamped > 0.6:
            explanation = "novel and on-topic"
        elif novelty_clamped > 0.5 and continuity_clamped <= 0.4:
            explanation = "novel but off-topic"
        elif novelty_clamped <= 0.35 and continuity_clamped > 0.6:
            explanation = "redundant on-topic"
        elif novelty_clamped <= 0.35 and continuity_clamped <= 0.4:
            explanation = "redundant off-topic"
        else:
            explanation = "borderline"

        return GateDecision(
            should_store=should_store,
            quality_score=quality_score,
            priority=priority,
            explanation=explanation,
        )

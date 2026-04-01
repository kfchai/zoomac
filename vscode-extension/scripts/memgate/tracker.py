"""ContinuityTracker — embedding-based topic continuity tracking."""

from __future__ import annotations

import numpy as np


class ContinuityTracker:
    """Tracks whether consecutive inputs are on the same topic.

    Uses cosine similarity between current and previous embeddings.
    High continuity = same topic; low continuity = topic shift.
    """

    def __init__(self) -> None:
        self._previous_embedding: np.ndarray | None = None

    def compute(
        self, current_emb: np.ndarray, previous_emb: np.ndarray | None = None
    ) -> float:
        """Compute continuity score between current and previous embedding.

        Args:
            current_emb: Current embedding vector.
            previous_emb: Previous embedding vector. If None, uses stored previous.

        Returns:
            Cosine similarity in [0, 1]. Returns 0.5 if no previous embedding.
        """
        prev = previous_emb if previous_emb is not None else self._previous_embedding

        if prev is None:
            return 0.5

        norm_curr = np.linalg.norm(current_emb)
        norm_prev = np.linalg.norm(prev)

        if norm_curr < 1e-10 or norm_prev < 1e-10:
            return 0.5

        similarity = float(np.dot(current_emb, prev) / (norm_curr * norm_prev))
        # Clamp to [0, 1] — negative cosine similarity treated as 0
        return max(0.0, min(1.0, similarity))

    def update(self, embedding: np.ndarray) -> None:
        """Update the stored previous embedding."""
        self._previous_embedding = embedding.copy()

    @property
    def previous_embedding(self) -> np.ndarray | None:
        return self._previous_embedding

    @previous_embedding.setter
    def previous_embedding(self, value: np.ndarray | None) -> None:
        self._previous_embedding = value.copy() if value is not None else None

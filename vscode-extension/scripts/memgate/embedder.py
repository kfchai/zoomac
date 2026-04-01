"""Embedder — sentence-transformer wrapper with LRU cache."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from memgate.config import MemGateConfig


class Embedder:
    """Wraps sentence-transformers for local CPU embedding."""

    # Class-level shared models to avoid reloading across instances
    _shared_models: dict[str, object] = {}

    def __init__(self, config: MemGateConfig | None = None) -> None:
        from memgate.config import MemGateConfig

        self._config = config or MemGateConfig()
        self._model = None  # lazy load
        self._cache: dict[str, np.ndarray] = {}
        self._cache_order: list[str] = []
        self._max_cache = self._config.embed_cache_size

    def _load_model(self):
        if self._model is None:
            model_name = self._config.embed_model
            if model_name not in Embedder._shared_models:
                from sentence_transformers import SentenceTransformer

                Embedder._shared_models[model_name] = SentenceTransformer(model_name)
            self._model = Embedder._shared_models[model_name]
        return self._model

    @property
    def dim(self) -> int:
        """Embedding dimensionality (384 for all-MiniLM-L6-v2)."""
        return 384

    def embed(self, text: str) -> np.ndarray:
        """Embed a single text string. Returns 384-dim float32 vector."""
        if text in self._cache:
            return self._cache[text].copy()

        model = self._load_model()
        vec = model.encode(text, show_progress_bar=False, convert_to_numpy=True)
        vec = vec.astype(np.float32)

        # LRU eviction
        if len(self._cache) >= self._max_cache:
            oldest = self._cache_order.pop(0)
            self._cache.pop(oldest, None)

        self._cache[text] = vec
        self._cache_order.append(text)
        return vec.copy()

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        """Embed a batch of texts. Uses cache for already-seen texts."""
        # Split into cached and uncached
        results: dict[int, np.ndarray] = {}
        uncached_indices: list[int] = []
        uncached_texts: list[str] = []

        for i, text in enumerate(texts):
            if text in self._cache:
                results[i] = self._cache[text].copy()
            else:
                uncached_indices.append(i)
                uncached_texts.append(text)

        if uncached_texts:
            model = self._load_model()
            vecs = model.encode(
                uncached_texts, show_progress_bar=False, convert_to_numpy=True
            )
            for idx, text, vec in zip(uncached_indices, uncached_texts, vecs):
                vec = vec.astype(np.float32)
                # Cache it
                if len(self._cache) >= self._max_cache:
                    oldest = self._cache_order.pop(0)
                    self._cache.pop(oldest, None)
                self._cache[text] = vec
                self._cache_order.append(text)
                results[idx] = vec.copy()

        return [results[i] for i in range(len(texts))]

"""Shared test fixtures for Zoomac."""

import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

# Ensure memgate submodule is importable
_memgate_path = str(Path(__file__).resolve().parent.parent / "memgate")
if _memgate_path not in sys.path:
    sys.path.insert(0, _memgate_path)

from zoomac.core.config import ZoomacSettings
from zoomac.memory.integration import MemoryManager


class FakeEmbedder:
    """A fake embedder that returns deterministic 384-dim vectors without a model."""

    _shared_models: dict = {}

    def __init__(self, config=None):
        self._config = config
        self._model = None
        self._rng = np.random.RandomState(42)
        self._cache: dict[str, np.ndarray] = {}
        self._cache_order: list[str] = []
        self._max_cache = 10000

    def _load_model(self):
        return None

    @property
    def dim(self) -> int:
        return 384

    def embed(self, text: str) -> np.ndarray:
        if text not in self._cache:
            vec = self._rng.randn(384).astype(np.float32)
            vec /= np.linalg.norm(vec)
            self._cache[text] = vec
            self._cache_order.append(text)
        return self._cache[text].copy()

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        return [self.embed(t) for t in texts]


@pytest.fixture(autouse=True)
def _patch_embedder():
    """Globally replace MemGate's Embedder with FakeEmbedder at import time."""
    with patch("memgate.engine.Embedder", FakeEmbedder):
        with patch("memgate.embedder.Embedder", FakeEmbedder):
            yield


@pytest.fixture
def tmp_settings(tmp_path):
    """Settings pointing to a temporary directory."""
    return ZoomacSettings(project_dir=tmp_path, memgate_db=Path(".memgate.db"))


@pytest.fixture
def memory_manager(tmp_path):
    """A MemoryManager backed by a temp directory."""
    mm = MemoryManager(project_dir=str(tmp_path), max_tokens=2000, top_k=10)
    yield mm
    mm.close()

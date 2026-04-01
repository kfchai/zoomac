"""MemGate configuration — loads from env vars, constructor kwargs, or defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MemGateConfig:
    """Configuration for the MemGate engine.

    Priority: constructor kwargs > env vars > defaults.
    Env vars are prefixed with MEMGATE_ (e.g. MEMGATE_STORE_THRESHOLD=0.8).
    """

    # Quality gate
    store_threshold: float = 0.8
    novelty_strength: float = 1.0
    continuity_strength: float = 0.8

    # Novelty detector (MLP)
    proj_dim: int = 64
    hidden1_dim: int = 128
    hidden2_dim: int = 64
    context_depth: int = 3
    learning_rate: float = 0.003
    weight_decay: float = 0.998
    permanent_ratio: float = 0.2
    ema_alpha: float = 0.05

    # Cold start
    cold_start_count: int = 10

    # Retriever
    default_top_k: int = 10
    max_context_tokens: int = 2000

    # Topic model
    h1_cache_size: int = 1000
    cluster_overlap_threshold: float = 0.5
    top_neurons_per_term: int = 5

    # Embedder
    embed_model: str = "all-MiniLM-L6-v2"
    embed_cache_size: int = 10000

    # Ring buffer decay weights (oldest → newest)
    context_decay_weights: tuple[float, ...] = (0.49, 0.7, 1.0)

    # Retrieval dedup
    recency_weight: float = 0.1
    conflict_similarity_threshold: float = 0.85

    # Temporal + entity
    temporal_boost_weight: float = 0.3
    entity_min_name_length: int = 3

    # Consolidation
    consolidation_interval: int = 50
    representatives_per_cluster: int = 5
    keywords_per_cluster: int = 10

    # LLM consolidation (opt-in, wired later)
    llm_consolidation: bool = False
    llm_consolidation_provider: str = ""
    llm_consolidation_model: str = ""

    # Database
    db_filename: str = ".memgate.db"

    def __post_init__(self) -> None:
        """Override fields from MEMGATE_ env vars where set."""
        for fld in self.__dataclass_fields__:
            env_key = f"MEMGATE_{fld.upper()}"
            env_val = os.environ.get(env_key)
            if env_val is not None:
                current = getattr(self, fld)
                try:
                    if isinstance(current, bool):
                        setattr(self, fld, env_val.lower() in ("1", "true", "yes"))
                    elif isinstance(current, int):
                        setattr(self, fld, int(env_val))
                    elif isinstance(current, float):
                        setattr(self, fld, float(env_val))
                    elif isinstance(current, str):
                        setattr(self, fld, env_val)
                except (ValueError, TypeError):
                    pass  # ignore malformed env vars

    @classmethod
    def from_kwargs(cls, **kwargs: Any) -> MemGateConfig:
        """Create config, passing only recognised fields."""
        valid = {k: v for k, v in kwargs.items() if k in cls.__dataclass_fields__}
        return cls(**valid)

"""NoveltyDetector — 2-layer MLP with split (dual-rate) learning."""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from memgate.config import MemGateConfig


def _gelu(x: np.ndarray) -> np.ndarray:
    """Gaussian Error Linear Unit activation."""
    return 0.5 * x * (1.0 + np.tanh(np.sqrt(2.0 / np.pi) * (x + 0.044715 * x**3)))


def _gelu_grad(x: np.ndarray) -> np.ndarray:
    """Approximate GELU gradient."""
    cdf = 0.5 * (1.0 + np.tanh(np.sqrt(2.0 / np.pi) * (x + 0.044715 * x**3)))
    t = np.sqrt(2.0 / np.pi) * (1.0 + 3 * 0.044715 * x**2)
    sech2 = 1.0 - np.tanh(np.sqrt(2.0 / np.pi) * (x + 0.044715 * x**3)) ** 2
    pdf = 0.5 * t * sech2
    return cdf + x * pdf


class NoveltyDetector:
    """Online predictive MLP with split learning for novelty detection.

    Architecture: context[input_dim] -> W1[h1_dim x input_dim] -> GELU
                  -> W2[h2_dim x h1_dim] -> GELU -> W_out[out_dim x h2_dim] -> prediction

    Split learning: Each weight matrix is decomposed into permanent (20%) and
    temporary (80%) components. Temporary weights decay, permanent weights persist.
    High-quality memories trigger consolidation (temporary -> permanent).
    """

    def __init__(self, config: MemGateConfig | None = None) -> None:
        from memgate.config import MemGateConfig

        self._config = config or MemGateConfig()

        input_dim = self._config.proj_dim * self._config.context_depth  # 64*3=192
        h1_dim = self._config.hidden1_dim  # 128
        h2_dim = self._config.hidden2_dim  # 64
        out_dim = self._config.proj_dim  # 64

        self._input_dim = input_dim
        self._h1_dim = h1_dim
        self._h2_dim = h2_dim
        self._out_dim = out_dim

        # Permanent weights (slow-adapting, 20% of learning)
        self._W1_perm = self._xavier_init(h1_dim, input_dim)
        self._b1_perm = np.zeros(h1_dim, dtype=np.float32)
        self._W2_perm = self._xavier_init(h2_dim, h1_dim)
        self._b2_perm = np.zeros(h2_dim, dtype=np.float32)
        self._Wout_perm = self._xavier_init(out_dim, h2_dim)
        self._bout_perm = np.zeros(out_dim, dtype=np.float32)

        # Temporary weights (fast-adapting, 80% of learning, decays)
        self._W1_temp = np.zeros((h1_dim, input_dim), dtype=np.float32)
        self._b1_temp = np.zeros(h1_dim, dtype=np.float32)
        self._W2_temp = np.zeros((h2_dim, h1_dim), dtype=np.float32)
        self._b2_temp = np.zeros(h2_dim, dtype=np.float32)
        self._Wout_temp = np.zeros((out_dim, h2_dim), dtype=np.float32)
        self._bout_temp = np.zeros(out_dim, dtype=np.float32)

        # Activation cache: term -> h1 activations
        self._h1_cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._max_cache = self._config.h1_cache_size

    def _xavier_init(self, fan_out: int, fan_in: int) -> np.ndarray:
        """Xavier weight initialization with sqrt(2/fan_in)."""
        std = np.sqrt(2.0 / fan_in)
        return (np.random.randn(fan_out, fan_in) * std).astype(np.float32)

    def forward(
        self, context: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Forward pass through the MLP.

        Args:
            context: Flattened context vector [input_dim]

        Returns:
            (pre_h1, h1, pre_h2, h2, prediction)
        """
        # Combined weights = permanent + temporary
        W1 = self._W1_perm + self._W1_temp
        b1 = self._b1_perm + self._b1_temp
        W2 = self._W2_perm + self._W2_temp
        b2 = self._b2_perm + self._b2_temp
        Wout = self._Wout_perm + self._Wout_temp
        bout = self._bout_perm + self._bout_temp

        # Layer 1
        pre_h1 = W1 @ context + b1
        h1 = _gelu(pre_h1)

        # Layer 2
        pre_h2 = W2 @ h1 + b2
        h2 = _gelu(pre_h2)

        # Output
        prediction = Wout @ h2 + bout

        return pre_h1, h1, pre_h2, h2, prediction

    def update(
        self,
        context: np.ndarray,
        actual: np.ndarray,
        term: str | None = None,
    ) -> float:
        """Forward + backprop + split learning. Returns prediction error (L2).

        Args:
            context: Flattened context vector [input_dim]
            actual: Target projection [out_dim]
            term: Optional content prefix for h1 caching

        Returns:
            Prediction error (L2 distance)
        """
        pre_h1, h1, pre_h2, h2, prediction = self.forward(context)

        # Cache h1 activations
        if term is not None:
            self._h1_cache[term] = h1.copy()
            if len(self._h1_cache) > self._max_cache:
                self._h1_cache.popitem(last=False)

        # Prediction error
        error = actual - prediction
        pred_error = float(np.linalg.norm(error))

        # Backpropagation
        lr = self._config.learning_rate
        perm_ratio = self._config.permanent_ratio  # 0.2
        temp_ratio = 1.0 - perm_ratio  # 0.8

        # Output layer gradients
        d_out = error  # dL/d_prediction (MSE gradient, negated since error = actual - pred)
        d_Wout = np.outer(d_out, h2)
        d_bout = d_out

        # Layer 2 gradients
        d_h2 = (self._Wout_perm + self._Wout_temp).T @ d_out
        d_pre_h2 = d_h2 * _gelu_grad(pre_h2)
        d_W2 = np.outer(d_pre_h2, h1)
        d_b2 = d_pre_h2

        # Layer 1 gradients
        d_h1 = (self._W2_perm + self._W2_temp).T @ d_pre_h2
        d_pre_h1 = d_h1 * _gelu_grad(pre_h1)
        d_W1 = np.outer(d_pre_h1, context)
        d_b1 = d_pre_h1

        # Apply weight decay to temporary weights BEFORE update
        decay = self._config.weight_decay
        self._W1_temp *= decay
        self._b1_temp *= decay
        self._W2_temp *= decay
        self._b2_temp *= decay
        self._Wout_temp *= decay
        self._bout_temp *= decay

        # Split update: permanent gets 20%, temporary gets 80%
        self._W1_perm += lr * perm_ratio * d_W1
        self._b1_perm += lr * perm_ratio * d_b1
        self._W2_perm += lr * perm_ratio * d_W2
        self._b2_perm += lr * perm_ratio * d_b2
        self._Wout_perm += lr * perm_ratio * d_Wout
        self._bout_perm += lr * perm_ratio * d_bout

        self._W1_temp += lr * temp_ratio * d_W1
        self._b1_temp += lr * temp_ratio * d_b1
        self._W2_temp += lr * temp_ratio * d_W2
        self._b2_temp += lr * temp_ratio * d_b2
        self._Wout_temp += lr * temp_ratio * d_Wout
        self._bout_temp += lr * temp_ratio * d_bout

        return pred_error

    def consolidate(self, strength: float) -> None:
        """Transfer temporary weights to permanent (consolidation).

        Called when a high-quality memory is stored. The strength parameter
        controls how much of the temporary weights get promoted.

        Args:
            strength: Consolidation strength in [0, 1].
        """
        strength = np.clip(strength, 0.0, 1.0)
        transfer = strength * 0.1  # conservative transfer rate

        self._W1_perm += transfer * self._W1_temp
        self._b1_perm += transfer * self._b1_temp
        self._W2_perm += transfer * self._W2_temp
        self._b2_perm += transfer * self._b2_temp
        self._Wout_perm += transfer * self._Wout_temp
        self._bout_perm += transfer * self._bout_temp

        # Reduce temporary proportionally
        self._W1_temp *= (1.0 - transfer)
        self._b1_temp *= (1.0 - transfer)
        self._W2_temp *= (1.0 - transfer)
        self._b2_temp *= (1.0 - transfer)
        self._Wout_temp *= (1.0 - transfer)
        self._bout_temp *= (1.0 - transfer)

    def get_h1_cache(self) -> dict[str, np.ndarray]:
        """Return a copy of the h1 activation cache."""
        return dict(self._h1_cache)

    def get_weights(self) -> dict[str, np.ndarray]:
        """Serialize all weights for persistence."""
        return {
            "W1_perm": self._W1_perm.copy(),
            "b1_perm": self._b1_perm.copy(),
            "W2_perm": self._W2_perm.copy(),
            "b2_perm": self._b2_perm.copy(),
            "Wout_perm": self._Wout_perm.copy(),
            "bout_perm": self._bout_perm.copy(),
            "W1_temp": self._W1_temp.copy(),
            "b1_temp": self._b1_temp.copy(),
            "W2_temp": self._W2_temp.copy(),
            "b2_temp": self._b2_temp.copy(),
            "Wout_temp": self._Wout_temp.copy(),
            "bout_temp": self._bout_temp.copy(),
        }

    def set_weights(self, weights: dict[str, np.ndarray]) -> None:
        """Restore weights from a saved state."""
        self._W1_perm = weights["W1_perm"].astype(np.float32)
        self._b1_perm = weights["b1_perm"].astype(np.float32)
        self._W2_perm = weights["W2_perm"].astype(np.float32)
        self._b2_perm = weights["b2_perm"].astype(np.float32)
        self._Wout_perm = weights["Wout_perm"].astype(np.float32)
        self._bout_perm = weights["bout_perm"].astype(np.float32)
        self._W1_temp = weights["W1_temp"].astype(np.float32)
        self._b1_temp = weights["b1_temp"].astype(np.float32)
        self._W2_temp = weights["W2_temp"].astype(np.float32)
        self._b2_temp = weights["b2_temp"].astype(np.float32)
        self._Wout_temp = weights["Wout_temp"].astype(np.float32)
        self._bout_temp = weights["bout_temp"].astype(np.float32)

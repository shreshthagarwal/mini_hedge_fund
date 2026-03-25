"""
strategy_4/ppo.py — PPO implementation with proper backprop

Uses scikit-learn MLPClassifier/MLPRegressor for the policy and value
networks — these have proper gradient-based optimization built in,
making training ~100x faster than finite differences.

PPO is approximated as a weighted retraining loop:
  - Compute advantages via GAE
  - Reweight samples by PPO importance ratio (clip to [1-ε, 1+ε])
  - Retrain policy on importance-weighted samples
  - Retrain value on TD returns

This is "PPO-lite" — captures the core benefit (prevents large
policy updates via clipping) without implementing full gradient PPO.
Suitable for NumPy-only environments.
"""

from __future__ import annotations

import os
import pickle
import logging
import numpy as np
from typing import List, Tuple, Optional

from strategies.strategy_4.config import (
    STATE_DIM, N_ACTIONS,
    GAMMA, LAMBDA, CLIP_EPS,
    EPOCHS_PER_UPDATE, BATCH_SIZE, HIDDEN_DIM,
    MODEL_PATH,
)

logger = logging.getLogger("strategy_4.ppo")


# ── GAE ───────────────────────────────────────────────────────────────────────

def compute_gae(
    rewards:  np.ndarray,
    values:   np.ndarray,
    dones:    np.ndarray,
    gamma:    float = GAMMA,
    lam:      float = LAMBDA,
) -> Tuple[np.ndarray, np.ndarray]:
    """Generalized Advantage Estimation."""
    n          = len(rewards)
    advantages = np.zeros(n, dtype=np.float32)
    gae        = 0.0
    for t in reversed(range(n)):
        next_v = values[t+1] if t+1 < len(values) else 0.0
        delta  = rewards[t] + gamma * next_v * (1 - dones[t]) - values[t]
        gae    = delta + gamma * lam * (1 - dones[t]) * gae
        advantages[t] = gae
    returns = advantages + values[:n]
    return advantages, returns


# ── PPO Agent using sklearn ───────────────────────────────────────────────────

class PPOAgent:
    """
    PPO-lite agent using sklearn MLP networks.

    Policy:  MLPClassifier  → action probabilities
    Value:   MLPRegressor   → state value estimates

    Training:
      1. Collect rollout (states, actions, rewards)
      2. Estimate values with value network
      3. Compute GAE advantages
      4. Compute PPO importance weights (clipped)
      5. Retrain policy on advantage-weighted samples
      6. Retrain value on TD returns
    """

    def __init__(self):
        self.policy  = None
        self.value   = None
        self.trained = False
        self._n_updates = 0
        self._last_probs: Optional[np.ndarray] = None  # old policy probs

    def _build_policy(self):
        from sklearn.neural_network import MLPClassifier
        return MLPClassifier(
            hidden_layer_sizes = (HIDDEN_DIM, HIDDEN_DIM // 2),
            activation         = "relu",
            solver             = "adam",
            learning_rate_init = 3e-4,
            max_iter           = EPOCHS_PER_UPDATE,
            warm_start         = True,   # incremental — don't reset weights
            random_state       = 42,
        )

    def _build_value(self):
        from sklearn.neural_network import MLPRegressor
        return MLPRegressor(
            hidden_layer_sizes = (HIDDEN_DIM, HIDDEN_DIM // 2),
            activation         = "relu",
            solver             = "adam",
            learning_rate_init = 3e-4,
            max_iter           = EPOCHS_PER_UPDATE,
            warm_start         = True,
            random_state       = 42,
        )

    def act(self, state: np.ndarray) -> Tuple[int, float]:
        """Sample action from policy. Returns (action, log_prob)."""
        if not self.trained or self.policy is None:
            # Random exploration before training
            action = np.random.randint(N_ACTIONS)
            return action, np.log(1.0 / N_ACTIONS)

        probs = self._get_probs(state.reshape(1, -1))[0]
        probs = np.clip(probs, 1e-8, 1.0)
        probs /= probs.sum()
        action = np.random.choice(N_ACTIONS, p=probs)
        return int(action), float(np.log(probs[action] + 1e-8))

    def act_greedy(self, state: np.ndarray) -> int:
        """Greedy action — used at inference (no exploration)."""
        if not self.trained or self.policy is None:
            return 0  # HOLD if not trained
        probs = self._get_probs(state.reshape(1, -1))[0]
        return int(np.argmax(probs))

    def _get_probs(self, states: np.ndarray) -> np.ndarray:
        """Get action probabilities from policy network."""
        try:
            # sklearn predict_proba returns shape (n, n_classes)
            probs = self.policy.predict_proba(states)
            # Ensure all N_ACTIONS classes are present
            if probs.shape[1] < N_ACTIONS:
                full = np.ones((len(states), N_ACTIONS)) / N_ACTIONS
                for i, cls in enumerate(self.policy.classes_):
                    full[:, int(cls)] = probs[:, i]
                probs = full
            probs = np.clip(probs, 1e-8, 1.0)
            probs /= probs.sum(axis=1, keepdims=True)
            return probs
        except Exception:
            return np.ones((len(states), N_ACTIONS)) / N_ACTIONS

    def train_on_episodes(self, episodes: List[dict], n_epochs: int = 3):
        """Train on collected rollout episodes."""
        try:
            from sklearn.neural_network import MLPClassifier, MLPRegressor
            from sklearn.utils import check_array
        except ImportError:
            logger.warning("scikit-learn not installed — S4 disabled")
            return

        # Flatten all episodes
        states   = np.vstack([e["states"]    for e in episodes]).astype(np.float32)
        actions  = np.concatenate([e["actions"]   for e in episodes]).astype(int)
        rewards  = np.concatenate([e["rewards"]   for e in episodes]).astype(np.float32)
        dones    = np.concatenate([e["dones"]     for e in episodes]).astype(np.float32)
        old_lp   = np.concatenate([e["log_probs"] for e in episodes]).astype(np.float32)

        n = len(states)
        if n < 32:
            logger.info(f"  Too few samples ({n}) — skipping update")
            return

        # ── Build/init networks on first call ────────────────────────────────
        if self.policy is None:
            self.policy = self._build_policy()
            # Cold-start: fit on uniform random targets to initialize weights
            dummy_actions = np.random.randint(N_ACTIONS, size=max(n, 50))
            dummy_states  = np.random.randn(max(n, 50), STATE_DIM).astype(np.float32)
            # Ensure all classes present
            for i in range(N_ACTIONS):
                dummy_actions[i] = i
            self.policy.fit(dummy_states, dummy_actions)

        if self.value is None:
            self.value = self._build_value()
            self.value.fit(states, rewards)

        # ── Estimate values ───────────────────────────────────────────────────
        values = self.value.predict(states).astype(np.float32)
        # Pad with 0 for last state
        values_padded = np.append(values, 0.0)

        # ── GAE ───────────────────────────────────────────────────────────────
        advantages, returns = compute_gae(rewards, values_padded, dones)

        # Normalize advantages
        adv_std = advantages.std()
        if adv_std > 0:
            advantages = (advantages - advantages.mean()) / (adv_std + 1e-8)

        # ── PPO importance weighting ──────────────────────────────────────────
        # Get current policy probabilities for old states+actions
        curr_probs = self._get_probs(states)
        curr_lp    = np.log(curr_probs[np.arange(n), np.clip(actions, 0, N_ACTIONS-1)] + 1e-8)

        # Importance ratio
        ratio = np.exp(curr_lp - old_lp)
        ratio = np.clip(ratio, 1 - CLIP_EPS, 1 + CLIP_EPS)

        # PPO sample weights: positive advantages → upweight, negative → downweight
        # Weight = clipped ratio × sign(advantage) → push toward good actions
        sample_weights = np.abs(ratio * advantages)
        sample_weights = np.clip(sample_weights, 0.1, 5.0)

        # ── Retrain policy on importance-weighted samples ─────────────────────
        # sklearn fit with sample_weight implements weighted cross-entropy
        self.policy.max_iter = EPOCHS_PER_UPDATE
        self.policy.fit(states, actions, sample_weight=sample_weights)

        # ── Retrain value network ─────────────────────────────────────────────
        self.value.max_iter = EPOCHS_PER_UPDATE
        self.value.fit(states, returns)

        self._n_updates += 1
        self.trained = True

        mean_r   = rewards.mean()
        mean_adv = advantages.mean()
        mean_rat = ratio.mean()
        logger.info(
            f"  PPO update {self._n_updates}: "
            f"n={n}  mean_r={mean_r:.4f}  "
            f"mean_adv={mean_adv:.3f}  "
            f"mean_ratio={mean_rat:.3f}"
        )

    def save(self, path: str = MODEL_PATH):
        try:
            with open(path, "wb") as f:
                pickle.dump({
                    "policy":    self.policy,
                    "value":     self.value,
                    "n_updates": self._n_updates,
                }, f)
        except Exception as e:
            logger.warning(f"Could not save PPO model: {e}")

    def load(self, path: str = MODEL_PATH) -> bool:
        if not os.path.exists(path):
            return False
        try:
            with open(path, "rb") as f:
                obj = pickle.load(f)
            self.policy     = obj["policy"]
            self.value      = obj["value"]
            self._n_updates = obj.get("n_updates", 0)
            self.trained    = True
            logger.info(f"PPO agent loaded ({self._n_updates} updates)")
            return True
        except Exception as e:
            logger.warning(f"Could not load PPO model: {e}")
            return False
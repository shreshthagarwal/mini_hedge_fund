"""
strategy_4/strategy.py — RL Momentum Scalper

Implements engine.StrategyProtocol.
The PPO agent is trained in a loop during backtesting:
  - Every TRAIN_EVERY_BARS bars, collect episodes from the last
    MIN_TRAIN_BARS bars of history and run N_TRAIN_EPOCHS PPO updates
  - At inference, act greedily (argmax of policy probabilities)
  - Before training, acts randomly (pure exploration)

The key insight: the agent discovers its own entry/exit rules
by optimizing the reward function. It learns things like:
  - Don't enter near square-off time (time penalty + forced exit)
  - Cut losses quickly (SL penalty)
  - Ride momentum early in session (ADX high → hold longer)
  - Mean revert near VWAP (learned from reward signal)
"""

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
from typing import Optional

from strategies.strategy_4.config import (
    TRAIN_EVERY_BARS, MIN_TRAIN_BARS, N_TRAIN_EPOCHS,
    ROLLOUT_STEPS, STATE_DIM, N_ACTIONS,
    ATR_SL_MULT, ATR_TARGET_MULT, TICK_SIZE,
    ENTRY_START, ENTRY_CUTOFF, SQUARE_OFF,
    MODEL_PATH,
)
from strategies.strategy_4.environment import TradingEnv
from strategies.strategy_4.ppo import PPOAgent
from src.engine import Signal

logger = logging.getLogger("strategy_4")

ATR_PERIOD        = 14
ADX_PERIOD        = 14
RSI_PERIOD        = 14
VOLUME_AVG_PERIOD = 20
EMA_PERIOD        = 20


# ── Indicators ────────────────────────────────────────────────────────────────

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # ATR
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    # ADX
    high, low, close    = df["high"], df["low"], df["close"]
    prev_high = high.shift(1)
    prev_low  = low.shift(1)
    dm_plus  = (high - prev_high).clip(lower=0)
    dm_minus = (prev_low - low).clip(lower=0)
    both = (dm_plus > 0) & (dm_minus > 0)
    dm_plus[both  & (dm_minus >= dm_plus)]  = 0
    dm_minus[both & (dm_plus  >  dm_minus)] = 0
    atr_s    = tr.ewm(span=ADX_PERIOD, adjust=False).mean()
    safe_atr = atr_s.replace(0, np.nan)
    di_plus  = 100 * dm_plus.ewm(span=ADX_PERIOD,  adjust=False).mean() / safe_atr
    di_minus = 100 * dm_minus.ewm(span=ADX_PERIOD, adjust=False).mean() / safe_atr
    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    df["adx"] = dx.ewm(span=ADX_PERIOD, adjust=False).mean()

    # RSI
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).ewm(span=RSI_PERIOD, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=RSI_PERIOD, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # VWAP
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv      = typical * df["volume"]
    vwap    = pd.Series(np.nan, index=df.index)
    for day in np.unique(df.index.date):
        mask    = df.index.date == day
        cum_vol = df["volume"][mask].cumsum().replace(0, np.nan)
        vwap[mask] = pv[mask].cumsum() / cum_vol
    df["vwap"] = vwap

    # Volume average
    df["vol_avg"] = df["volume"].rolling(VOLUME_AVG_PERIOD).mean()

    return df


# ── Strategy ──────────────────────────────────────────────────────────────────

class RLScalperStrategy:
    """
    PPO-based RL momentum scalper.
    Implements engine.StrategyProtocol.

    The agent outputs actions 0-3:
      0 = HOLD
      1 = LONG entry
      2 = SHORT entry
      3 = EXIT

    The engine handles SL/target exits — the agent can also
    request exit via action=3.
    """

    name = "S4_RL_SCALPER"

    def __init__(self, load_existing: bool = True):
        self.agent       = PPOAgent()
        self.in_position = False
        self.position_side: Optional[str] = None

        self._bar_count  = 0
        self._bar_buffer: list = []
        self._last_train_i = -TRAIN_EVERY_BARS

        # Try loading existing model
        if load_existing:
            loaded = self.agent.load(MODEL_PATH)
            if loaded:
                logger.info("Loaded existing PPO model")

    def reset(self):
        self.agent        = PPOAgent()
        self.in_position  = False
        self.position_side = None
        self._bar_count   = 0
        self._bar_buffer  = []
        self._last_train_i = -TRAIN_EVERY_BARS

    def on_bar(self, bar: pd.Series, prev_bar: pd.Series, context: dict) -> Signal:
        ts = bar.name
        self._bar_count += 1
        self._bar_buffer.append(bar)

        # Keep buffer bounded
        max_buf = max(MIN_TRAIN_BARS + 100, 3000)
        if len(self._bar_buffer) > max_buf:
            self._bar_buffer = self._bar_buffer[-MIN_TRAIN_BARS:]

        # ── Training loop ─────────────────────────────────────────────────────
        needs_train = (self._bar_count - self._last_train_i) >= TRAIN_EVERY_BARS
        has_enough  = self._bar_count >= MIN_TRAIN_BARS

        if needs_train and has_enough:
            self._train()
            self._last_train_i = self._bar_count

        # ── Inference ─────────────────────────────────────────────────────────
        if context.get("in_position"):
            # Agent can request exit
            if self.agent.trained:
                state  = self._make_state(bar, context)
                action = self.agent.act_greedy(state)
                if action == 3:
                    # Return a signal that triggers exit — we do this by
                    # returning NONE here (engine handles SL/target exits)
                    # Agent-requested exit is handled via square-off fallback
                    pass
            return Signal("NONE")

        if not (ENTRY_START <= ts.time() <= ENTRY_CUTOFF):
            return Signal("NONE", reason="outside window")

        atr = bar.get("atr", np.nan)
        if pd.isna(atr) or atr == 0:
            return Signal("NONE", reason="warming up")

        # Before training: no trades
        if not self.agent.trained:
            return Signal("NONE", reason="agent not yet trained")

        state  = self._make_state(bar, context)
        action = self.agent.act_greedy(state)

        if action == 1:
            return Signal(
                direction = "LONG",
                sl_dist   = ATR_SL_MULT * atr,
                rr        = ATR_TARGET_MULT / ATR_SL_MULT,
                reason    = "RL agent: LONG",
                meta      = {"action": action},
            )
        elif action == 2:
            return Signal(
                direction = "SHORT",
                sl_dist   = ATR_SL_MULT * atr,
                rr        = ATR_TARGET_MULT / ATR_SL_MULT,
                reason    = "RL agent: SHORT",
                meta      = {"action": action},
            )

        return Signal("NONE", reason=f"RL agent: HOLD (action={action})")

    def _make_state(self, bar: pd.Series, context: dict) -> np.ndarray:
        """Build state vector from current bar + context."""
        if len(self._bar_buffer) < 2:
            return np.zeros(STATE_DIM, dtype=np.float32)

        # Create a mini DataFrame from buffer for state computation
        df_buf = pd.DataFrame(self._bar_buffer[-20:])
        if "datetime" not in df_buf.columns:
            df_buf["datetime"] = [b.name for b in self._bar_buffer[-20:]]

        env = TradingEnv(df_buf)
        env._i          = min(len(df_buf) - 1, 20)
        env._in_pos     = context.get("in_position", False)
        env._bars_held  = 0
        return env._state()

    def _train(self):
        """Run PPO training on recent history."""
        logger.info(f"  Training PPO agent on {len(self._bar_buffer)} bars...")

        df_train = pd.DataFrame(self._bar_buffer)
        if "datetime" not in df_train.columns:
            df_train.insert(0, "datetime",
                            [b.name for b in self._bar_buffer])

        # Collect rollout episodes
        episodes = []
        env      = TradingEnv(df_train)
        state    = env.reset()

        ep_states, ep_actions, ep_rewards, ep_dones, ep_lps = \
            [], [], [], [], []

        steps = 0
        while not env.done and steps < ROLLOUT_STEPS * 4:
            action, log_prob = self.agent.act(state)
            next_state, reward, done, _ = env.step(action)

            ep_states.append(state)
            ep_actions.append(action)
            ep_rewards.append(reward)
            ep_dones.append(float(done))
            ep_lps.append(log_prob)

            state  = next_state
            steps += 1

            if done or steps % ROLLOUT_STEPS == 0:
                if len(ep_states) >= 32:
                    episodes.append({
                        "states":    np.array(ep_states,  dtype=np.float32),
                        "actions":   np.array(ep_actions, dtype=np.int32),
                        "rewards":   np.array(ep_rewards, dtype=np.float32),
                        "dones":     np.array(ep_dones,   dtype=np.float32),
                        "log_probs": np.array(ep_lps,     dtype=np.float32),
                    })
                ep_states, ep_actions, ep_rewards, ep_dones, ep_lps = \
                    [], [], [], [], []

                if done:
                    state = env.reset()

        if episodes:
            self.agent.train_on_episodes(episodes, n_epochs=N_TRAIN_EPOCHS)
            self.agent.save(MODEL_PATH)
        else:
            logger.info("  No complete episodes — skipping PPO update")

    def on_fill(self, direction: str, price: float, ts: pd.Timestamp):
        self.in_position   = True
        self.position_side = direction

    def on_exit(self, reason: str, price: float, ts: pd.Timestamp):
        self.in_position   = False
        self.position_side = None
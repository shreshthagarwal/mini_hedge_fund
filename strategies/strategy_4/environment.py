"""
strategy_4/environment.py — Trading environment for RL agent

Implements a Gym-style trading environment for NIFTY 5-min futures.
The agent steps through bars one at a time, receiving state observations
and rewards based on its actions.

State vector (STATE_DIM = 47):
  [0:20]   normalized 20-bar returns
  [20:40]  volume ratios vs 20-bar rolling average
  [40]     ATR / close (normalized volatility)
  [41]     ADX / 100
  [42]     RSI / 100
  [43]     VWAP distance / ATR
  [44]     time of day (0=9:15, 1=15:25)
  [45]     in_position (0 or 1)
  [46]     bars_held / MAX_HOLD_BARS

Reward:
  step reward = -TIME_PENALTY if in position
  exit reward = net_pnl / (capital * REWARD_SCALE) + bonuses/penalties
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Optional, Tuple

from strategies.strategy_4.config import (
    LOOKBACK_BARS, STATE_DIM, N_ACTIONS,
    ATR_SL_MULT, ATR_TARGET_MULT, MAX_HOLD_BARS,
    REWARD_SCALE, TIME_PENALTY, SL_PENALTY, TARGET_BONUS,
    LOT_SIZE, COMMISSION_PER_LOT, TICK_SIZE,
    SQUARE_OFF, ENTRY_START, ENTRY_CUTOFF,
    INITIAL_CAPITAL,
)


class TradingEnv:
    """
    Step-by-step trading environment.

    Usage:
        env = TradingEnv(df)
        state = env.reset()
        while not done:
            action = agent.act(state)
            state, reward, done, info = env.step(action)
    """

    def __init__(self, df: pd.DataFrame, capital: float = INITIAL_CAPITAL):
        self.df      = df.reset_index()
        self.capital = capital
        self.n       = len(df)
        self._i      = LOOKBACK_BARS
        self._reset_position()

    def reset(self, start_i: Optional[int] = None) -> np.ndarray:
        self._i = start_i if start_i is not None else LOOKBACK_BARS
        self._reset_position()
        return self._state()

    def _reset_position(self):
        self._in_pos    = False
        self._pos_dir   = 0        # 1=long, -1=short
        self._entry_px  = 0.0
        self._sl        = 0.0
        self._target    = 0.0
        self._bars_held = 0

    def step(self, action: int) -> Tuple[np.ndarray, float, bool, dict]:
        """
        action: 0=HOLD, 1=LONG, 2=SHORT, 3=EXIT
        Returns: (next_state, reward, done, info)
        """
        bar   = self.df.iloc[self._i]
        ts    = bar.get("datetime", None)
        close = bar["close"]
        high  = bar["high"]
        low   = bar["low"]
        atr   = bar.get("atr", close * 0.005)

        reward = 0.0
        done   = False
        info   = {}

        # ── Check forced exits ────────────────────────────────────────────────
        if self._in_pos:
            self._bars_held += 1
            exit_price  = None
            exit_reason = None

            # Square-off time
            if ts is not None and pd.Timestamp(ts).time() >= SQUARE_OFF:
                exit_price  = close
                exit_reason = "square-off"

            # Max hold time
            elif self._bars_held >= MAX_HOLD_BARS:
                exit_price  = close
                exit_reason = "max-hold"

            # SL / Target
            elif self._pos_dir == 1:
                if low <= self._sl:
                    exit_price  = self._sl
                    exit_reason = "stop-loss"
                elif high >= self._target:
                    exit_price  = self._target
                    exit_reason = "target"

            elif self._pos_dir == -1:
                if high >= self._sl:
                    exit_price  = self._sl
                    exit_reason = "stop-loss"
                elif low <= self._target:
                    exit_price  = self._target
                    exit_reason = "target"

            # Agent-requested exit
            elif action == 3:
                exit_price  = close
                exit_reason = "agent-exit"

            if exit_price is not None:
                pts   = (exit_price - self._entry_px) * self._pos_dir
                gross = pts * LOT_SIZE
                net   = gross - COMMISSION_PER_LOT * 2
                reward += net / (self.capital * REWARD_SCALE)

                if exit_reason == "stop-loss":
                    reward -= SL_PENALTY
                elif exit_reason == "target":
                    reward += TARGET_BONUS

                info = {"exit": exit_reason, "pnl": net, "pts": pts}
                self._reset_position()

            else:
                # Still in position — time penalty
                reward -= TIME_PENALTY

        # ── Process entry actions (only if flat) ──────────────────────────────
        if not self._in_pos:
            in_window = True
            if ts is not None:
                t = pd.Timestamp(ts).time()
                in_window = ENTRY_START <= t <= ENTRY_CUTOFF

            if in_window and action in (1, 2):
                slippage   = TICK_SIZE
                self._pos_dir = 1 if action == 1 else -1

                if self._pos_dir == 1:
                    self._entry_px = round(close + slippage, 2)
                    self._sl       = round(self._entry_px - ATR_SL_MULT * atr, 2)
                    self._target   = round(self._entry_px + ATR_TARGET_MULT * atr, 2)
                else:
                    self._entry_px = round(close - slippage, 2)
                    self._sl       = round(self._entry_px + ATR_SL_MULT * atr, 2)
                    self._target   = round(self._entry_px - ATR_TARGET_MULT * atr, 2)

                self._in_pos    = True
                self._bars_held = 0

        # ── Advance ───────────────────────────────────────────────────────────
        self._i += 1
        done     = self._i >= self.n

        return self._state(), reward, done, info

    def _state(self) -> np.ndarray:
        if self._i >= self.n:
            return np.zeros(STATE_DIM, dtype=np.float32)

        window = self.df.iloc[max(0, self._i - LOOKBACK_BARS): self._i]
        bar    = self.df.iloc[self._i]
        state  = np.zeros(STATE_DIM, dtype=np.float32)

        closes  = window["close"].values
        volumes = window["volume"].values
        n       = len(closes)

        # Normalized returns
        if n > 1:
            rets = np.diff(closes) / (closes[:-1] + 1e-8)
            rets = np.clip(rets, -0.1, 0.1)
            state[:min(n-1, LOOKBACK_BARS)] = rets[-LOOKBACK_BARS:]

        # Volume ratios
        vol_avg = volumes.mean() if volumes.mean() > 0 else 1
        vols    = volumes / vol_avg
        vols    = np.clip(vols, 0, 5)
        state[LOOKBACK_BARS:LOOKBACK_BARS + min(n, LOOKBACK_BARS)] = \
            vols[-LOOKBACK_BARS:]

        # Indicators
        close  = bar["close"]
        atr    = float(bar.get("atr", close * 0.005))
        adx    = float(bar.get("adx", 25)) / 100
        rsi    = float(bar.get("rsi", 50)) / 100
        vwap   = float(bar.get("vwap", close))
        vwap_d = (close - vwap) / (atr + 1e-8)

        ts = bar.get("datetime")
        if ts is not None:
            t = pd.Timestamp(ts)
            time_norm = (t.hour * 60 + t.minute - 9*60 - 15) / (6*60+15)
        else:
            time_norm = 0.5

        state[LOOKBACK_BARS * 2]     = np.clip(atr / (close + 1e-8) * 100, 0, 5)
        state[LOOKBACK_BARS * 2 + 1] = np.clip(adx, 0, 1)
        state[LOOKBACK_BARS * 2 + 2] = np.clip(rsi, 0, 1)
        state[LOOKBACK_BARS * 2 + 3] = np.clip(vwap_d, -5, 5) / 5
        state[LOOKBACK_BARS * 2 + 4] = np.clip(time_norm, 0, 1)
        state[LOOKBACK_BARS * 2 + 5] = float(self._in_pos)
        state[LOOKBACK_BARS * 2 + 6] = self._bars_held / MAX_HOLD_BARS

        return state.astype(np.float32)

    @property
    def current_bar(self) -> Optional[pd.Series]:
        if self._i < self.n:
            return self.df.iloc[self._i]
        return None

    @property
    def done(self) -> bool:
        return self._i >= self.n
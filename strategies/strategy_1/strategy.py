"""
strategy_1/strategy.py — ORB + ADX, implementing engine.StrategyProtocol

Plugs into src/engine.py BacktestEngine.
All parameters in strategy_1/config.py.
"""

from __future__ import annotations

import datetime
import numpy as np
import pandas as pd
from typing import Optional
import logging

from strategies.strategy_1.config import (
    ATR_PERIOD, ADX_PERIOD, EMA_PERIOD,
    VOLUME_AVG_PERIOD, VOLUME_SPIKE_MULT,
    OR_BARS, ADX_THRESHOLD, ATR_SL_MULT, RR_RATIO,
    ORB_ENTRY_CUTOFF, ORB_DEAD_ZONE_START, ORB_DEAD_ZONE_END,
    ORB_SKIP_SLOTS, TRADE_DIRECTION, SQUARE_OFF,
)
from src.engine import Signal

logger = logging.getLogger("strategy_1")


# ── Indicators ────────────────────────────────────────────────────────────────

def compute_atr(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=ATR_PERIOD, adjust=False).mean()


def compute_adx(df: pd.DataFrame) -> pd.Series:
    high, low, close    = df["high"], df["low"], df["close"]
    prev_high = high.shift(1)
    prev_low  = low.shift(1)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
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
    return dx.ewm(span=ADX_PERIOD, adjust=False).mean()


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv      = typical * df["volume"]
    vwap    = pd.Series(np.nan, index=df.index)
    for day in np.unique(df.index.date):
        mask    = df.index.date == day
        cum_vol = df["volume"][mask].cumsum().replace(0, np.nan)
        vwap[mask] = pv[mask].cumsum() / cum_vol
    return vwap


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["atr"]       = compute_atr(df)
    df["adx"]       = compute_adx(df)
    df["vwap"]      = compute_vwap(df)
    df["ema20"]     = df["close"].ewm(span=EMA_PERIOD, adjust=False).mean()
    df["vol_avg"]   = df["volume"].rolling(VOLUME_AVG_PERIOD).mean()
    df["vol_spike"] = df["volume"] > (VOLUME_SPIKE_MULT * df["vol_avg"])
    return df


# ── Opening Range Tracker ─────────────────────────────────────────────────────

class OpeningRangeTracker:
    def __init__(self):
        self._date:    Optional[datetime.date] = None
        self._bars:    list                    = []
        self.or_high:  Optional[float]         = None
        self.or_low:   Optional[float]         = None
        self.or_ready: bool                    = False

    def update(self, bar: pd.Series):
        day = bar.name.date()
        if day != self._date:
            self._date    = day
            self._bars    = []
            self.or_high  = None
            self.or_low   = None
            self.or_ready = False
        if not self.or_ready:
            self._bars.append(bar)
            if len(self._bars) >= OR_BARS:
                self.or_high  = max(b["high"] for b in self._bars)
                self.or_low   = min(b["low"]  for b in self._bars)
                self.or_ready = True


# ── ORB Strategy ──────────────────────────────────────────────────────────────

class ORBStrategy:
    """
    Opening Range Breakout + ADX filter.
    Implements engine.StrategyProtocol.

    on_bar() returns Signal with sl_dist set.
    Engine handles entry at next bar open + slippage.
    """

    name = "S1_ORB"

    def __init__(self):
        self.or_tracker     = OpeningRangeTracker()
        self.in_position    = False
        self.position_side: Optional[str] = None

    def reset(self):
        self.or_tracker  = OpeningRangeTracker()
        self.in_position = False
        self.position_side = None

    def on_bar(self, bar: pd.Series, prev_bar: pd.Series, context: dict) -> Signal:
        ts    = bar.name
        close = bar["close"]
        atr   = bar.get("atr", np.nan)
        adx   = bar.get("adx", np.nan)

        self.or_tracker.update(bar)

        if context.get("in_position"):
            return Signal("NONE")

        if pd.isna(atr) or pd.isna(adx) or atr == 0:
            return Signal("NONE", reason="warming up")

        if not self.or_tracker.or_ready:
            return Signal("NONE", reason="building OR")

        if ts.time() > ORB_ENTRY_CUTOFF:
            return Signal("NONE", reason="past cutoff")

        if ORB_DEAD_ZONE_START <= ts.time() <= ORB_DEAD_ZONE_END:
            return Signal("NONE", reason="dead zone")

        if (ts.hour, ts.minute) in ORB_SKIP_SLOTS:
            return Signal("NONE", reason="skip slot")

        if adx < ADX_THRESHOLD:
            return Signal("NONE", reason=f"ADX {adx:.1f} < {ADX_THRESHOLD}")

        or_high = self.or_tracker.or_high
        or_low  = self.or_tracker.or_low
        sl_dist = ATR_SL_MULT * atr

        if TRADE_DIRECTION in ("BOTH", "LONG_ONLY"):
            if close > or_high and prev_bar["close"] <= or_high:
                return Signal(
                    direction = "LONG",
                    sl_dist   = sl_dist,
                    rr        = RR_RATIO,
                    reason    = f"ORB long  H={or_high:.0f}  ADX={adx:.0f}",
                    meta      = {"or_high": or_high, "or_low": or_low,
                                 "adx": adx, "atr": atr},
                )

        if TRADE_DIRECTION in ("BOTH", "SHORT_ONLY"):
            if close < or_low and prev_bar["close"] >= or_low:
                return Signal(
                    direction = "SHORT",
                    sl_dist   = sl_dist,
                    rr        = RR_RATIO,
                    reason    = f"ORB short L={or_low:.0f}  ADX={adx:.0f}",
                    meta      = {"or_high": or_high, "or_low": or_low,
                                 "adx": adx, "atr": atr},
                )

        return Signal("NONE", reason="no breakout")

    def on_fill(self, direction: str, price: float, ts: pd.Timestamp):
        self.in_position   = True
        self.position_side = direction

    def on_exit(self, reason: str, price: float, ts: pd.Timestamp):
        self.in_position   = False
        self.position_side = None
"""
Strategy module — NIFTY 5-min Futures

Contains:
  - ORBStrategy  : Opening Range Breakout + ADX filter  (active)
  - VWAPEMAStrategy : VWAP + EMA Momentum (kept for reference)

All tunable parameters live in config.py — change them there, not here.
"""

import datetime
import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional, Literal
from enum import Enum

from config import (
    ATR_PERIOD, RR_RATIO, ATR_SL_MULT,
    ENTRY_CUTOFF, SQUARE_OFF,
    EMA_PERIOD, VOLUME_AVG_PERIOD, VOLUME_SPIKE_MULT, ENTRY_START,
    OR_BARS, ADX_PERIOD, ADX_THRESHOLD, ORB_ENTRY_CUTOFF, TRADE_DIRECTION,
)
from utils import get_logger, round_to_tick

logger = get_logger("strategy")


# ─── Signal types ─────────────────────────────────────────────────────────────

class Signal(Enum):
    LONG  = "LONG"
    SHORT = "SHORT"
    EXIT  = "EXIT"
    NONE  = "NONE"


@dataclass
class TradeSignal:
    signal: Signal
    entry:  float
    sl:     float
    target: float
    reason: str
    atr:    float


# ═══════════════════════════════════════════════════════════════════════════════
# Indicators
# ═══════════════════════════════════════════════════════════════════════════════

def compute_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def compute_adx(df: pd.DataFrame, period: int = ADX_PERIOD) -> pd.Series:
    """Average Directional Index — measures trend strength (not direction)."""
    high, low, close      = df["high"], df["low"], df["close"]
    prev_high, prev_low   = high.shift(1), low.shift(1)
    prev_close            = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    dm_plus  = (high - prev_high).clip(lower=0)
    dm_minus = (prev_low - low).clip(lower=0)
    both     = (dm_plus > 0) & (dm_minus > 0)
    dm_plus[both  & (dm_minus >= dm_plus)]  = 0
    dm_minus[both & (dm_plus  >  dm_minus)] = 0

    atr_s    = tr.ewm(span=period, adjust=False).mean()
    safe_atr = atr_s.replace(0, np.nan)
    di_plus  = 100 * dm_plus.ewm(span=period,  adjust=False).mean() / safe_atr
    di_minus = 100 * dm_minus.ewm(span=period, adjust=False).mean() / safe_atr
    dx       = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    return dx.ewm(span=period, adjust=False).mean()


def compute_ema(series: pd.Series, period: int = EMA_PERIOD) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP — resets at 09:15 each day."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv      = typical * df["volume"]
    vwap    = pd.Series(np.nan, index=df.index)
    for day in np.unique(df.index.date):
        mask          = df.index.date == day
        cum_vol       = df["volume"][mask].cumsum().replace(0, np.nan)
        vwap[mask]    = pv[mask].cumsum() / cum_vol
    return vwap


def compute_volume_avg(df: pd.DataFrame, period: int = VOLUME_AVG_PERIOD) -> pd.Series:
    return df["volume"].rolling(period).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all indicators used by ORBStrategy and VWAPEMAStrategy."""
    df         = df.copy()
    df["atr"]  = compute_atr(df)
    df["adx"]  = compute_adx(df)
    # VWAP + EMA + volume only needed by VWAPEMAStrategy
    # but cheap to compute — keep for analytics visibility
    df["vwap"]      = compute_vwap(df)
    df["ema20"]     = compute_ema(df["close"])
    df["vol_avg"]   = compute_volume_avg(df)
    df["vol_spike"] = df["volume"] > (VOLUME_SPIKE_MULT * df["vol_avg"])
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# Opening Range Tracker
# ═══════════════════════════════════════════════════════════════════════════════

class OpeningRangeTracker:
    """
    Builds the Opening Range from the first OR_BARS × 5-min candles each day.
    OR_BARS = 6  →  09:15 to 09:44  (30-min range)
    OR_BARS = 4  →  09:15 to 09:34  (20-min range)
    Set OR_BARS in config.py.
    """

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
                logger.debug(
                    f"OR set [{day}]  H:{self.or_high:.2f}  L:{self.or_low:.2f}"
                )


# ═══════════════════════════════════════════════════════════════════════════════
# ORB Strategy  (active)
# ═══════════════════════════════════════════════════════════════════════════════

class ORBStrategy:
    """
    Opening Range Breakout + ADX Trend Filter

    Entry:
      LONG  — close breaks above OR_HIGH  AND  ADX > ADX_THRESHOLD
      SHORT — close breaks below OR_LOW   AND  ADX > ADX_THRESHOLD

    Exit (handled by backtester/live_ticker):
      SL     = entry ± ATR_SL_MULT × ATR
      Target = RR_RATIO × SL distance
      Time   = 3:20 PM square-off

    Tune in config.py:
      OR_BARS          — opening range window (bars)
      ADX_THRESHOLD    — minimum trend strength to trade
      ATR_SL_MULT      — stop loss width in ATR units
      RR_RATIO         — reward-to-risk ratio
      ORB_ENTRY_CUTOFF — no new entries after this time
    """

    def __init__(self):
        self.or_tracker     = OpeningRangeTracker()
        self.in_position:   bool         = False
        self.position_side: Optional[str] = None

    def evaluate(self, bar: pd.Series, prev_bar: pd.Series) -> TradeSignal:
        ts    = bar.name
        close = bar["close"]
        atr   = bar.get("atr", np.nan)
        adx   = bar.get("adx", np.nan)

        self.or_tracker.update(bar)

        # Square-off
        if ts.time() >= SQUARE_OFF and self.in_position:
            return TradeSignal(Signal.EXIT, close, 0, 0, "3:20 PM square-off", atr)

        if pd.isna(atr) or pd.isna(adx):
            return TradeSignal(Signal.NONE, 0, 0, 0, "indicators not ready", 0)

        if self.in_position:
            return TradeSignal(Signal.NONE, 0, 0, 0, "holding", atr)

        if not self.or_tracker.or_ready:
            return TradeSignal(Signal.NONE, 0, 0, 0, "building opening range", atr)

        if ts.time() > ORB_ENTRY_CUTOFF:
            return TradeSignal(Signal.NONE, 0, 0, 0, "past entry cutoff", atr)

        if adx < ADX_THRESHOLD:
            return TradeSignal(Signal.NONE, 0, 0, 0, f"ADX {adx:.1f} < {ADX_THRESHOLD}", atr)

        or_high = self.or_tracker.or_high
        or_low  = self.or_tracker.or_low

        # LONG breakout
        if TRADE_DIRECTION in ("BOTH", "LONG_ONLY"):
            if close > or_high and prev_bar["close"] <= or_high:
                entry  = round_to_tick(close)
                sl     = round_to_tick(entry - ATR_SL_MULT * atr)
                target = round_to_tick(entry + RR_RATIO * (entry - sl))
                return TradeSignal(
                    Signal.LONG, entry, sl, target,
                    f"ORB long > {or_high:.0f}  ADX={adx:.0f}", atr
                )

        # SHORT breakdown
        if TRADE_DIRECTION in ("BOTH", "SHORT_ONLY"):
            if close < or_low and prev_bar["close"] >= or_low:
                entry  = round_to_tick(close)
                sl     = round_to_tick(entry + ATR_SL_MULT * atr)
                target = round_to_tick(entry - RR_RATIO * (sl - entry))
                return TradeSignal(
                    Signal.SHORT, entry, sl, target,
                    f"ORB short < {or_low:.0f}  ADX={adx:.0f}", atr
                )

        return TradeSignal(Signal.NONE, 0, 0, 0, "no breakout", atr)

    def enter(self, side: str):
        self.in_position   = True
        self.position_side = side

    def exit_position(self):
        self.in_position   = False
        self.position_side = None


# ═══════════════════════════════════════════════════════════════════════════════
# VWAP + EMA Strategy  (reference — not active)
# ═══════════════════════════════════════════════════════════════════════════════

class VWAPEMAStrategy:
    """
    Original VWAP + EMA Momentum Breakout strategy.
    Kept for reference and comparison backtests.
    To activate: change backtester.py to use VWAPEMAStrategy().
    """

    def __init__(self):
        self.in_position:   bool         = False
        self.position_side: Optional[str] = None

    def evaluate(self, bar: pd.Series, prev_bar: pd.Series) -> TradeSignal:
        ts    = bar.name
        close = bar["close"]
        atr   = bar.get("atr", np.nan)

        if not self._in_entry_window(ts):
            if self._is_squareoff_time(ts) and self.in_position:
                return TradeSignal(Signal.EXIT, close, 0, 0, "3:20 PM square-off", atr)
            return TradeSignal(Signal.NONE, 0, 0, 0, "outside entry window", atr)

        vwap  = bar.get("vwap",  np.nan)
        ema20 = bar.get("ema20", np.nan)

        if any(pd.isna([close, vwap, ema20, atr])):
            return TradeSignal(Signal.NONE, 0, 0, 0, "indicators not ready", 0)

        if self.in_position:
            if self.position_side == "LONG"  and close < vwap:
                return TradeSignal(Signal.EXIT, close, 0, 0, "VWAP cross short", atr)
            if self.position_side == "SHORT" and close > vwap:
                return TradeSignal(Signal.EXIT, close, 0, 0, "VWAP cross long", atr)
            return TradeSignal(Signal.NONE, 0, 0, 0, "holding", atr)

        vol_ok = bar.get("vol_spike", False)

        if close > vwap and ema20 > vwap and bar["high"] > prev_bar["high"] and vol_ok:
            entry  = round_to_tick(close)
            sl     = round_to_tick(entry - ATR_SL_MULT * atr)
            target = round_to_tick(entry + RR_RATIO * (entry - sl))
            return TradeSignal(Signal.LONG, entry, sl, target, "LONG breakout", atr)

        if close < vwap and ema20 < vwap and bar["low"] < prev_bar["low"] and vol_ok:
            entry  = round_to_tick(close)
            sl     = round_to_tick(entry + ATR_SL_MULT * atr)
            target = round_to_tick(entry - RR_RATIO * (sl - entry))
            return TradeSignal(Signal.SHORT, entry, sl, target, "SHORT breakdown", atr)

        return TradeSignal(Signal.NONE, 0, 0, 0, "no signal", atr)

    def enter(self, side: str):
        self.in_position   = True
        self.position_side = side

    def exit_position(self):
        self.in_position   = False
        self.position_side = None

    @staticmethod
    def _in_entry_window(ts: pd.Timestamp) -> bool:
        return ENTRY_START <= ts.time() <= ENTRY_CUTOFF

    @staticmethod
    def _is_squareoff_time(ts: pd.Timestamp) -> bool:
        return ts.time() >= SQUARE_OFF